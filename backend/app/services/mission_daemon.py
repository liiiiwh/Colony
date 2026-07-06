"""M1：Mission 运行时态管理 + 心跳监督。

**M1 设计（精简）**：
- 没有后台常驻协程。`start()` 只在 DB 写入 status="running" + 立即心跳。
- 真正会"做事"的循环放到 M2 引入 scheduler 后再由 cron / interval 触发 `run_once`；
  M1 阶段 daemon 是「逻辑上 running、物理上 idle」状态机。
- 在进程内维护一个轻量心跳 sweeper（M1 也起；每 30s 给所有 status='running' 的 project
  bump 一次 last_heartbeat_at）。crash 后 reconcile 通过心跳是否过期判断「真死了 vs
  正常运行」。
- 状态机：stopped → starting → running → stopping → stopped。任何时候挂了 → error。
- start/stop/restart 幂等；并发用 `_DAEMON_LOCK` 串行化。

进程外 worker（M2 Arq / Temporal 替换）只需要：实现同样的 _set_project_runtime 接口 +
heartbeat 协议即可。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 用模块属性访问而非 `from X import Y`，方便测试 monkeypatch
# (`app.db.session.AsyncSessionLocal` 在 conftest 中被替换为 sqlite test factory)
from app.db import session as _db_session
from app.models.mission import Mission, MissionAgentMemory, MissionRunState


def _open_session() -> AsyncSession:
    """每次现取 module attribute，避免被 `from X import Y` 早绑定。"""
    return _db_session.AsyncSessionLocal()

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ─────────────────────────── 配置 ───────────────────────────
HEARTBEAT_INTERVAL_SEC = 30
# ADR-028 D4 H6 · 单次 tick 墙钟封顶（秒）；超过即收尾 → paused_idle（下次 cron 重拉）。
# 防 super 在一个 tick 内无限绕（LLM 自循环 / 慢 worker 串联）卡死 running 常驻。
TICK_WALLCLOCK_CAP_SEC = 900
STALE_HEARTBEAT_SEC = 120  # 2x interval；超过即视为挂掉
STOP_WAIT_SEC = 10


# ─────────────────── 进程内 daemon 注册表（M1：仅记录已 start 的 mission_id） ───────────────────
@dataclass
class _DaemonEntry:
    started_at: datetime


_DAEMONS: dict[uuid.UUID, _DaemonEntry] = {}
_DAEMON_LOCK = asyncio.Lock()

# 正在跑 tick 的 mission → 加锁时刻 monotonic（防并发 run_once）。单事件循环里
# check+set 之间无 await → 原子。并发再触发 run_once 时直接 no-op skipped。
# 值是时间戳：锁持有超过 _TICK_LOCK_STALE_SEC = 僵尸（2026-07-03 事故：tick task 被
# 半途炸掉的 cancel 弄成永久挂起，finally 永远不跑 → 锁永久持有 → mission 挂死），
# run_once 判僵尸后抢锁自愈。
_TICKING_MISSIONS: dict[uuid.UUID, float] = {}
# 僵尸判定阈值：正常 tick 墙钟上限 + 充分余量（收尾/落库）。
_TICK_LOCK_STALE_SEC = TICK_WALLCLOCK_CAP_SEC + 300


# 心跳 sweeper：一个全局后台任务，遍历所有注册的 daemon 并 bump 心跳。
# 启动期 reconcile 之后才创建；shutdown 时取消。
_HEARTBEAT_SWEEPER_TASK: asyncio.Task | None = None

# fire-and-forget 后台任务的强引用 registry（2026-07-03 实证：无引用的 create_task
# 会被 GC **静默吞掉**——drain tick 跑到一半消失，finalize/收尾全没执行、零日志零异常。
# Python 文档明示必须保存 create_task 返回值）。done 后自动清出，防泄漏。
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro, *, name: str) -> asyncio.Task:
    """create_task + 持强引用 + done 自清。所有 fire-and-forget 都必须走这里。"""
    t = asyncio.create_task(coro, name=name)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


async def _heartbeat_sweep_pass(
    db: AsyncSession, pids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """给一批 pid 各 bump 一次心跳；返回「mission 已删→已摘除」的僵尸 pid 列表。

    健壮性约束（回归 F7 清理留下的僵尸 daemon 拖垮全平台）：
    - mission 已不在库（_get_or_create_run_state INSERT 会撞 FK）→ rollback 解毒 + 从
      _DAEMONS 摘除该 daemon，不再每轮重试；
    - 任意单 pid 失败都 rollback，**绝不污染同一 session 让后续真实 mission 的心跳连带失败**。
    """
    deregistered: list[uuid.UUID] = []
    for pid in pids:
        # 先判 mission 是否还在：没了直接摘僵尸，连 INSERT 都不发起（避免污染 session）。
        if await db.get(Mission, pid) is None:
            await db.rollback()
            _DAEMONS.pop(pid, None)
            deregistered.append(pid)
            logger.warning(
                "[heartbeat-sweeper] mission 已删，摘除僵尸 daemon pid=%s", pid,
            )
            continue
        try:
            await _update_heartbeat(db, pid, current_step="idle")
        except Exception:  # noqa: BLE001
            # 单次抖动：rollback 解毒后继续，不连累后续 pid。
            await db.rollback()
            logger.exception(
                "[heartbeat-sweeper] update_heartbeat 失败 pid=%s（rollback 后继续）", pid,
            )
    return deregistered


async def _heartbeat_sweeper() -> None:
    """每 HEARTBEAT_INTERVAL_SEC 给所有 _DAEMONS 项目 bump 心跳。

    B3：异常永不退出循环 —— 任意 inner 抛错 → log + 继续；
    确保 sweeper task 长期存活，不会因为单次 DB 抖动让全平台 daemon 失心跳被 reconcile 误标 error。
    """
    logger.info("[heartbeat-sweeper] 启动")
    try:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                pids = list(_DAEMONS.keys())
                if not pids:
                    continue
                async with _open_session() as db:
                    await _heartbeat_sweep_pass(db, pids)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # inner loop 任何异常都不让 task 退出
                logger.exception("[heartbeat-sweeper] inner 异常（继续）")
    except asyncio.CancelledError:
        logger.info("[heartbeat-sweeper] 收到 cancel，退出")
        raise


# ─────────────────────────── 私有：daemon 专用 session/branch ───────────────────────────
# ADR-018 mission-only · 已删 _ensure_daemon_session：daemon 直接跑在 (mission_id=mission_id, 'main')，
# 不再需要 scope='daemon' 的 session 容器行。




# ─────────────────────────── 私有：DB 写操作 ───────────────────────────
async def _get_or_create_run_state(
    db: AsyncSession, mission_id: uuid.UUID
) -> MissionRunState:
    row = await db.execute(
        select(MissionRunState).where(MissionRunState.mission_id == mission_id)
    )
    rs = row.scalar_one_or_none()
    if rs is None:
        rs = MissionRunState(mission_id=mission_id, status="stopped")
        db.add(rs)
        await db.flush()
    return rs


#: current_step 列上限（model String(255)）；防 paused_reason 等长文案溢出
_STEP_MAX = 255


def _clip_step(val):
    """把 current_step 值裁到列上限内（None 透传），防 StringDataRightTruncationError。"""
    if isinstance(val, str) and len(val) > _STEP_MAX:
        return val[: _STEP_MAX - 1] + "…"
    return val


def _is_auto_tick(trigger) -> bool:
    """是否「自动触发」的 tick（cron/interval/event 调度 + kickoff）。

    用于待审批时只跳过自动 tick；用户驱动（user_chat/manual/smoke/user_* 及 Run Once 的
    trigger=None）一律不算 auto，照常执行。
    """
    return isinstance(trigger, str) and (
        trigger.startswith(("cron", "interval", "event")) or trigger == "initial_activation"
    )


async def _set_project_runtime(
    db: AsyncSession,
    mission_id: uuid.UUID,
    *,
    status: str,
    started_at: datetime | None = None,
    stopped_at: datetime | None = None,
    last_error: str | None | bool = False,  # False = 不变；None = 清空；str = 设
    current_step: str | None | bool = False,
) -> None:
    proj = await db.get(Mission, mission_id)
    if proj is None:
        return
    proj.runtime_status = status
    rs = await _get_or_create_run_state(db, mission_id)
    rs.status = status
    if started_at is not None:
        rs.started_at = started_at
    if stopped_at is not None:
        rs.stopped_at = stopped_at
    if last_error is not False:
        rs.last_error = last_error  # type: ignore[assignment]
    if current_step is not False:
        rs.current_step = _clip_step(current_step)  # type: ignore[assignment]
    await db.commit()


async def _update_heartbeat(
    db: AsyncSession, mission_id: uuid.UUID, *, current_step: str
) -> None:
    rs = await _get_or_create_run_state(db, mission_id)
    rs.last_heartbeat_at = datetime.now(UTC)
    rs.current_step = _clip_step(current_step)
    await db.commit()


async def _mark_error(
    db: AsyncSession, mission_id: uuid.UUID, message: str
) -> None:
    await _set_project_runtime(
        db,
        mission_id,
        status="error",
        stopped_at=datetime.now(UTC),
        last_error=message,
        current_step=None,
    )


# ─────────────────────────── 公开 API ───────────────────────────
async def start(db: AsyncSession, mission_id: uuid.UUID, *, kickoff: bool = False) -> str:
    """启动 project daemon。幂等：已 running 时 no-op。

    kickoff=True 且 run_count==0（从未 tick）时，后台立即 kick off 一轮，让「创建+start」=
    立刻开始工作（不干等下次 cron）。默认 False：测试 / super_conversation 自动 start 等路径
    不重复打扰（它们另有 idle-trigger 或不需要立即 tick）。

    M1 阶段：只是注册到 _DAEMONS 表 + 把 DB status 改为 running。
    心跳由全局 sweeper 维护；M2 起 scheduler 会用此 daemon 触发 run_once。
    """
    async with _DAEMON_LOCK:
        if mission_id in _DAEMONS:
            logger.info("[daemon %s] start: 已在 running，跳过", mission_id)
            return "running"

        proj = await db.get(Mission, mission_id)
        if proj is None:
            raise ValueError(f"Mission {mission_id} 不存在")

        # draft → active：启动 daemon 等于宣告项目可用；让它在 /observe 和
        # active 列表里出现。archived 项目不自动升回 active（admin 主动 reactivate）。
        if proj.status == "draft":
            proj.status = "active"
            await db.commit()
            await db.refresh(proj)
            logger.info("[daemon %s] start 时自动 draft→active", mission_id)

        try:
            _DAEMONS[mission_id] = _DaemonEntry(started_at=datetime.now(UTC))
            await _set_project_runtime(
                db,
                mission_id,
                status="running",
                started_at=datetime.now(UTC),
                stopped_at=None,
                last_error=None,
                current_step="idle",
            )
            # 立刻打一次心跳，避免 reconcile 误判
            await _update_heartbeat(db, mission_id, current_step="idle")
            # 同步 lifecycle：start = 业务上「运行中」。修 runtime=running 但 lifecycle 仍
            # stopped 的漂移（UI 徽章 / paused 判定都看 lifecycle）。paused_* 不强转（那是等修状态）。
            try:
                if (proj.lifecycle_status or "") in ("stopped", "draft", "error", ""):
                    from app.domain.lifecycle_service import LifecycleService
                    from app.domain.lifecycle import LifecycleAction
                    await LifecycleService(db).transition(
                        mission_id, LifecycleAction.START, force=True,
                    )
            except Exception:
                logger.exception("[daemon %s] lifecycle→running 同步失败（不阻塞）", mission_id)
            logger.info("[daemon %s] 已 running", mission_id)
            # 首次激活立即 kick off 一轮（不再干等下次 cron）：仅 run_count==0（从未 tick）触发，
            # 避免 restart/admin-start 重复打扰。让「创建+start」= 立刻开始工作。
            if kickoff:
                try:
                    _rs = await _get_or_create_run_state(db, mission_id)
                    if (_rs.run_count or 0) == 0:
                        import asyncio as _asyncio
                        _spawn_bg(_initial_kickoff(mission_id), name=f"kickoff-{mission_id}")
                        logger.info("[daemon %s] 首次激活 → 安排 kickoff tick", mission_id)
                except Exception:
                    logger.exception("[daemon %s] kickoff 安排失败（不阻塞 start）", mission_id)
            return "running"
        except Exception as exc:  # noqa: BLE001
            _DAEMONS.pop(mission_id, None)
            await _mark_error(db, mission_id, f"start 失败: {exc}")
            raise


async def _initial_kickoff(mission_id: uuid.UUID) -> None:
    """首次激活后台 kick off 一轮 tick（独立 session），让新建 super 立刻开始按 soul_md 目标工作。

    lifecycle 若为 paused_*，run_once 会自行跳过，无害。
    """
    try:
        async with _open_session() as db:
            await run_once(db, mission_id, payload={
                "trigger": "initial_activation",
                "user_message": (
                    "项目刚上线。请阅读你的角色目标（soul_md）与长期记忆，规划并执行第一轮工作；"
                    "完成后用 memory_append 记录进展与 next_step。"
                ),
            })
    except Exception:
        logger.exception("[daemon %s] initial kickoff tick 失败", mission_id)


async def _drain_kickoff(mission_id: uuid.UUID) -> None:
    """drain 接力：消费 pending 队列的一轮 tick（消息 run_once 内自取，payload 不带正文）。

    收尾再查一次 pending：还有货且 lifecycle 可消费 → 链式安排下一轮（每链一环 = 一整个
    tick，天然限速；gate 的 build_incomplete 入队有 3 次上限，不会无限链）。
    ⚠️ tick **失败不 re-arm**：失败是瞬时的（如启动依赖缺失直接 raise），re-arm = 无退避
    热循环（2026-07-03 生产实证 9717 次/分钟）。失败等下次 reconcile / 用户触发。"""
    try:
        async with _open_session() as db:
            # reconcile idle-drain 场景：paused_idle 的 daemon 没被 reconcile start
            # （只恢复 lifecycle=running）→ runtime=stopped，run_once 必 raise。先拉起。
            proj = await db.get(Mission, mission_id)
            if proj is not None and proj.runtime_status != "running":
                await start(db, mission_id)
            await run_once(db, mission_id, payload={
                "trigger": "user_chat", "user_message": "",
            })
    except Exception:
        logger.exception("[daemon %s] drain tick 失败（不 re-arm，等下次触发）", mission_id)
        return
    with suppress(Exception):
        await maybe_autodrain(mission_id)


async def maybe_autodrain(mission_id: uuid.UUID) -> bool:
    """pending>0 且 lifecycle 可消费（paused_idle/running）→ 安排一轮 _drain_kickoff。

    2026-07-03 实证：「运行一次」API 是单发 tick，gate 在 tick 末 enqueue 的缺口报告
    没人消费 → 又停摆。所有「单发 tick」路径（run_once API / _drain_kickoff）收尾都该
    调本函数补上消费闭环。返回是否安排了 drain。"""
    from app.domain.tick_policy import should_drain_after_tick
    from app.services import pending_queue as _pq

    try:
        async with _open_session() as db:
            pending = await _pq.count_pending(db, mission_id)
            proj = await db.get(Mission, mission_id)
            ls = (proj.lifecycle_status if proj else "") or ""
        if should_drain_after_tick(pending_count=pending, lifecycle_status=ls):
            logger.info("[autodrain] project=%s pending=%d → 安排 drain tick", mission_id, pending)
            _spawn_bg(_drain_kickoff(mission_id), name=f"autodrain-{mission_id}")
            return True
    except Exception:
        logger.exception("[autodrain] 检查失败（不阻塞）project=%s", mission_id)
    return False


async def stop(db: AsyncSession, mission_id: uuid.UUID) -> str:
    """停止 project daemon。幂等：已 stopped 时 no-op。"""
    async with _DAEMON_LOCK:
        entry = _DAEMONS.pop(mission_id, None)
        proj = await db.get(Mission, mission_id)
        if proj is None:
            return "stopped"
        if entry is None and proj.runtime_status == "stopped":
            return "stopped"
        await _set_project_runtime(
            db,
            mission_id,
            status="stopped",
            stopped_at=datetime.now(UTC),
            current_step=None,
        )
        logger.info("[daemon %s] 已 stopped", mission_id)
        return "stopped"


async def restart(db: AsyncSession, mission_id: uuid.UUID) -> str:
    """先 stop 再 start。"""
    await stop(db, mission_id)
    return await start(db, mission_id)


async def get_runtime(
    db: AsyncSession, mission_id: uuid.UUID
) -> MissionRunState | None:
    """获取 mission_run_state（如果不存在则创建空记录）。

    强制 refresh：本函数会被 lifecycle endpoint 直接返回，必须避免 identity map
    返回 heartbeat sweeper 后台 session 未同步的旧实例。
    """
    rs = await _get_or_create_run_state(db, mission_id)
    await db.commit()
    await db.refresh(rs)
    return rs


# ──────────────────── v4 F1 · 5 段 prompt assembly ────────────────────

# R5-2 · 5 段 prompt 拼装已搬到 app/domain/daemon_prompts.py（纯函数，5 测试）
from app.domain.daemon_prompts import assemble_super_prompt as _assemble_super_prompt  # noqa: E402,F401


async def _should_skip_tick(
    db: AsyncSession, mission_id: uuid.UUID, proj: Mission, payload: dict | None
) -> tuple[str | None, dict]:
    """ADR-020 candidate 4 · 集中所有「本 tick 是否跳过」的守卫判定（只读，无副作用）。

    返回 (reason | None, detail)；run_once 据 reason 做各自的 run_state 更新/副作用。
    三类守卫：lifecycle paused / dev.max_daemon_ticks token guard / 自动 tick 遇未决审批。"""
    # V8：paused_waiting_capability
    if proj.lifecycle_status == "paused_waiting_capability":
        return "paused_waiting_capability", {"reason": proj.paused_reason or "waiting capability"}
    # ADR-025 D3 · 审批暂停不变式：有 pending 卡 → mission paused_clarification → 任何 tick 跳过，
    # 仅答卡 / 用户消息能 resolve→running 再 tick（恢复在 tick 前完成，故此处只 skip）。
    if proj.lifecycle_status == "paused_clarification":
        return "paused_clarification", {"reason": proj.paused_reason or "pending_approval"}
    # ADR-028 D4 · stopped → skip（用户显式停；调度只能由 START 拉起）。
    # 注意 paused_idle 不在此列：它是「调度拉起→跑一轮→必落」的恢复点，必须放行。
    if proj.lifecycle_status == "stopped":
        return "stopped", {"reason": "stopped"}
    # V56：dev.max_daemon_ticks token guard
    try:
        from sqlalchemy import text as _sql_text
        max_ticks = int((await db.execute(
            _sql_text("SELECT value::int FROM system_settings WHERE key='dev.max_daemon_ticks'")
        )).scalar() or 0)
    except Exception:
        logger.warning("[run_once] V56 token guard 检查失败（不阻塞）")
        max_ticks = 0
    if max_ticks > 0:
        rs_cur = await _get_or_create_run_state(db, mission_id)
        if rs_cur.run_count >= max_ticks:
            return "token_guard", {"max_ticks": max_ticks, "ticks": rs_cur.run_count}
    # 自动 tick 遇未决审批 → 跳过（initial_activation 例外，首跑要出方案卡 · ADR-013）
    _trigger = (payload or {}).get("trigger") if isinstance(payload, dict) else None
    if _is_auto_tick(_trigger) and _trigger != "initial_activation":
        from sqlalchemy import func as _sql_func
        from app.models.approvals import PendingApproval
        pending_count = (await db.execute(
            select(_sql_func.count()).select_from(PendingApproval).where(
                PendingApproval.mission_id == mission_id,
                PendingApproval.status == "pending",
            )
        )).scalar() or 0
        if pending_count > 0:
            return "pending_approval_exists", {"pending_count": pending_count}
    return None, {}


async def run_once(
    db: AsyncSession, mission_id: uuid.UUID, payload: dict | None = None
) -> dict:
    """单次 tick 的并发守卫薄包装：同一 mission 已有 tick 在跑时直接 **no-op**（返回 skipped），
    避免手动「运行一次」赶上 scheduler/kickoff、或前端重复点 → 两个 tick 并发撞库 → 500。
    想打断正在跑的 tick 请走 interrupt（停止），不是再发一个 run_once。"""
    import time as _time
    _now = _time.monotonic()
    _held = _TICKING_MISSIONS.get(mission_id)
    if _held is not None and (_now - _held) <= _TICK_LOCK_STALE_SEC:
        logger.info("[run_once] mission=%s 已有 tick 在跑 → no-op 跳过（防并发）", mission_id)
        return {"ok": True, "skipped": "tick_in_progress"}
    if _held is not None:
        logger.warning(
            "[run_once] mission=%s tick 锁已持有 %.0fs > %ds → 判定僵尸 tick，抢锁自愈",
            mission_id, _now - _held, _TICK_LOCK_STALE_SEC,
        )
    _TICKING_MISSIONS[mission_id] = _now
    try:
        return await _run_once_body(db, mission_id, payload)
    finally:
        # 只释放自己的锁（时间戳=token）：若期间被更新（TTL 抢锁），别弹掉新持有者。
        if _TICKING_MISSIONS.get(mission_id) == _now:
            _TICKING_MISSIONS.pop(mission_id, None)


async def _run_once_body(
    db: AsyncSession, mission_id: uuid.UUID, payload: dict | None = None
) -> dict:
    """v3 super 单次 tick。

    v3 模型：project.supervisor_agent_id 指向 kind='super' 的 agent；
    super 持 goal_spec / runtime_state；按 tick 用 invoke_worker 调度平台 worker；
    缺 capability 自动 request_new_capability → paused_waiting_capability。

    V8 lifecycle_status 'paused_waiting_capability' 时跳过。
    V56 dev.max_daemon_ticks token guard：达上限自动 stop。
    """
    import langchain_core.messages as _msgs

    from app.services import agent_service
    from app.skills_builtin.context import BuiltinToolContext

    proj = await db.get(Mission, mission_id)
    if proj is None:
        raise ValueError(f"Mission {mission_id} 不存在")
    # ADR-025 D1 · 派发器调度：体检扫描是跨平台确定性纯代码（无退化候选不唤起 LLM），不走 super LLM
    # tick。由 dispatcher mission 的**可见 MissionSchedule**（payload trigger=worker_health_scan）触发，
    # fan-out 退化 worker 到各 work-order。比"每 6h 空跑一轮 LLM"省 token，且调度 tab 可见可调。
    if isinstance(payload, dict) and payload.get("trigger") == "worker_health_scan":
        from app.services import worker_health_service
        res = await worker_health_service.run_health_tick(db)
        rs = await _get_or_create_run_state(db, mission_id)
        rs.run_count += 1
        rs.current_step = _clip_step(f"health_scan: {res}")
        rs.last_heartbeat_at = datetime.now(UTC)
        await db.commit()
        return {"ok": True, "health_scan": res}
    if proj.runtime_status != "running":
        raise ValueError(
            f"Mission 当前 runtime_status={proj.runtime_status}，需先 start 后 run_once"
        )
    # ADR-020 candidate 4 · tick 守卫集中到 _should_skip_tick（只读判定）；副作用按 reason 在此处理。
    _skip, _d = await _should_skip_tick(db, mission_id, proj, payload)
    if _skip in ("paused_waiting_capability", "paused_clarification", "stopped"):
        rs = await _get_or_create_run_state(db, mission_id)
        rs.run_count += 1
        rs.current_step = _clip_step(f"skip: {_skip} ({_d.get('reason')})")
        rs.last_heartbeat_at = datetime.now(UTC)
        await db.commit()
        return {"ok": True, "skipped": _skip, "reason": proj.paused_reason or _d.get("reason")}
    if _skip == "token_guard":
        # 自动 stop，防止 dev/test 烧 token (v6 · LifecycleService)
        rs_cur = await _get_or_create_run_state(db, mission_id)
        try:
            from app.domain.lifecycle_service import LifecycleService
            from app.domain.lifecycle import LifecycleAction
            await LifecycleService(db).transition(
                mission_id, LifecycleAction.STOP,
                reason=f"token_guard: ticks={rs_cur.run_count}>={_d['max_ticks']}",
                force=True,
            )
            await db.refresh(proj)
        except Exception:
            proj.runtime_status = "stopped"
            proj.lifecycle_status = "stopped"
        rs_cur.current_step = f"auto-stop: dev.max_daemon_ticks={_d['max_ticks']} reached"
        rs_cur.last_heartbeat_at = datetime.now(UTC)
        await db.commit()
        logger.warning(
            "📊 colony_v3_token_guard auto-stop project=%s ticks=%d/max=%d",
            mission_id, rs_cur.run_count, _d["max_ticks"],
        )
        return {"ok": True, "skipped": "token_guard_auto_stop", "ticks": rs_cur.run_count}
    if _skip == "pending_approval_exists":
        logger.info(
            "[daemon %s] skip: %d 个待决审批，等用户决策后再继续",
            mission_id, _d["pending_count"],
        )
        rs = await _get_or_create_run_state(db, mission_id)
        rs.run_count += 1
        rs.last_heartbeat_at = datetime.now(UTC)
        await db.commit()
        return {"ok": True, "skipped": "pending_approval_exists", "pending_count": _d["pending_count"]}

    rs = await _get_or_create_run_state(db, mission_id)
    rs.run_count += 1
    rs.current_step = "loading supervisor"
    rs.last_heartbeat_at = datetime.now(UTC)
    await db.commit()

    payload = payload or {}

    # ── v4 · F1 · pop pending user messages（用户 /btw 队列） + 5 段 prompt assembly ──
    pending_user_msgs: list[dict] = []
    try:
        from app.services import super_inbox as _sib
        pending_user_msgs = await _sib.pop_pending_messages(db, mission_id)
    except Exception:
        logger.exception("[run_once] pop_pending_messages 失败（不阻塞）")
        pending_user_msgs = []

    base_message = (
        payload.get("user_message")
        or payload.get("message")
        or "[scheduler tick] 按当前 schedule 触发一轮工作流。请检查项目长期记忆，"
           "执行下一步动作；完成后调 memory_append 记录进展。"
    )
    user_message = _assemble_super_prompt(
        base_message=base_message,
        pending_user_msgs=pending_user_msgs,
        payload=payload,
        runtime_state=rs,
        cancel_resumed=bool(payload.get("trigger") == "user_chat"),
    )

    # 装配 supervisor
    supervisor = await agent_service.get_agent(db, proj.supervisor_agent_id)
    if supervisor is None:
        rs.last_error = f"supervisor_agent_id={proj.supervisor_agent_id} 不存在"
        rs.current_step = None
        await db.commit()
        raise ValueError(rs.last_error)

    # v6.L · 不再 per-tick 创建 daemon-scope branch。super 直接读 / 写 main thread。
    # 见 docs/adr/006-v6-session-model.md：
    #   - 1 session × 1 thread；super reasoning history = 用户对话历史
    #   - per-tick metadata 走 agent_activities（已有）
    #   - artifact / tool_output 写为同一 thread 的 message + meta.kind 标识
    # ADR-018 mission-only · daemon 主流 = (mission_id=mission_id, thread_key='main')，无 session/branch 行
    # v4 · 把当前 tick task 注册到 super_inbox（让 user_chat 能 cancel）
    try:
        from app.services import super_inbox as _sib
        _sib.register_task(mission_id, asyncio.current_task())
        _cancel_event = _sib.get_cancel_event(mission_id)
    except Exception:
        _cancel_event = None
    ctx = BuiltinToolContext(
        mission_id=mission_id,
        thread_key="main",
        agent_node_name="supervisor",
        memory_scope="project",
        db_factory=_db_session.AsyncSessionLocal,
        cancel_event=_cancel_event,  # v4 · 让 invoke_worker 内部能检测
        extra={
            "acting_user_id": str(proj.created_by) if proj.created_by else None,
            "agent_id": str(proj.supervisor_agent_id),  # v3 super_dispatch 需要
        },
    )

    rs.current_step = "supervisor invoking"
    rs.last_heartbeat_at = datetime.now(UTC)
    await db.commit()

    # ADR-020 · 主线超阈值时先压缩旧消息（LLM 摘要进 ThreadAgentMemory，agent_node_name='supervisor'
    # 与 assemble_system_prompt 读回键一致）。best-effort，不阻塞 tick。压缩在 build_executor 前 →
    # 本 tick 即读到摘要 + 精简历史。（修 ADR-018 删 stream_service 时压缩被孤立的 feature regression。）
    try:
        from app.services import compression_service as _sess_c
        await _sess_c.maybe_compress_context(db, mission_id, "main", "supervisor")
    except Exception:
        logger.warning("[run_once] maybe_compress_context(main) 失败（不阻塞）", exc_info=True)

    try:
        executor = await agent_service.build_agent_executor(db, supervisor, ctx=ctx)
    except agent_service.LLMNotConfiguredError as exc:
        # ADR-017 · 未配置默认 LLM → agent 默认存在但不运行；安静跳过本 tick（非错误）。
        logger.info("[daemon %s] 跳过 tick：未配置 LLM（%s）", mission_id, exc)
        rs.last_error = "no_llm_configured"
        rs.current_step = None
        await db.commit()
        return {"ok": False, "skipped": "no_llm_configured", "run_count": rs.run_count}
    except Exception as exc:  # noqa: BLE001
        logger.exception("[daemon %s] build_agent_executor 失败", mission_id)
        rs.last_error = f"build_executor: {exc}"
        rs.current_step = None
        await db.commit()
        return {"ok": False, "error": str(exc), "run_count": rs.run_count}

    # V7.2 · daemon tick 走**流式**（下方 drive_agent_events 边跑边推 event_bus →
    # /super/{slug}/stream SSE → 前端直播气泡）。原 "ainvoke 非 streaming" 注释已过时。
    invoke_result: dict | None = None
    final_text: str | None = None
    err_msg: str | None = None
    try:
        # 用独立 session 跑 LLM，避免与 db 同事务冲突
        async with _open_session() as run_db:
            ctx2 = BuiltinToolContext(
                mission_id=mission_id,
                thread_key="main",
                agent_node_name="supervisor",
                memory_scope="project",
                db_factory=_db_session.AsyncSessionLocal,
                cancel_event=_cancel_event,  # v4
                extra={
                    "acting_user_id": str(proj.created_by) if proj.created_by else None,
                    "agent_id": str(proj.supervisor_agent_id),
                },
            )
            executor2 = await agent_service.build_agent_executor(run_db, supervisor, ctx=ctx2)
            # V7.2 · daemon 走流式：每个 LLM/tool/thinking/assistant 事件实时落 session chat
            # （append_message 自动 publish event_bus → /super/{slug}/stream 转发 → 前端直播）
            from app.services.streaming_executor import drive_agent_events
            from app.services.daemon_sink import persist_stream_piece
            from app.services import messaging_service as _sess
            from functools import partial

            _seq = 0
            _turn_id = str(uuid.uuid4())
            # recursion_limit 从 supervisor.max_iterations（super 默认 40）推；留余量 ×2，
            # 避免多轮 tool（含 request_approval 收尾）撞 LangGraph 默认 25 报 GRAPH_RECURSION。
            _reclimit = max(25, (getattr(supervisor, "max_iterations", 0) or 40) * 2)
            # ADR-028 D4 H6 · 墙钟封顶：超时 break → err_msg 仍 None、lifecycle 仍 running →
            # 走下方 paused_idle 收尾（不是 error，留待下次 cron 重拉）。
            from app.domain.tick_policy import tick_wallclock_exceeded
            _tick_started = asyncio.get_event_loop().time()
            async for piece in drive_agent_events(
                executor2,
                [_msgs.HumanMessage(content=user_message)],
                text_id=_turn_id,
                recursion_limit=_reclimit,
                cancel_event=_cancel_event,  # ADR-028 D4 · E2 · 人工门落卡 set → tool 边界即停
            ):
                if tick_wallclock_exceeded(
                    elapsed_s=asyncio.get_event_loop().time() - _tick_started,
                    cap_s=TICK_WALLCLOCK_CAP_SEC,
                ):
                    logger.warning(
                        "[daemon %s] tick 墙钟超 %ds → 收尾 paused_idle", mission_id, TICK_WALLCLOCK_CAP_SEC
                    )
                    break
                # ADR-010 UI · token 逐字直播：piece.sse（text-delta）发到 event_bus（不落库），
                # mission 页累积成「直播气泡」；正式持久消息到达时前端清掉占位。
                if piece.persist is None and piece.sse:
                    try:
                        import json as _json
                        _raw = piece.sse.split("data:", 1)[1].strip() if "data:" in piece.sse else ""
                        _ev = _json.loads(_raw) if _raw else {}
                        if _ev.get("type") == "text-delta" and _ev.get("delta"):
                            from app.services.event_bus import bus as _bus
                            # ADR-018 step 3b · channel = Mission (mission_id), not session_id
                            await _bus.publish(mission_id, {
                                "type": "token", "id": _turn_id, "delta": _ev["delta"],
                            })
                    except Exception:  # noqa: BLE001 — 直播尽力而为，不阻塞 tick
                        pass
                # 落库（自动 publish message 事件）
                if piece.persist is not None:
                    async with _open_session() as sink_db:
                        _append = partial(_sess.append_message, sink_db, mission_id, "main")
                        await persist_stream_piece(
                            lambda *, role, content, meta: _append(role=role, content=content, meta=meta),
                            piece, turn_id=_turn_id, seq=_seq,
                        )
                    _seq += 1
                    # 最终 assistant 段也记到 final_text（兼容后续 run_state / 老逻辑）
                    if piece.persist.get("kind") == "assistant":
                        final_text = piece.persist.get("text", "")
    except asyncio.CancelledError:
        # v4 · 用户 /btw 触发 cancel；记录后干净返回
        logger.info("[daemon %s] tick cancelled by user_chat", mission_id)
        err_msg = "cancelled_by_user_chat"
    except Exception as exc:  # noqa: BLE001
        logger.exception("[daemon %s] supervisor invoke 失败", mission_id)
        err_msg = f"invoke: {exc}"
    finally:
        # v4 · 清掉 super_inbox 注册（比对是本 task 的注册才 pop，防弹掉别人的）
        try:
            from app.services import super_inbox as _sib
            _sib.unregister_task(mission_id, task=asyncio.current_task())
        except Exception:
            pass

    # 更新 run_state
    rs = await _get_or_create_run_state(db, mission_id)
    rs.last_heartbeat_at = datetime.now(UTC)
    if err_msg:
        rs.last_error = err_msg
        rs.current_step = "error" if "cancelled" not in err_msg else "cancelled"
    else:
        rs.last_error = None
        rs.current_step = f"completed run_count={rs.run_count}"
    await db.commit()

    # V7.2 · final assistant 消息已在流式 sink 里实时落库（persist_stream_piece kind=assistant）
    # 不再 mirror 重复写。final_text 仅供 run_state / 老逻辑参考。

    # ADR-013 · 确定性收尾：Builder tick 正常结束后，把它本会话建的 super 项目自动收尾
    # （ensure_ready + 激活首跑 + 进入按钮），不依赖 LLM 记得调。幂等、不阻塞。
    if err_msg is None:
        try:
            from app.services.build_finalizer import maybe_finalize_after_builder_tick
            await maybe_finalize_after_builder_tick(db, mission_id)
        except Exception:  # noqa: BLE001
            logger.exception("[daemon %s] 构建确定性收尾失败（不阻塞）", mission_id)

    # ADR-028 D4 · 确定性收尾：「调度拉起→跑一轮→必落 paused」。
    # tick 正常结束 + 期间没落人工门（lifecycle 仍 running）+ 无外部 pending → 转 paused_idle。
    # 凌驾 LLM：不依赖 super 记得调，FSM 兜底保证 mission 不会「逻辑 running、物理 idle」常驻空转。
    # （若 tick 内 request_approval/request_new_capability 已转 paused_for_human，则 fresh lifecycle
    #  非 running → 跳过，不覆盖人工门；还有 pending 时让 auto-drain 接着跑同一阶段。）
    try:
        from app.domain.tick_policy import should_pause_idle_after_tick
        from app.services import super_inbox as _sib2
        _ext_pending = await _sib2.count_pending(db, mission_id)
        _fresh = await db.get(Mission, mission_id)
        _fresh_ls = (_fresh.lifecycle_status if _fresh else "") or ""
        if should_pause_idle_after_tick(
            err_msg=err_msg, lifecycle_status=_fresh_ls, external_pending=_ext_pending,
        ):
            from app.domain.lifecycle_service import LifecycleService
            from app.domain.lifecycle import LifecycleAction
            await LifecycleService(db).transition(
                mission_id, LifecycleAction.PAUSE_IDLE, reason="阶段完成",
            )
            logger.info("[daemon %s] tick 收尾 → paused_idle（阶段完成）", mission_id)
    except Exception:  # noqa: BLE001
        logger.exception("[daemon %s] D4 paused_idle 收尾失败（不阻塞）", mission_id)

    # ADR-025 D2 · work-order max-tick 封顶：跑超上限仍没收尾 → 强制软关闭(capped)，
    # 防修不动的 worker 被兜底调度无限重踢。非 work-order mission 自守卫跳过。
    if err_msg is None:
        try:
            from app.core import system_settings as _ss
            from app.services.worker_optimization_service import (
                WORK_ORDER_MAX_TICKS_DEFAULT,
                maybe_cap_work_order,
            )
            _cap = await _ss.get_int(db, "worker_opt.work_order_max_ticks",
                                     WORK_ORDER_MAX_TICKS_DEFAULT)
            await maybe_cap_work_order(db, mission_id, run_count=rs.run_count, max_ticks=_cap)
        except Exception:  # noqa: BLE001
            logger.exception("[daemon %s] work-order max-tick 检查失败（不阻塞）", mission_id)

    return {
        "ok": err_msg is None or "cancelled" in (err_msg or ""),
        "run_count": rs.run_count,
        "final_text_preview": (final_text or "")[:500],
        "error": err_msg,
        "cancelled": "cancelled" in (err_msg or ""),
    }


async def clear_memory(db: AsyncSession, mission_id: uuid.UUID) -> dict:
    """清空 project 级记忆（M3）。删除 mission_agent_memory 所有行 + 关联 s3 key。

    注意：不影响 branch_agent_memories（那个是 Orchestrator 用的）。
    """
    proj = await db.get(Mission, mission_id)
    if proj is None:
        raise ValueError(f"Mission {mission_id} 不存在")
    rows = await db.execute(
        select(MissionAgentMemory).where(MissionAgentMemory.mission_id == mission_id)
    )
    items = list(rows.scalars().all())
    s3_keys = [r.s3_key for r in items if r.s3_key]
    # M3 阶段不真正 delete s3 对象（避免误删；M4 接 daemon 真跑后再补 S3 GC）
    for r in items:
        await db.delete(r)
    await db.commit()
    return {
        "ok": True,
        "deleted": len(items),
        "skipped_s3_keys": s3_keys,
    }


# ─────────────────────────── 启动期 reconcile ───────────────────────────
async def reconcile_on_boot() -> None:
    """后端启动时调用。进程刚起、**无任何 live daemon**，按 FSM 意图恢复运行态：

    1) `lifecycle_status='running'` 的 mission（FSM 认为该运行）→ 重新拉起 daemon（resume），
       不论 runtime 残留是 running 还是上一轮 reconcile 留下的 error。免去重启/部署后人工逐个
       点「启动」。`start()` 幂等，重复调用无害。
    2) 其余 runtime 还残留 running/starting/stopping、但 lifecycle 非 running（如 paused 后进程
       被 SIGKILL）且心跳过期 → 标 error（清掉 UI 上的假 running，等人处理）。
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=STALE_HEARTBEAT_SEC)
    async with _open_session() as db:
        # 1) 恢复 FSM 认为该运行的 mission
        resume_rows = await db.execute(
            select(Mission).where(Mission.lifecycle_status == "running")
        )
        for proj in resume_rows.scalars().all():
            logger.warning(
                "[reconcile] project=%s lifecycle=running（runtime=%s）→ resume daemon",
                proj.id, proj.runtime_status,
            )
            with suppress(Exception):
                await start(db, proj.id)
            # 重启接力（2026-07-03 事故后半场）：进程死时在途的续跑 tick（in-memory）全丢，
            # 但 DB pending 队列里可能还躺着未消费消息（如审批回执）→ 不 drain 的话
            # mission 干等到下一条用户消息。发现有货 → 安排一轮 drain tick。
            with suppress(Exception):
                from app.services import pending_queue as _pq
                if await _pq.count_pending(db, proj.id) > 0:
                    logger.warning(
                        "[reconcile] project=%s 有未消费 pending 消息 → 安排 drain tick", proj.id
                    )
                    _spawn_bg(_drain_kickoff(proj.id), name=f"drain-{proj.id}")

        # 1.5) paused_idle + pending>0 也要 drain（2026-07-03 gate 停摆场景：builder 被
        # 完整性 gate 拦下后转 paused_idle，缺口报告在 pending 队列里等消费；paused_idle
        # 不在上面 lifecycle=running 恢复集里，但 drain 策略允许 paused_idle 消费）。
        idle_rows = await db.execute(
            select(Mission).where(Mission.lifecycle_status == "paused_idle")
        )
        for proj in idle_rows.scalars().all():
            with suppress(Exception):
                from app.services import pending_queue as _pq2
                if await _pq2.count_pending(db, proj.id) > 0:
                    logger.warning(
                        "[reconcile] project=%s paused_idle 且有 pending → 安排 drain tick", proj.id
                    )
                    _spawn_bg(_drain_kickoff(proj.id), name=f"drain-idle-{proj.id}")

        # 2) lifecycle 非 running、但 runtime 残留 running/starting/stopping 且心跳过期 → 标 error
        stale_rows = await db.execute(
            select(Mission, MissionRunState)
            .outerjoin(MissionRunState, Mission.id == MissionRunState.mission_id)
            .where(
                Mission.runtime_status.in_(("running", "starting", "stopping")),
                Mission.lifecycle_status != "running",
            )
        )
        items: Sequence[tuple[Mission, MissionRunState | None]] = list(stale_rows.all())  # type: ignore[arg-type]
        for proj, rs in items:
            # tz 安全：DB 读回的时间戳应带 tz（Postgres timestamptz）；万一是 naive（某些后端/测试）
            # 按 UTC 处理，避免 offset-naive vs aware 比较直接崩。
            hb = rs.last_heartbeat_at if rs else None
            if hb is not None and hb.tzinfo is None:
                hb = hb.replace(tzinfo=UTC)
            if rs is None or hb is None or hb < cutoff:
                logger.warning(
                    "[reconcile] project=%s lifecycle=%s runtime=%s 心跳过期 → 标 error",
                    proj.id, proj.lifecycle_status, proj.runtime_status,
                )
                with suppress(Exception):
                    await _mark_error(
                        db, proj.id, "上次进程未优雅退出（reconcile 检测到 stale heartbeat）"
                    )


