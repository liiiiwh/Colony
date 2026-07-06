"""Skill 目录绿地清理（2026-07-02 grill 决议）。

1. 死技能代码级移除：build_super/build_worker（M2 工厂 skill 层）、sandbox_clone_mission/
   sandbox_cleanup（无人用的手动沙箱件）、worker_telemetry、find_workers、voice_chat_mock
   （demo 占位）从 metadata + 工具注册表消失；remote_skill_invoke 只删 metadata 模板行、
   **工厂必须保留**（所有 clawhub 安装件的 builtin_ref 执行模板）；clawhub_uninstall 保留。
2. seed_builtin_skills 孤儿从"自动下架"改为"物理删除"——builtin 行不在 metadata 即删
   （绑定靠 FK CASCADE 清），A 类孤儿（dispatch_to_worker 等 6 个）随启动 seed 消失。
3. reconcile_scoped_skill_bindings 跳过 is_system agent：系统 agent（Approval Judge /
   MCP Installer）技能集只认 seed 显式绑定，不再被 worker-scope 回填糊上 xhs 发布件。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import Agent, AgentSkill
from app.models.skill import Skill


REMOVED_SLUGS = [
    "build_super",
    "build_worker",
    "sandbox_clone_mission",
    "sandbox_cleanup",
    "worker_telemetry",
    "find_workers",
    "voice_chat_mock",
    "remote_skill_invoke",
]


def test_removed_skills_absent_from_metadata_and_registry():
    from app.skills_builtin import BUILTIN_SKILL_METADATA, BUILTIN_TOOL_REGISTRY

    meta_slugs = {m["slug"] for m in BUILTIN_SKILL_METADATA}
    for slug in REMOVED_SLUGS:
        assert slug not in meta_slugs, f"{slug} 不该再出现在 metadata"
    for slug in REMOVED_SLUGS:
        if slug == "remote_skill_invoke":
            continue
        assert slug not in BUILTIN_TOOL_REGISTRY, f"{slug} 的工厂该删掉"
    # remote_skill_invoke 工厂必须活着：clawhub 安装件 builtin_ref 指向它
    assert "remote_skill_invoke" in BUILTIN_TOOL_REGISTRY
    # 幸存者不许误伤
    for slug in ("clawhub_uninstall", "mission_run_test", "create_skill_from_template",
                 "release_work_claim", "list_workers"):
        assert slug in meta_slugs and slug in BUILTIN_TOOL_REGISTRY, f"{slug} 该保留"


@pytest.mark.asyncio
async def test_seed_purges_builtin_orphans(db_session):
    """metadata 里没有的 builtin 行 → seed 物理删除（连绑定），不再只是下架。"""
    from app.db.init_db import seed_builtin_skills

    orphan = Skill(
        name="Dead Dispatch", slug="dispatch_to_worker_zzz", description="",
        skill_type="tool_builtin", builtin_ref="dispatch_to_worker_zzz",
        content_md="", config_schema={}, is_enabled=False, is_builtin=True,
    )
    agent = Agent(name="orphan-holder", kind="worker", category="custom")
    db_session.add_all([orphan, agent])
    await db_session.flush()
    db_session.add(AgentSkill(agent_id=agent.id, skill_id=orphan.id, config={}))
    await db_session.commit()

    await seed_builtin_skills(db_session)

    assert (await db_session.execute(
        select(Skill).where(Skill.slug == "dispatch_to_worker_zzz")
    )).scalar_one_or_none() is None, "孤儿 builtin 行该被物理删除"
    assert (await db_session.execute(
        select(AgentSkill).where(AgentSkill.skill_id == orphan.id)
    )).scalar_one_or_none() is None, "孤儿的绑定该随之消失"
    # 非 builtin 的自定义 skill 不受影响
    custom = Skill(
        name="My Custom", slug="my-custom-zzz", description="",
        skill_type="prompt", content_md="x", config_schema={},
        is_enabled=True, is_builtin=False,
    )
    db_session.add(custom)
    await db_session.commit()
    await seed_builtin_skills(db_session)
    assert (await db_session.execute(
        select(Skill).where(Skill.slug == "my-custom-zzz")
    )).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_reconcile_skips_system_agents(db_session):
    """worker-scope 回填/prune 都跳过 is_system；普通 worker 照常回填。"""
    from app.db.init_db import reconcile_scoped_skill_bindings

    wskill = Skill(
        name="W Tool", slug="w-tool-zzz", description="", skill_type="tool_builtin",
        builtin_ref="w-tool-zzz", content_md="", config_schema={},
        is_enabled=True, is_builtin=True, scope="worker",
    )
    sys_worker = Agent(name="sys-judge", kind="worker", category="utility", is_system=True)
    biz_worker = Agent(name="biz-w", kind="worker", category="custom")
    db_session.add_all([wskill, sys_worker, biz_worker])
    await db_session.commit()

    await reconcile_scoped_skill_bindings(db_session)

    sys_bound = (await db_session.execute(
        select(AgentSkill).where(AgentSkill.agent_id == sys_worker.id)
    )).scalars().all()
    biz_bound = (await db_session.execute(
        select(AgentSkill).where(
            AgentSkill.agent_id == biz_worker.id, AgentSkill.skill_id == wskill.id
        )
    )).scalar_one_or_none()
    assert sys_bound == [], "系统 worker 不该被 scope 回填"
    assert biz_bound is not None, "普通 worker 照常回填"


@pytest.mark.asyncio
async def test_worker_opt_ensure_backfills_super_toolkit(db_session):
    """reconcile 豁免 is_system 后，worker-opt 的 super 工具集由它自己的 seed 幂等补齐——
    即便存量 agent 的绑定被清空（真出过：清理脏绑定把它打成 0 技能）。"""
    from app.db.init_db import seed_builtin_skills
    from app.models.user import User
    from app.services.worker_optimization_service import (
        WORKER_OPT_AGENT_NAME,
        ensure_worker_optimization_super,
    )

    db_session.add(User(username="admin", email="a@x.com", hashed_password="x", role="admin"))
    await db_session.commit()
    await seed_builtin_skills(db_session)

    res = await ensure_worker_optimization_super(db_session)
    assert res is not None
    agent, _ = res
    # 模拟脏绑定清空
    await db_session.execute(
        AgentSkill.__table__.delete().where(AgentSkill.agent_id == agent.id)
    )
    await db_session.commit()

    await ensure_worker_optimization_super(db_session)

    bound_slugs = set((await db_session.execute(
        select(Skill.slug).join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(AgentSkill.agent_id == agent.id)
    )).scalars().all())
    for slug in ("invoke_worker", "list_workers", "memory_append", "request_approval"):
        assert slug in bound_slugs, f"worker-opt 该拿回 super 工具集（缺 {slug}）"


@pytest.mark.asyncio
async def test_builder_seed_binds_invoke_worker(db_session):
    """ADR-031 委派链的硬前提：Builder 显式绑 invoke_worker。

    真出过：协议叫 Builder invoke_worker(capability:mcp_installer)，但 seed 清单没这个
    工具——之前全靠 reconcile 把 super-scope 糊给 Builder 兜着；is_system 豁免 reconcile
    后断供，Builder 建 MCP super 时退化成'让用户手动装'。"""
    from app.db.init_db import seed_builder_project, seed_builtin_skills
    from app.models.user import User

    db_session.add(User(username="admin", email="adm@x.com", hashed_password="x", role="admin"))
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
    assert "invoke_worker" in bound, "Builder 必须能 invoke_worker 才能委派 mcp_installer"
