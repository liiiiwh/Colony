"""Colony Worker Optimization · ADR-018 D2.

The platform's single, non-deletable system super that owns the **worker** iteration loop —
the symmetric counterpart to Builder owning the **super** loop. Workers are shared across every
super, so their optimization is centralized here (not under any one Builder mission):

  - one fixed auto-running mission (no extra missions, no copies, undeletable);
  - two inputs — ① the periodic health self-check (reads worker_invocation_log for degraded
    candidates, the 6h tick that used to live under Builder), and ② worker-issue reports any
    super raises via report_worker_issue;
  - conservative gate-first (ADR-015 L2): only reversible protocol tweaks, applied only when the
    cross-caller compatibility gate passes; degraded-but-ambiguous candidates are observed/
    reported, never force-changed. Irreversible moves escalate to a human (force_human=True).

This module is the deep seam: `ensure_worker_optimization_super(db)` idempotently seeds and
returns the (Agent, Mission) singleton; callers treat it as an opaque platform anchor.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User

logger = logging.getLogger(__name__)

WORKER_OPT_AGENT_NAME = "Colony Worker Optimization"
WORKER_OPT_SLUG = "colony-worker-optimization"
WORKER_OPT_MISSION_NAME = "Colony Worker Optimization"

_SOUL_MD = """\
你是 **Colony Worker Optimization** —— 平台唯一的 worker 迭代守护 super（系统对象，不可删/复制，
固定一个自动运行的 mission）。worker 被所有 super 共享，所以它们的"优化"集中归你一处，不挂任何
Builder mission。Builder 管 super 的创建与迭代；你管 worker 的**优化**（worker 的创建仍由 Builder 在
建 super 时做——创建与优化分家）。

你的天性是**保守**：worker 是全局契约，一次坏改会同时打穿很多调用方。你宁可少改、可逆地改、
用证据说话，也不做投机式重写。
"""

_PROTOCOL_MD = """\
## 输入（两条）
1. **定期健康自检**：系统按节律给你一份「确定性体检报告」（从 worker_invocation_log 聚合的退化候选：
   成功率、样本量、高频错误）。
2. **super 上报**：任一 super 任一 mission 通过 report_worker_issue 把"某 worker 坏了"的证据发给你。

## 处理铁律（保守门控优先 · ADR-015 L2）
- **只做可逆的 protocol 优化**：走 agent_protocol_propose → apply → evaluate。apply 会自动过
  **跨调用方兼容门**（compat_gate）：任一现有调用方成功率回退超阈值 → 自动回滚。门没过就不算改。
- **样本不足或归因不清** → 不改：只观察 / 记录 / 必要时回上报，绝不凭单条失败就动 worker。
- **不可逆动作**（删/改 action 语义、加 tool、动高危能力）→ 不要自行 apply，改走
  request_approval(force_human=True) 升级真人。
- 本 mission 默认 AUTO（无人盯审批卡）：request_approval 不带 force_human 会自动通过；所以**只有
  真正不可逆**的动作才升级，且**必须显式 force_human=True**才会真正停下等真人。

## 一次处理的节奏（work-order：一个 mission 盯一个 worker）
每个 mission 只针对**一个 worker** 的优化，全自动跑到完成：
- 看 telemetry 的 per_super / per_action 失败分布定位根因 → 判断是否可逆可改 →
  能改则 propose+apply（过门即生效，破坏即回滚）→ evaluate 记录效果；不能改则观察/记录。
- **每轮结束必须二选一收口**：
  - 还没弄完（要多步诊断/等 evaluate） → 调 `optimization_continue` 入队续跑（下一 tick 继续）。
  - 已修好 / 判定归因外部不可修 → 调 `optimization_done(summary, outcome)`（outcome='fixed' 或
    'not_fixable'）：软关闭本 mission + 自动唤醒所有等这个 worker 的 super。
- 漏调 continue 会被兜底调度补踢、跑太久会被 max-tick 强制收尾——但别依赖兜底，正常都要主动收口。
- 不可逆动作（删/改 action 语义、加 tool、动高危能力）→ request_approval(force_human=True) 升级真人
  （会暂停本 mission 等人，绝不自行 apply）。