# ─────────────────────────── Sweeper lifecycle ───────────────────────────
def start_heartbeat_sweeper() -> None:
    """在 FastAPI lifespan startup 调用，避免测试 import 时就 spawn。"""
    global _HEARTBEAT_SWEEPER_TASK
    if _HEARTBEAT_SWEEPER_TASK is None or _HEARTBEAT_SWEEPER_TASK.done():
        _HEARTBEAT_SWEEPER_TASK = asyncio.create_task(
            _heartbeat_sweeper(), name="project-heartbeat-sweeper"
        )


async def stop_heartbeat_sweeper() -> None:
    global _HEARTBEAT_SWEEPER_TASK
    if _HEARTBEAT_SWEEPER_TASK is None or _HEARTBEAT_SWEEPER_TASK.done():
        return
    _HEARTBEAT_SWEEPER_TASK.cancel()
    with suppress(asyncio.CancelledError):
        await _HEARTBEAT_SWEEPER_TASK
    _HEARTBEAT_SWEEPER_TASK = None


# ─────────────────────────── 关停 (供 shutdown hook) ───────────────────────────
async def shutdown_all() -> None:
    """后端 graceful shutdown 时调用；把所有 running daemon 标 stopped。"""
    await stop_heartbeat_sweeper()
    pids = list(_DAEMONS.keys())
    if not pids:
        return
    logger.info("[shutdown] 优雅停止 %d 个 daemon", len(pids))
    async with _open_session() as db:
        for pid in pids:
            with suppress(Exception):
                await stop(db, pid)
