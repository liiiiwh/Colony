"""clawhub 工具 target_project_id 容错（2026-07-03 e2e 实证缺陷）。

事故：installer worker 的 LLM 给 clawhub_install 传了非 UUID 的 target_project_id
（mission slug），工具裸抛 ValueError('badly formed hexadecimal UUID string') →
整个 invoke_worker 图崩掉。违反「错误回流自愈」：工具**永不裸抛**，坏参数要返回
结构化错误（LLM 才能自纠重试）；slug 是 LLM 天然爱传的形态 → 顺手支持 slug 解析。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.skills_builtin.context import BuiltinToolContext

pytestmark = pytest.mark.asyncio


async def _mk_mission(db, slug: str) -> Mission:
    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.io",
             hashed_password="x")
    db.add(u)
    await db.flush()
    ag = Agent(name=f"s-{uuid.uuid4().hex[:6]}", category="custom", kind="super",
               soul_md="x", protocol_md="x")
    db.add(ag)
    await db.flush()
    m = Mission(name=slug, slug=slug, supervisor_agent_id=ag.id, created_by=u.id)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


def _ctx(db_factory) -> BuiltinToolContext:
    return BuiltinToolContext(
        mission_id=None, thread_key="main", agent_node_name="installer",
        memory_scope="project", db_factory=db_factory,
    )


async def test_install_bad_target_project_id_returns_structured_error(
    db_session, _patched_session_local,
):
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.channel.clawhub_skills import clawhub_install_tool

    tool = clawhub_install_tool(_ctx(AsyncSessionLocal))
    res = await tool.coroutine(slug="xhs-mcp", target_project_id="不是-uuid-也不是-slug")
    assert isinstance(res, dict), "坏参数不得裸抛（事故里 ValueError 崩掉整个 invoke_worker）"
    assert res.get("ok") is False
    assert "target_project_id" in str(res.get("error", "")) or res.get("instruction")


async def test_install_accepts_mission_slug(
    db_session, _patched_session_local, monkeypatch,
):
    """LLM 天然爱传 slug → 工具解析 slug 到 mission UUID。"""
    from app.db.session import AsyncSessionLocal
    from app.services import remote_skill_installer
    from app.skills_builtin.channel import clawhub_skills as cs

    m = await _mk_mission(db_session, f"m-{uuid.uuid4().hex[:8]}")

    seen: dict = {}

    async def _fake_install(db, *, slug, version=None, mission_id=None, force_high_risk=False):
        seen["mission_id"] = mission_id
        raise RuntimeError("stop-here")  # 只验证 pid 解析，不真装

    monkeypatch.setattr(remote_skill_installer, "install", _fake_install)

    tool = cs.clawhub_install_tool(_ctx(AsyncSessionLocal))
    res = await tool.coroutine(slug="xhs-mcp", target_project_id=m.slug)
    assert seen.get("mission_id") == m.id, f"slug 应解析成 mission UUID（实得 {seen}）"
    assert isinstance(res, dict)  # RuntimeError 也不裸抛（既有 except 捕获或本次补）


async def test_list_installed_bad_target_project_id_returns_structured_error(
    db_session, _patched_session_local,
):
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.channel.clawhub_skills import clawhub_list_installed_tool

    tool = clawhub_list_installed_tool(_ctx(AsyncSessionLocal))
    res = await tool.coroutine(target_project_id="xxx-bad")
    assert isinstance(res, dict) and res.get("ok") is False


async def test_uninstall_bad_install_id_returns_structured_error(
    db_session, _patched_session_local,
):
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.channel.clawhub_skills import clawhub_uninstall_tool

    tool = clawhub_uninstall_tool(_ctx(AsyncSessionLocal))
    res = await tool.coroutine(install_id="not-a-uuid")
    assert isinstance(res, dict) and res.get("ok") is False
