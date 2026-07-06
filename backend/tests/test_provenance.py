"""ADR-018 D3 · 1:1 provenance.

A produced super records the origin Builder mission (built_by_mission_id) so super
self-iteration can route back to where it was designed — replacing the retired
session.target_project_id reverse-lookup chain. Migration window: write-only (escalation
routing still uses the old chain until step 5), so these tests pin the WRITE, not the route.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.skills_builtin.context import BuiltinToolContext

pytestmark = pytest.mark.asyncio


async def _seed_builder_mission(db) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns (builder_project_id, builder_session_id, model_id)."""
    from app.models.provider import LLMModel, LLMProvider

    u = User(username=f"u-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t.io",
             hashed_password="x")
    db.add(u)
    await db.flush()
    ag = Agent(name=f"builder-{uuid.uuid4().hex[:6]}", category="builder", kind="builder",
               model_id=None, soul_md="x", protocol_md="x")
    db.add(ag)
    await db.flush()
    proj = Mission(name="builder", slug=f"builder-{uuid.uuid4().hex[:6]}",
                   supervisor_agent_id=ag.id, created_by=u.id)
    db.add(proj)
    await db.flush()
    pid = uuid.uuid4()
    db.add(LLMProvider(id=pid, name=f"ds-{uuid.uuid4().hex[:5]}", provider_type="openai",
                       api_key="x", base_url="https://x"))
    mid = uuid.uuid4()
    db.add(LLMModel(id=mid, provider_id=pid, model_id="deepseek-v4-pro",
                    display_name="deepseek-v4-pro", model_type="chat"))
    await db.commit()
    return proj.id, mid


async def test_created_super_records_origin_builder_mission(db_session, _patched_session_local):
    from app.skills_builtin.builder.builder_skills import agent_create_tool

    mission_id, mid = await _seed_builder_mission(db_session)
    from app.db.session import AsyncSessionLocal

    ctx = BuiltinToolContext(
        mission_id=mission_id, db_factory=AsyncSessionLocal,
    )
    tool = agent_create_tool(ctx)
    res = await tool.coroutine(
        name=f"super-{uuid.uuid4().hex[:6]}", model_id=str(mid), kind="super",
        soul_md="s", protocol_md="p",
    )
    assert res["ok"] is True and res.get("reused") is not True

    agent = (await db_session.execute(
        select(Agent).where(Agent.id == uuid.UUID(res["agent_id"]))
    )).scalar_one()
    assert agent.built_by_mission_id == mission_id


async def test_created_worker_carries_provenance(db_session, _patched_session_local):
    """2026-07-05 语义更新：worker 也带 built_by_mission_id——完整性 gate 的确定性
    自修复按「本 builder 会话建的孤儿 worker」收编花名册（LLM 屡次无视 agent_update
    声明指令，e2e 实证三连拒），provenance 让 finalize 能代劳。"""
    from app.skills_builtin.builder.builder_skills import agent_create_tool

    mission_id, mid = await _seed_builder_mission(db_session)
    from app.db.session import AsyncSessionLocal

    ctx = BuiltinToolContext(mission_id=mission_id, db_factory=AsyncSessionLocal)
    res = await agent_create_tool(ctx).coroutine(
        name=f"wk-{uuid.uuid4().hex[:6]}", model_id=str(mid), kind="worker",
        capability="xhs_ops", category="worker.web",
    )
    assert res["ok"] is True
    agent = (await db_session.execute(
        select(Agent).where(Agent.id == uuid.UUID(res["agent_id"]))
    )).scalar_one()
    assert agent.built_by_mission_id == mission_id
