"""ADR-030 · run_shell 门喂入本任务已核实用户审批 → 已批准操作放行的装配。"""
from __future__ import annotations

import uuid
import pytest

pytestmark = pytest.mark.asyncio


async def _mk_mission(db):
    from app.models.agent import Agent
    from app.models.mission import Mission
    from app.models.user import User
    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.io", hashed_password="x")
    db.add(u); await db.flush()
    ag = Agent(name="b", category="builder", kind="super", model_id=None, soul_md="x", protocol_md="x")
    db.add(ag); await db.flush()
    proj = Mission(name="m", slug=f"m-{uuid.uuid4().hex[:8]}", supervisor_agent_id=ag.id,
                   created_by=u.id, lifecycle_status="running", runtime_status="running")
    db.add(proj); await db.commit(); await db.refresh(proj)
    return proj


async def test_recent_approved_decisions_returns_decided_cards(db_session):
    """helper 取本任务最近已决审批（title/option/message），喂给 shell judge。"""
    from app.skills_builtin.builder.builder_skills import _recent_approved_decisions
    from app.services.pending_approval_service import create_pending, decide

    proj = await _mk_mission(db_session)
    row = await create_pending(
        db_session, mission_id=proj.id, title="授权安装小红书 MCP",
        message="clone+build+启动", options=["同意安装", "取消"],
        thread_key="main", dispatch_wechat=False,
    )
    await decide(db_session, request_id=row.request_id, option="同意安装", decided_by="user")

    got = await _recent_approved_decisions(db_session, proj.id)
    assert got, "应返回已决审批"
    assert got[0]["title"] == "授权安装小红书 MCP"
    assert got[0]["option"] == "同意安装"


async def test_recent_approved_decisions_empty_when_none(db_session):
    from app.skills_builtin.builder.builder_skills import _recent_approved_decisions
    proj = await _mk_mission(db_session)
    assert await _recent_approved_decisions(db_session, proj.id) == []
