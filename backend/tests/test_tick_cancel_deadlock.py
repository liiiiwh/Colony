"""2026-07-03 · tick 自 cancel 死锁 + 注册表 clobbering + 僵尸锁 · 真实事故回归。

事故（mission-cc129f，审批后挂死 12+ 分钟直至重启）：
1. LangGraph 把 tool 跑在**子 task**里 → request_approval 内部调 cancel_current_tick 时
   `task is current_task()` 自检失效（注册的是外层 tick task）→ 走 wait_for(shield(tick), 10s)
   —— tool 等 tick 结束、tick 等 tool 返回 = **互相死锁**。
2. 10s 超时 → task.cancel() 沿 LangGraph ~987 层嵌套 gather 递归 → RecursionError，
   cancel 半途炸掉；超时回调自己也炸 → tool 的 wait_for 永远等不到 → tick 僵尸。
3. 僵尸永久持有 run_once 并发锁 → mission 挂死。
4. 帮凶：_trigger_tick_async 每次 respawn register_task 覆盖注册表并**清 cancel_event**
   （1s 一次，协作取消信号被抹）；unregister_task 无条件 pop（弹掉真 tick 的注册）。

修复合同：
- tick 内人工门（_pause_for_pending / readiness）只 **signal_cancel**（set 事件），绝不
  await/cancel task；E2 checkpoint 在 on_tool_end break 收尾。硬 cancel 仅留给外部端点。
- respawn/skip task 不注册不清事件；unregister 比对是自己的注册才 pop。
- cancel_current_tick 外部强 cancel 撞 RecursionError 不炸出去。
- run_once 锁带时间戳，超过墙钟上限+余量判僵尸 → 抢锁自愈。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _mk_running_mission(db) -> uuid.UUID:
    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.io",
             hashed_password="x")
    db.add(u)
    await db.flush()
    ag = Agent(name=f"sup-{uuid.uuid4().hex[:6]}", category="custom", kind="super",
               model_id=None, soul_md="x", protocol_md="x")
    db.add(ag)
    await db.flush()
    proj = Mission(name="m", slug=f"m-{uuid.uuid4().hex[:8]}",
                   supervisor_agent_id=ag.id, created_by=u.id,
                   lifecycle_status="running", runtime_status="running")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj.id


# ── 1 · tick 内人工门 signal-only，绝不 await/cancel tick task ──────────────────

async def test_pause_for_pending_signals_without_awaiting_tick(
    db_session, _patched_session_local,
):
    """场景还原：注册表里是「正在跑的 tick」（长活 task），_pause_for_pending 从别的
    task（langgraph tool task）里被调。老代码 wait_for(shield(tick), 10s) 死锁等待；
    新语义：立即返回（<2s），只 set cancel_event，tick task 毫发无损。"""
    from app.services import tick_lifecycle as tl

    mid = await _mk_running_mission(db_session)

    async def _long_tick():
        await asyncio.sleep(30)

    tick_task = asyncio.ensure_future(_long_tick())
    await asyncio.sleep(0)
    tl.register_task(mid, tick_task)
    try:
        from app.services import pending_approval_service as pas

        t0 = asyncio.get_event_loop().time()
        await asyncio.wait_for(pas._pause_for_pending(db_session, mid), timeout=5.0)
        elapsed = asyncio.get_event_loop().time() - t0

        assert elapsed < 2.0, f"落卡不该等 tick（等了 {elapsed:.1f}s = 旧死锁行为）"
        assert tl.get_cancel_event(mid).is_set(), "应 set 协作取消信号（E2 收尾用）"
        assert not tick_task.done() and not tick_task.cancelled(), \
            "tick task 不该被硬 cancel（深 gather 链会 RecursionError 变僵尸）"

        db_session.expire_all()
        proj = await db_session.get(Mission, mid)
        assert proj.lifecycle_status == "paused_clarification"
    finally:
        tick_task.cancel()
        tl.unregister_task(mid)


def test_readiness_pause_does_not_hard_cancel():
    """readiness 的人工门同样只 signal（源码守卫：不再引用 cancel_current_tick）。"""
    import inspect

    from app.services import readiness

    src = inspect.getsource(readiness)
    assert "cancel_current_tick" not in src, \
        "readiness 落卡在 tick 内执行，硬 cancel 自己会死锁（改用 signal_cancel）"
    assert "signal_cancel" in src


# ── 2 · respawn/skip task 不 clobber 注册表、不清 cancel_event ─────────────────

async def test_skip_respawn_keeps_registry_and_cancel_event(
    db_session, _patched_session_local, monkeypatch,
):
    """真 tick 注册在案 + cancel_event 已 set（人工门信号）。此时 1s 退避轮询的 skip
    respawn 不得覆盖注册、不得清信号（老代码 register_task 每秒抹一次信号）。"""
    from app.api import super_conversation as sc
    from app.services import tick_lifecycle as tl

    mid = await _mk_running_mission(db_session)

    async def _long_tick():
        await asyncio.sleep(30)

    real_tick = asyncio.ensure_future(_long_tick())
    await asyncio.sleep(0)
    tl.register_task(mid, real_tick)
    tl.get_cancel_event(mid).set()  # 人工门信号在案

    async def _skip_run_once(db, mission_id, payload=None):
        return {"ok": True, "skipped": "tick_in_progress"}

    from app.services import mission_daemon
    monkeypatch.setattr(mission_daemon, "run_once", _skip_run_once)
    monkeypatch.setattr(sc, "_SKIP_RESPAWN_BACKOFF_SEC", 0.01, raising=False)
    from app.domain import tick_policy
    monkeypatch.setattr(tick_policy, "should_drain_after_tick", lambda **kw: False)

    try:
        await sc._trigger_tick_async(mid, None)
        await asyncio.sleep(0.3)  # 等 _run 完整跑完 finally

        assert tl._RUNNING_TICKS.get(mid) is real_tick, \
            "skip respawn 不得覆盖/弹掉真 tick 的注册"
        assert tl.get_cancel_event(mid).is_set(), \
            "skip respawn 不得清掉人工门的协作取消信号"
    finally:
        real_tick.cancel()
        tl.unregister_task(mid)
        tl.get_cancel_event(mid).clear()


async def test_unregister_only_pops_own_task():
    """unregister_task(mid, task=X)：注册表里不是 X → 不动；是 X → pop。"""
    from app.services import tick_lifecycle as tl

    mid = uuid.uuid4()

    async def _sleep():
        await asyncio.sleep(10)

    a = asyncio.ensure_future(_sleep())
    b = asyncio.ensure_future(_sleep())
    try:
        tl.register_task(mid, a)
        tl.unregister_task(mid, task=b)
        assert tl._RUNNING_TICKS.get(mid) is a, "别人的 unregister 不得弹掉 a 的注册"
        tl.unregister_task(mid, task=a)
        assert tl._RUNNING_TICKS.get(mid) is None
    finally:
        a.cancel()
        b.cancel()


# ── 3 · 外部强 cancel 撞深 gather 链不炸 ───────────────────────────────────────

async def test_force_cancel_survives_deep_gather_chain():
    """复刻事故：~1200 层嵌套 gather（LangGraph astream_events 真实形态），
    task.cancel() 递归爆 RecursionError。cancel_current_tick 必须兜住，
    返回结构化结果而不是把 RecursionError 抛给调用方。"""
    from app.services import tick_lifecycle as tl

    mid = uuid.uuid4()
    leaf_gate = asyncio.Event()

    async def _leaf():
        await leaf_gate.wait()

    leaf = asyncio.ensure_future(_leaf())
    fut = leaf
    for _ in range(1200):
        fut = asyncio.gather(fut)

    async def _tick():
        await fut

    tick = asyncio.ensure_future(_tick())
    await asyncio.sleep(0)
    tl.register_task(mid, tick)
    try:
        res = await tl.cancel_current_tick(mid, timeout_seconds=0.1)
        assert isinstance(res, dict), "RecursionError 不得抛出（事故里它炸掉了超时回调）"
        assert res.get("stage") == "forced_failed_recursion", res
    finally:
        # 清理：放行 leaf → 完成沿链自底向上传播（cancel 穿链会再爆 RecursionError）
        leaf_gate.set()
        await asyncio.wait_for(tick, timeout=5.0)
        tl.unregister_task(mid)


# ── 3.5 · 重启接力：reconcile 时 pending 队列有货 → 安排 drain tick ────────────

async def test_reconcile_on_boot_drains_pending_queue(
    db_session, _patched_session_local, monkeypatch,
):
    """事故后半场：进程重启把在途续跑 tick（in-memory）全丢，但 DB pending 队列里还躺着
    未消费的审批回执 → 老代码 reconcile 只 resume daemon 不 drain，mission 干等到天荒地老。
    新语义：resume 后发现 pending>0 → 自动安排一轮 drain tick。"""
    from app.services import mission_daemon as md
    from app.services import pending_queue as pq

    mid = await _mk_running_mission(db_session)

    # super_pending_messages 是迁移建的裸表（SQLite 测试库没有）→ mock 队列有 1 条未消费
    async def _one_pending(db, mission_id):
        return 1 if mission_id == mid else 0

    ticked: list[uuid.UUID] = []

    async def _spy_run_once(db, mission_id, payload=None):
        ticked.append(mission_id)
        return {"ok": True}

    monkeypatch.setattr(md, "run_once", _spy_run_once)
    monkeypatch.setattr(pq, "count_pending", _one_pending)

    async def _noop_start(db, mission_id, **kw):
        return "running"

    monkeypatch.setattr(md, "start", _noop_start)

    await md.reconcile_on_boot()
    await asyncio.sleep(0.1)  # drain tick 是后台 task

    assert mid in ticked, "重启后 pending 队列有未消费消息 → 必须安排 drain tick 接力"


# ── 3.6 · fire-and-forget task 必须持强引用（GC 静默吞任务）────────────────────

async def test_spawn_bg_keeps_strong_reference():
    """2026-07-03 实证：reconcile 的 create_task(_drain_kickoff) 无强引用，tick 任务
    中途被 GC 静默吞掉（09:33:39 后 finalize/autodrain 全没跑、无任何日志/异常）。
    Python 文档明示要保存 create_task 返回值。_spawn_bg：入 registry 持引用，done 后自清。"""
    from app.services import mission_daemon as md

    ran = {"ok": False}

    async def _work():
        await asyncio.sleep(0.05)
        ran["ok"] = True

    t = md._spawn_bg(_work(), name="test-bg")
    assert t in md._BG_TASKS, "运行中必须持强引用（防 GC）"
    await t
    await asyncio.sleep(0)
    assert ran["ok"] is True
    assert t not in md._BG_TASKS, "完成后应自动从 registry 清掉（防泄漏）"


# ── 4 · run_once 僵尸锁 TTL 自愈 ───────────────────────────────────────────────

async def test_stale_tick_lock_is_stolen(db_session, monkeypatch):
    """锁持有超过墙钟上限+余量 = 僵尸（事故里 finally 永远不跑）→ run_once 抢锁自愈。"""
    import time as _time

    from app.services import mission_daemon as md

    mid = uuid.uuid4()
    ran = {"n": 0}

    async def _body(db, mission_id, payload=None):
        ran["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(md, "_run_once_body", _body)

    # 新鲜锁 → skip
    md._TICKING_MISSIONS[mid] = _time.monotonic()
    res = await md.run_once(db_session, mid)
    assert res.get("skipped") == "tick_in_progress" and ran["n"] == 0

    # 僵尸锁（持有超过 stale 阈值）→ 抢锁，正常跑
    md._TICKING_MISSIONS[mid] = _time.monotonic() - md._TICK_LOCK_STALE_SEC - 1
    res = await md.run_once(db_session, mid)
    assert ran["n"] == 1 and res.get("ok") is True
    assert mid not in md._TICKING_MISSIONS, "跑完应释放锁"
