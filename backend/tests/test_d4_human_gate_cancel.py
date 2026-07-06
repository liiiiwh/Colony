"""ADR-028 D4 · H1 · 人工门落卡时硬停当前 tick。

落 paused_clarification（_pause_for_pending）时调 super_inbox.cancel_current_tick，
取消正在跑的 tick（E2 cooperative cancel 的触发点），避免「再蹦几个」。
"""
from __future__ import annotations

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


@pytest.mark.parametrize("lifecycle", [
    "paused_clarification", "paused_waiting_capability",
])
async def test_resume_after_clarification_handles_both_paused_for_human(
    db_session, _patched_session_local, lifecycle
):
    """ADR-028 D4 · H2 · 决卡恢复统一覆盖 paused_for_human 两态（不再只认 paused_clarification）。"""
    mid = await _mk_running_mission(db_session)
    # 切到目标 paused 态（force 跳 FSM）
    from app.domain.lifecycle_service import LifecycleService
    from app.domain.lifecycle import LifecycleAction
    action = (LifecycleAction.PAUSE_FOR_CLARIFICATION
              if lifecycle == "paused_clarification"
              else LifecycleAction.PAUSE_FOR_CAPABILITY)
    await LifecycleService(db_session).transition(mid, action, reason="x")

    from app.services import pending_approval_service as pas
    await pas._resume_after_clarification(db_session, mid)

    db_session.expire_all()
    proj = await db_session.get(Mission, mid)
    assert proj.lifecycle_status == "running"
    assert proj.paused_reason is None


async def test_pause_for_pending_signals_current_tick(
    db_session, _patched_session_local, monkeypatch
):
    """2026-07-03 语义更新：落卡只 **signal**（set cancel_event，E2 tool 边界收尾），
    不再硬 cancel —— 本函数在 tick 的 tool 子 task 里执行，硬 cancel 自己会死锁 +
    深 gather 链 RecursionError 僵尸（见 test_tick_cancel_deadlock.py）。"""
    mid = await _mk_running_mission(db_session)

    signalled: list[uuid.UUID] = []

    def _fake_signal(mission_id):
        signalled.append(mission_id)
        return {"ok": True, "stage": "signal"}

    from app.services import super_inbox
    monkeypatch.setattr(super_inbox, "signal_cancel", _fake_signal)

    from app.services import pending_approval_service as pas
    await pas._pause_for_pending(db_session, mid)

    db_session.expire_all()
    proj = await db_session.get(Mission, mid)
    assert proj.lifecycle_status == "paused_clarification"
    assert signalled == [mid], "落人工门时应发协作取消信号 (H1 signal-only)"
