"""activate_super_first_run 技能过完整性 gate（2026-07-03 e2e 实证绕过洞）。

事故：finalize 的完整性 gate 两次拦下残废 super（roster 空、8 个孤儿 worker），但
Builder LLM 直接调 activate_super_first_run 技能自我认证激活——技能路径没有 gate，
写下 super_activated 后 finalize 永远 already_finalized 短路，gate 形同虚设。
修：技能与 finalize 共用 check_build_completeness；不完整 → 拒绝激活 + 把缺口
作为工具结果回流（错误驱动自愈，LLM 看到具体缺口才能补）。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.skills_builtin.context import BuiltinToolContext

pytestmark = pytest.mark.asyncio


async def _setup(db, *, roster, worker_caps):
    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.io",
             hashed_password="x")
    db.add(u)
    await db.flush()
    sup = Agent(name=f"s-{uuid.uuid4().hex[:6]}", kind="super", category="custom",
                slug=f"s-{uuid.uuid4().hex[:8]}",
                extra_config={"required_capabilities": roster} if roster is not None else {})
    db.add(sup)
    await db.flush()
    for c in worker_caps:
        db.add(Agent(name=f"w-{c}", kind="worker", category="custom", capability=c))
    m = Mission(name="m", slug=f"m-{uuid.uuid4().hex[:8]}", supervisor_agent_id=sup.id,
                created_by=u.id, status="active")
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


def _tool(db_factory, builder_mission_id=None):
    from app.skills_builtin.builder.builder_skills import activate_super_first_run_tool

    return activate_super_first_run_tool(BuiltinToolContext(
        mission_id=builder_mission_id, thread_key="main", agent_node_name="supervisor",
        memory_scope="project", db_factory=db_factory,
    ))


async def test_activate_refused_when_incomplete(db_session, _patched_session_local, monkeypatch):
    """roster 空 + 有孤儿 worker → 拒绝激活：不 kickoff、不写 super_activated、
    工具结果带缺口清单（LLM 自愈）。"""
    from app.db.session import AsyncSessionLocal
    from app.services import mission_daemon as md

    m = await _setup(db_session, roster=[], worker_caps=["trend", "publisher"])

    kicked = {"n": 0}

    async def _spy_start(db, pid, kickoff=False):
        kicked["n"] += 1

    monkeypatch.setattr(md, "start", _spy_start)

    res = await _tool(AsyncSessionLocal).coroutine(mission_id=str(m.id))
    assert res.get("ok") is False, "残废 super 不得被技能路径自我认证激活"
    assert kicked["n"] == 0
    assert "orphan" in str(res).lower() or "花名册" in str(res) or "required_capabilities" in str(res), \
        f"缺口要回流给 LLM 自愈（实得 {res}）"

    from sqlalchemy import text
    row = (await db_session.execute(text(
        "SELECT COUNT(*) FROM messages WHERE meta LIKE '%super_activated%'"
    ))).scalar()
    assert not row, "不完整时不得写 super_activated（否则 finalize 永远短路）"


async def test_activate_allowed_when_complete(db_session, _patched_session_local, monkeypatch):
    from app.db.session import AsyncSessionLocal
    from app.services import mission_daemon as md

    m = await _setup(db_session, roster=["trend"], worker_caps=["trend"])

    async def _spy_start(db, pid, kickoff=False):
        return None

    monkeypatch.setattr(md, "start", _spy_start)

    res = await _tool(AsyncSessionLocal).coroutine(mission_id=str(m.id))
    assert res.get("ok") is True, res