"""


async def _platform_admin(db: AsyncSession) -> User | None:
    return (
        await db.execute(select(User).where(User.username == "admin").limit(1))
    ).scalar_one_or_none()


async def ensure_worker_optimization_super(
    db: AsyncSession,
) -> tuple[Agent, Mission] | None:
    """Idempotently seed and return the (Agent, Mission) singleton. None if admin not ready.

    Safe to call on every boot: existing rows are re-asserted as system/non-deletable, not
    duplicated. model_id stays NULL — the agent resolves the platform default at runtime
    (ADR-017), so it can be seeded before any LLM is configured."""
    agent = (
        await db.execute(select(Agent).where(Agent.name == WORKER_OPT_AGENT_NAME).limit(1))
    ).scalar_one_or_none()
    if agent is None:
        # Via create_agent (not raw Agent(...)): it auto-binds the super-scoped skill set
        # (list_workers, agent_protocol_propose/apply/evaluate, request_approval, telemetry …)
        # so the worker-opt super can actually run the L2 optimization loop. model_id=None →
        # resolves the platform default at runtime (ADR-017).
        from app.schemas.agent import AgentCreate
        from app.services import agent_service
        from app.db.system_agent_prompts import soul_for
        from app.domain.onboarding.seed_language import get_seed_language

        _lang = await get_seed_language(db)
        agent = await agent_service.create_agent(db, AgentCreate(
            name=WORKER_OPT_AGENT_NAME,
            description="平台 worker 迭代守护 super（系统对象，不可删；保守门控优先）。",
            category="utility",
            kind="super",
            model_id=None,
            soul_md=soul_for(WORKER_OPT_AGENT_NAME, _lang) or _SOUL_MD,
            protocol_md=_PROTOCOL_MD,
        ))
        agent.is_system = True  # ADR-018 D2 · 不可删/复制
        await db.flush()
        logger.info("[worker_opt] seeded system super agent=%s", agent.id)
    else:
        # re-assert system invariants (don't clobber an admin-tuned soul/protocol)
        agent.is_system = True
        agent.kind = "super"

    # super 工具集幂等补齐：is_system agent 豁免 reconcile 回填（2026-07 决议），
    # 它的技能集由这里 seed 专管——存量 agent 缺绑（如脏绑定清理误清）也能自愈。
    from app.models.agent import AgentSkill
    from app.models.skill import Skill

    scope_skills = (await db.execute(
        select(Skill).where(
            Skill.is_builtin.is_(True),
            Skill.is_enabled.is_(True),
            Skill.scope.in_(("super", "all")),
        )
    )).scalars().all()
    bound_ids = set((await db.execute(
        select(AgentSkill.skill_id).where(AgentSkill.agent_id == agent.id)
    )).scalars().all())
    added = 0
    for sk in scope_skills:
        if sk.id not in bound_ids:
            db.add(AgentSkill(agent_id=agent.id, skill_id=sk.id, config={}))
            added += 1
    if added:
        await db.flush()
        logger.info("[worker_opt] 补齐 super 工具集 %d 个 skill", added)

    admin = await _platform_admin(db)
    if admin is None:
        logger.warning("[worker_opt] admin user 未就绪，跳过 mission seed")
        return None

    mission = (
        await db.execute(select(Mission).where(Mission.slug == WORKER_OPT_SLUG).limit(1))
    ).scalar_one_or_none()
    if mission is None:
        mission = Mission(
            name=WORKER_OPT_MISSION_NAME,
            slug=WORKER_OPT_SLUG,
            description="Colony 平台 worker 健康自检 + 优化的固定自动 mission（系统对象，不可删/增）。",
            status="active",
            supervisor_agent_id=agent.id,
            auto_approve=True,  # 系统 AUTO：无人盯卡片；保守门走 L2 兼容门兜底
            is_system=True,
            workflow_config={},
            created_by=admin.id,
        )
        db.add(mission)
        await db.flush()
        logger.info("[worker_opt] seeded fixed mission project=%s", mission.id)
    else:
        mission.supervisor_agent_id = agent.id
        mission.is_system = True
        if mission.status != "active":
            mission.status = "active"

    await db.commit()
    await db.refresh(agent)
    await db.refresh(mission)

    # ADR-025 D1（严格 B-1）· 给 dispatcher mission 挂 6h 可见 MissionSchedule（调度 tab 可见可调，
    # 取代平台 cron sys-worker-health）。payload trigger=worker_health_scan → run_once 走确定性扫描。
    await _ensure_dispatcher_schedule(db, mission.id, admin.id)

    # standing 自动运行：把 lifecycle_status 置 running（FSM「该运行」意图，reconcile_on_boot
    # 据此在重启后自动续跑）+ 拉起 daemon。健康自检靠上面的 6h schedule 触发，所以 kickoff=False
    # （不需要建好立刻 tick）。best-effort：daemon 起不来也绝不能崩 startup。
    try:
        from app.domain.lifecycle import LifecycleAction
        from app.domain.lifecycle_service import LifecycleService

        if mission.lifecycle_status != "running":
            await LifecycleService(db).transition(
                mission.id, LifecycleAction.START, force=True,
            )
        from app.services import mission_daemon
        await mission_daemon.start(db, mission.id, kickoff=False)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[worker_opt] dispatcher mission 自动续跑启动失败（不阻塞 startup）mission=%s",
            mission.id,
        )
    return agent, mission


WORKER_OPT_SCAN_INTERVAL = "6h"


async def _ensure_dispatcher_schedule(
    db: AsyncSession, mission_id: uuid.UUID, created_by: uuid.UUID
) -> None:
    """给体检派发器 mission 挂一条可见的 6h 体检 MissionSchedule（已有则不重挂）。"""
    from app.models.mission import MissionSchedule

    existing = (await db.execute(
        select(MissionSchedule).where(
            MissionSchedule.mission_id == mission_id,
            MissionSchedule.kind == "interval",
        )
    )).scalars().first()
    if existing is not None:
        return
    sched = MissionSchedule(
        mission_id=mission_id, name="平台 worker 健康自检（每 6h）", kind="interval",
        expr=WORKER_OPT_SCAN_INTERVAL, enabled=True,
        payload_template={"trigger": "worker_health_scan"}, created_by=created_by,
    )
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    try:
        from app.services import scheduler_service
        scheduler_service.reschedule_one(sched)
    except Exception:  # noqa: BLE001
        logger.exception("[worker_opt] dispatcher 6h 调度注册失败（不阻塞）mission=%s", mission_id)


def _wo_slug_prefix(capability: str) -> str:
    """work-order mission slug 前缀（capability 经 slug 化）。"""
    import re

    # 下划线也要规整成连字符：mission slug 走 url-safe 规则 `^[a-z0-9][a-z0-9-]*$`，
    # 留下划线会让 MissionPublic 读模型校验失败、连累 /api/missions/all 整表 500。
    safe = re.sub(r"[^a-z0-9-]+", "-", (capability or "unknown").lower()).strip("-")
    return f"wo-{safe or 'unknown'}"


async def spawn_or_attach_work_order(
    db: AsyncSession,
    *,
    super_agent_id: uuid.UUID,
    created_by: uuid.UUID,
    capability: str,
    worker_agent_id: str,
    report: str,
) -> tuple[Mission, bool]:
    """ADR-025 D1 · 给一个退化 worker 取得它的 work-order mission，并把报告投进 main 线程。

    同 worker(capability) 至多一个**未归档**的 work-order（worker protocol 全局共享，并发改必打架）：
    - 已有未归档 → 复用（attach），返回 (mission, False)
    - 没有 → 新建独立 ephemeral mission（supervisor=worker-opt super，全自动 auto_approve），
      返回 (mission, True)
    跨不同 capability → 各自独立 mission（天然并行，run_once 不持全局锁）。

    报告写入后由 S4 的续跑机制驱动它跑到完成→软关闭。"""
    from app.services import messaging_service

    prefix = _wo_slug_prefix(capability)
    existing = (await db.execute(
        select(Mission).where(
            Mission.supervisor_agent_id == super_agent_id,
            Mission.slug.like(f"{prefix}-%"),
            Mission.status != "archived",
        ).order_by(Mission.created_at.asc())
    )).scalars().first()

    created = existing is None
    if existing is not None:
        mission = existing
    else:
        mission = Mission(
            name=f"Worker 优化 · {capability}",
            slug=f"{prefix}-{uuid.uuid4().hex[:8]}",
            description=f"worker capability={capability} 的一次优化（ephemeral，完成即软关闭）。",
            status="active",
            supervisor_agent_id=super_agent_id,
            auto_approve=True,  # 全自动：无人盯卡；保守门走 L2 兼容门兜底（ADR-025 D2）
            workflow_config={"work_order": {"capability": capability,
                                            "worker_agent_id": worker_agent_id}},
            created_by=created_by,
        )
        db.add(mission)
        await db.commit()
        await db.refresh(mission)
        logger.info("[worker_opt] spawned work-order mission=%s cap=%s", mission.id, capability)

    await messaging_service.append_message(
        db, mission.id, "main", role="user", content=report,
        meta={"type": "worker_issue_report", "capability": capability,
              "worker_agent_id": worker_agent_id},
    )
    # ADR-025 fix · daemon tick 的 LLM 上下文不加载 thread 历史，只读 pending_queue → 报告必须
    # **入队**，否则 work-order super 的 kickoff tick 看不到退化报告、无法优化（F7 e2e 实测命中）。
    try:
        from app.services.pending_queue import enqueue_user_message

        await enqueue_user_message(
            db, mission.id, super_agent_id, content=report,
            meta={"type": "worker_issue_report", "capability": capability,
                  "worker_agent_id": worker_agent_id},
        )
    except Exception:  # noqa: BLE001
        logger.exception("[worker_opt] 报告入队失败（不阻塞）mission=%s", mission.id)
    return mission, created


def _work_order_capability(mission: Mission) -> str:
    """从 work-order mission 取它盯的 capability（spawn 时写进 workflow_config）。"""
    wo = (mission.workflow_config or {}).get("work_order") or {}
    return wo.get("capability") or ""


async def close_work_order(
    db: AsyncSession, mission_id: uuid.UUID, *, outcome: str = "fixed"
) -> dict:
    """ADR-025 D2 · 软关闭一个 work-order mission：STOP + status=archived + 注销调度 +
    按 capability 唤醒所有 worker_issue:<cap> 等待者。outcome ∈ fixed / not_fixable / capped。

    软关闭（非 hard-delete）保审计；先唤醒等待者再归档（唤醒需读 mission 记录）。"""
    mission = await db.get(Mission, mission_id)
    if mission is None:
        return {"ok": False, "error": "mission 不存在"}
    capability = _work_order_capability(mission)
    if not capability:
        # 自守卫：技能按 kind 绑给所有 super，但只能关 work-order（防普通 super 误关自己 mission）
        return {"ok": False, "error": "not_a_work_order"}

    # 停 lifecycle（force：兼容 running/paused 各种当前态）
    try:
        from app.domain.lifecycle import LifecycleAction
        from app.domain.lifecycle_service import LifecycleService

        await LifecycleService(db).transition(
            mission_id, LifecycleAction.STOP, force=True,
            reason=f"work_order_done:{outcome}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("[worker_opt] close work-order STOP 失败（继续归档）mission=%s", mission_id)

    # 软归档 + 注销调度（兜底调度对 archived/stopped 自然失效，这里显式清 job 防积累）
    mission.status = "archived"
    await _drop_mission_schedules(db, mission_id)
    await db.commit()

    woken = 0
    if outcome in ("fixed", "not_fixable", "capped") and capability:
        from app.services.worker_health_service import resume_waiters_for_capability

        woken = await resume_waiters_for_capability(db, capability)
    logger.info("[worker_opt] closed work-order mission=%s outcome=%s woke=%d",
                mission_id, outcome, woken)
    return {"ok": True, "outcome": outcome, "capability": capability, "woke_waiters": woken}


async def _drop_mission_schedules(db: AsyncSession, mission_id: uuid.UUID) -> None:
    """删某 mission 的所有 MissionSchedule 行 + 尽力注销 APScheduler job（best-effort）。"""
    from app.models.mission import MissionSchedule

    rows = (await db.execute(
        select(MissionSchedule).where(MissionSchedule.mission_id == mission_id)
    )).scalars().all()
    for sched in rows:
        try:
            from app.services import scheduler_service
            scheduler_service.delete_one(sched.id)
        except Exception:  # noqa: BLE001
            pass
        await db.delete(sched)


WORK_ORDER_MAX_TICKS_DEFAULT = 12
WORK_ORDER_BACKSTOP_INTERVAL = "5m"  # 兜底续跑间隔：LLM 漏调 optimization_continue 时补踢


async def _ensure_backstop_schedule(
    db: AsyncSession, mission_id: uuid.UUID, created_by: uuid.UUID
) -> None:
    """ADR-025 D2 · 给 work-order mission 挂一条兜底 interval schedule（已有则不重挂）。

    lifecycle 天然做闸门：running 未完成→补踢续跑；archived→no-op；paused_clarification→被守卫跳过。
    close_work_order 会注销它。"""
    from app.models.mission import MissionSchedule

    existing = (await db.execute(
        select(MissionSchedule).where(MissionSchedule.mission_id == mission_id)
    )).scalars().first()
    if existing is not None:
        return
    sched = MissionSchedule(
        mission_id=mission_id, name="work-order 兜底续跑", kind="interval",
        expr=WORK_ORDER_BACKSTOP_INTERVAL, enabled=True,
        payload_template={"trigger": "work_order_backstop"}, created_by=created_by,
    )
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    try:
        from app.services import scheduler_service
        scheduler_service.reschedule_one(sched)
    except Exception:  # noqa: BLE001
        logger.exception("[worker_opt] 兜底调度注册失败（不阻塞）mission=%s", mission_id)


async def activate_work_order(
    db: AsyncSession, mission_id: uuid.UUID, created_by: uuid.UUID
) -> None:
    """启动 work-order：daemon start(kickoff 立即首跑) + 挂兜底续跑调度。幂等、best-effort。"""
    try:
        from app.services import mission_daemon
        await mission_daemon.start(db, mission_id, kickoff=True)
    except Exception:  # noqa: BLE001
        logger.exception("[worker_opt] work-order daemon start 失败（不阻塞）mission=%s", mission_id)
    try:
        await _ensure_backstop_schedule(db, mission_id, created_by)
    except Exception:  # noqa: BLE001
        logger.exception("[worker_opt] work-order 兜底调度失败（不阻塞）mission=%s", mission_id)


async def maybe_cap_work_order(
    db: AsyncSession, mission_id: uuid.UUID, *, run_count: int, max_ticks: int
) -> bool:
    """ADR-025 D2 · max-tick 封顶：work-order 跑超 max_ticks 仍没收尾 → 强制软关闭(capped)，
    防修不动的 worker 被兜底调度无限重踢烧 token。返回是否已封顶关闭。"""
    if max_ticks <= 0 or run_count < max_ticks:
        return False
    mission = await db.get(Mission, mission_id)
    if mission is None or not _work_order_capability(mission) or mission.status == "archived":
        return False
    await close_work_order(db, mission_id, outcome="capped")
    logger.info("[worker_opt] work-order mission=%s 到 max-tick=%d，强制收尾", mission_id, max_ticks)
    return True


async def enqueue_continue(db: AsyncSession, mission_id: uuid.UUID) -> dict:
    """ADR-025 D2 · LLM 自驱续跑：入队一条续跑指令，daemon 下一 tick 取走继续优化。

    守卫：有任何未决审批卡（auto 模式下=force_human 真人门）→ 拒绝入队，不得越过人工门。"""
    from app.models.approvals import PendingApproval
    from sqlalchemy import func as _func

    pending = (await db.execute(
        select(_func.count()).select_from(PendingApproval).where(
            PendingApproval.mission_id == mission_id,
            PendingApproval.status == "pending",
        )
    )).scalar() or 0
    if pending > 0:
        return {"enqueued": False, "reason": "pending_human_approval"}

    mission = await db.get(Mission, mission_id)
    if mission is None or mission.supervisor_agent_id is None:
        return {"enqueued": False, "reason": "mission_missing"}
    if not _work_order_capability(mission):
        return {"enqueued": False, "reason": "not_a_work_order"}

    from app.services.pending_queue import enqueue_user_message

    await enqueue_user_message(
        db, mission_id, mission.supervisor_agent_id,
        content="[续跑] 当前 worker 优化尚未完成，请继续逐步推进；完成后调 optimization_done。",
        meta={"type": "optimization_continue"},
    )
    return {"enqueued": True}
