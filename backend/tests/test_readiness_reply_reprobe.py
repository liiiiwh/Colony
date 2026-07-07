"""用户回复 readiness-paused mission → _autostart_and_trigger re-probe（2026-07-06）。

二维码改无卡渲染消息后，用户回复（"扫好了"/"刷新二维码"）是恢复信号：
mission 处于 paused_waiting_capability(readiness:) → resume 后 re-run ensure_ready
（登录成功即过 / 未登录重发 QR）。锁住 wiring 不回归。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User


@pytest.mark.asyncio
async def test_reply_to_readiness_paused_triggers_reprobe(db_session, _patched_session_local, monkeypatch):
    from app.api import super_conversation as sc

    u = User(username="adm-rp", email="rp@x.com", hashed_password="x", role="admin")
    sup = Agent(name="sup-rp", kind="super", category="custom")
    db_session.add_all([u, sup])
    await db_session.flush()
    m = Mission(name="ops", slug="ops-rp", supervisor_agent_id=sup.id, created_by=u.id,
                status="active", lifecycle_status="paused_waiting_capability",
                runtime_status="running",
                paused_reason="readiness:xiaohongshu-mcp:logged_in 需人工介入（human-qr）")
    db_session.add(m)
    await db_session.commit()

    reprobed = []
    async def _spy_rerun(db, mission_id, reason):
        reprobed.append((mission_id, reason))
    monkeypatch.setattr(sc, "_trigger_tick_async", lambda *a, **k: None)
    from app.services import pending_approval_service as pas
    monkeypatch.setattr(pas, "_rerun_readiness_if_applicable", _spy_rerun)

    await sc._autostart_and_trigger(db_session, m.id, u.id, auto_trigger=False, auto_start=False)

    assert len(reprobed) == 1, "readiness-paused mission 收到用户回复应 re-probe"
    assert reprobed[0][0] == m.id
    assert reprobed[0][1].startswith("readiness:")


@pytest.mark.asyncio
async def test_reply_to_normal_paused_idle_no_reprobe(db_session, _patched_session_local, monkeypatch):
    """非 readiness 的 paused_idle → 不 re-probe（只有 readiness reason 才触发）。"""
    from app.api import super_conversation as sc

    u = User(username="adm-rp2", email="rp2@x.com", hashed_password="x", role="admin")
    sup = Agent(name="sup-rp2", kind="super", category="custom")
    db_session.add_all([u, sup])
    await db_session.flush()
    m = Mission(name="ops", slug="ops-rp2", supervisor_agent_id=sup.id, created_by=u.id,
                status="active", lifecycle_status="paused_idle", runtime_status="running")
    db_session.add(m)
    await db_session.commit()

    reprobed = []
    async def _spy_rerun(db, mission_id, reason):
        reprobed.append(mission_id)
    monkeypatch.setattr(sc, "_trigger_tick_async", lambda *a, **k: None)
    from app.services import pending_approval_service as pas
    monkeypatch.setattr(pas, "_rerun_readiness_if_applicable", _spy_rerun)

    await sc._autostart_and_trigger(db_session, m.id, u.id, auto_trigger=False, auto_start=False)
    assert reprobed == [], "非 readiness 暂停不该 re-probe"
