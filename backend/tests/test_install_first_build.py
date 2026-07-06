"""install-first 构建：Builder 拿回 agent_mcp_bind + installer 不绑。

2026-07-03 grill 决议（问题2-C/a）：
- 真 install-first——先把 MCP/skill 基础设施装好登录好，再建 worker 并绑定。
- installer 职责收敛到"基础设施就绪+登录好"，**不绑** worker（去掉 bind_to_agent_id）。
- 绑定归 Builder：worker 建出来后 Builder 自己 agent_mcp_bind + skill_bind。
  → agent_mcp_bind 必须加回 Builder 技能集（ADR-031 曾解绑给 installer）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import Agent, AgentSkill
from app.models.skill import Skill
from app.models.user import User


@pytest.mark.asyncio
async def test_builder_has_agent_mcp_bind(db_session):
    """Builder 建完 worker 要自己绑 MCP → 必须有 agent_mcp_bind。"""
    from app.db.init_db import seed_builder_project, seed_builtin_skills

    db_session.add(User(username="admin", email="adm-if@x.com", hashed_password="x", role="admin"))
    await db_session.commit()
    await seed_builtin_skills(db_session)
    await seed_builder_project(db_session)

    builder = (await db_session.execute(
        select(Agent).where(Agent.name == "Builder Supervisor")
    )).scalar_one()
    bound = set((await db_session.execute(
        select(Skill.slug).join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(AgentSkill.agent_id == builder.id)
    )).scalars().all())
    assert "agent_mcp_bind" in bound, "install-first 下 Builder 要自己绑 MCP 到 worker"
    # 但仍不碰安装/shell（那是 installer 的）
    for slug in ("run_shell", "mcp_server_register", "mcp_ensure_ready"):
        assert slug not in bound, f"{slug} 仍应只属 installer"


def test_installer_protocol_no_bind():
    """installer 协议不再声称自己 bind；明确 install-first + 不绑。"""
    from app.db.system_agent_prompts import MCP_INSTALLER_PROTOCOL as p

    assert "install-first" in p
    assert "do **NOT** bind" in p or "never call agent_mcp_bind" in p.lower() or \
        "Never call agent_mcp_bind" in p, "installer 协议应明确不绑 worker"
