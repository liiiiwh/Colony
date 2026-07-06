"""ADR-031 · 系统级 MCP Installer worker：种子 + 能力 + 技能 + 可被 capability dispatch 解析。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

async def _seed_with_admin(db):
    """seed_builder_project 需要 INIT_ADMIN_USERNAME 用户存在，否则跳过。"""
    from app.core.config import settings
    from app.models.user import User
    from app.db.init_db import seed_builder_project
    db.add(User(username=settings.INIT_ADMIN_USERNAME, email="admin@t.io", hashed_password="x"))
    await db.flush()
    await seed_builder_project(db)



async def test_mcp_installer_seeded_with_capability_and_skills(db_session):
    from sqlalchemy import select
    from app.db.init_db import seed_builder_project
    from app.db.system_agent_prompts import MCP_INSTALLER_NAME
    from app.models.agent import Agent, AgentSkill
    from app.models.skill import Skill

    await _seed_with_admin(db_session)

    inst = (await db_session.execute(
        select(Agent).where(Agent.name == MCP_INSTALLER_NAME))).scalar_one_or_none()
    assert inst is not None, "MCP Installer 未被种子"
    assert inst.kind == "worker"
    assert inst.capability == "mcp_installer"
    assert inst.is_system is True

    slugs = (await db_session.execute(
        select(Skill.slug).join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(AgentSkill.agent_id == inst.id))).scalars().all()
    for need in ["run_shell", "mcp_server_register", "agent_mcp_bind",
                 "mcp_ensure_ready", "request_approval", "clawhub_install"]:
        assert need in slugs, f"installer 缺技能 {need}（实得 {sorted(slugs)}）"


async def test_installer_invokable_by_capability(db_session):
    from app.db.init_db import seed_builder_project
    from app.skills_builtin.super.super_dispatch_skills import _resolve_worker

    await _seed_with_admin(db_session)
    agent, err = await _resolve_worker(db_session, "capability:mcp_installer")
    assert err is None, f"应能按 capability 解析，实得 err={err}"
    assert agent is not None and agent.capability == "mcp_installer"


async def test_installer_not_in_business_worker_catalog(db_session):
    """installer 是系统对象 → 不进 list_workers 业务目录（is_system 过滤）。"""
    from sqlalchemy import select
    from app.db.init_db import seed_builder_project
    from app.models.agent import Agent

    await _seed_with_admin(db_session)
    catalog = (await db_session.execute(
        select(Agent).where(Agent.kind == "worker", Agent.is_system.is_(False)))).scalars().all()
    assert all(a.capability != "mcp_installer" for a in catalog)


async def test_builder_unbound_from_shell_mcp_tools(db_session):
    """ADR-031 + install-first(2026-07-03) · Builder 不绑 run_shell/mcp_server_register/mcp_ensure_ready
    （安装/shell 委派 installer），但**保留 agent_mcp_bind**——install-first 下 Builder 建完 worker
    后自己把就绪 MCP 绑上去。"""
    from sqlalchemy import select
    from app.models.agent import Agent, AgentSkill
    from app.models.skill import Skill

    await _seed_with_admin(db_session)
    b = (await db_session.execute(select(Agent).where(Agent.slug == "builder"))).scalar_one()
    slugs = (await db_session.execute(
        select(Skill.slug).join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(AgentSkill.agent_id == b.id))).scalars().all()
    for gone in ["run_shell", "mcp_server_register", "mcp_ensure_ready"]:
        assert gone not in slugs, f"Builder 仍绑着已委派技能 {gone}"
    assert "agent_mcp_bind" in slugs, "install-first 下 Builder 要自己绑 MCP → 应保留 agent_mcp_bind"
