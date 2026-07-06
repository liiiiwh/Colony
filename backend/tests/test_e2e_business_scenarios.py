"""ADR-019 · 3 个完整业务场景集成测试（mission-only 模型）。

「业务场景」级集成测试：通过真实 service / API 公共接口串联完整流程。
LLM 由 conftest mock 为固定回显（无法触发真 tool-calling），故聚焦数据面 +
控制面真实代码路径，而非 LLM 决策本身。

- 场景 A：Colony Builder 工厂建出**合规** super + worker（provenance + 单-super 不变量）
- 场景 B：Mission 自动运行 —— 派发线程隔离 + worker 记忆持久 + 交付物入 workspace
- 场景 C：人在回路审批 —— super 主动发起审批 → 落 pending_approvals → 人决策落定
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent, AgentSkill
from app.models.mission import Mission
from app.models.provider import LLMModel, LLMProvider
from app.models.skill import Skill
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _seed_model(db) -> uuid.UUID:
    pid = uuid.uuid4()
    db.add(LLMProvider(id=pid, name=f"prov-{uuid.uuid4().hex[:5]}", provider_type="openai",
                       api_key="x", base_url="https://x"))
    mid = uuid.uuid4()
    db.add(LLMModel(id=mid, provider_id=pid, model_id="gpt-4o",
                    display_name="GPT-4o", model_type="chat"))
    await db.flush()
    return mid


async def _agent_skill_slugs(db, agent_id: uuid.UUID) -> set[str]:
    rows = (await db.execute(
        select(Skill.slug)
        .join(AgentSkill, AgentSkill.skill_id == Skill.id)
        .where(AgentSkill.agent_id == agent_id)
    )).scalars().all()
    return set(rows)


# ─────────────────────────── 场景 A ───────────────────────────

async def test_scenario_a_builder_creates_compliant_super_and_worker(
    seeded_db, _patched_session_local
):
    """Colony Builder 通过工厂工具创建出符合平台规范的 worker + super：
    worker 带 capability、自动绑 return_result、无 provenance；
    super 带 provenance(=builder mission)、强脑默认、绑 invoke_worker/request_approval；
    且同一 builder mission 再建 super 触发单-super 不变量（复用，不重建）。
    """
    from app.db.init_db import seed_builtin_skills
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.builder.builder_skills import agent_create_tool
    from app.skills_builtin.context import BuiltinToolContext

    db = seeded_db
    await seed_builtin_skills(db)
    u = (await db.execute(select(User))).scalars().first()

    builder_agent = Agent(name="Builder Supervisor T", category="builder", kind="builder",
                          model_id=None, soul_md="x", protocol_md="x")
    db.add(builder_agent)
    await db.flush()
    # slug='builder' 才触发单-super 不变量分支
    builder_mission = Mission(name="Colony Builder", slug="builder",
                              supervisor_agent_id=builder_agent.id, created_by=u.id)
    db.add(builder_mission)
    mid = await _seed_model(db)
    await db.commit()

    ctx = BuiltinToolContext(mission_id=builder_mission.id, db_factory=AsyncSessionLocal)

    # 1) Builder 建 worker
    wres = await agent_create_tool(ctx).coroutine(
        name="xhs-ops-worker", model_id=str(mid), kind="worker",
        capability="xhs_ops", category="worker.io",
        soul_md="# Who I am\n小红书运营 worker", protocol_md="## Steps\n1. 执行",
    )
    assert wres["ok"] is True, wres
    worker = (await db.execute(
        select(Agent).where(Agent.id == uuid.UUID(wres["agent_id"]))
    )).scalar_one()
    assert worker.kind == "worker"
    assert worker.capability == "xhs_ops"
    # 2026-07-05：worker 也带 provenance（完整性 gate 确定性收编花名册用）
    assert worker.built_by_mission_id is not None
    wskills = await _agent_skill_slugs(db, worker.id)
    assert "return_result" in wskills  # 合规：自动绑定 worker 默认 skill

    # 2) Builder 建 super
    sres = await agent_create_tool(ctx).coroutine(
        name="xhs-super", model_id=str(mid), kind="super",
        soul_md="# Who I am\n小红书运营 super", protocol_md="规划并分派",
    )
    assert sres["ok"] is True and sres.get("reused") is not True, sres
    super_agent = (await db.execute(
        select(Agent).where(Agent.id == uuid.UUID(sres["agent_id"]))
    )).scalar_one()
    assert super_agent.kind == "super"
    assert super_agent.built_by_mission_id == builder_mission.id  # provenance
    assert super_agent.max_iterations == 40  # super 默认深度迭代
    sskills = await _agent_skill_slugs(db, super_agent.id)
    assert "invoke_worker" in sskills
    assert "request_approval" in sskills

    # 3) 单-super 不变量：同 builder mission 再建 super → 复用，不重建
    sres2 = await agent_create_tool(ctx).coroutine(
        name="xhs-super-2", model_id=str(mid), kind="super",
        soul_md="x", protocol_md="y",
    )
    assert sres2["ok"] is True
    assert sres2.get("reused") is True
    assert sres2["agent_id"] == sres["agent_id"]


# ─────────────────────────── 场景 B ───────────────────────────

async def test_scenario_b_mission_autorun_isolation_memory_deliverable(db_session):
    """Mission 自动运行：main 线程（super↔用户）与 super↔worker 派发线程消息隔离；
    worker 记忆按 (mission, thread_key, worker) 持久且隔离；交付物上传 S3（ADR-027 ·
    按 mission_id + capability + label 归档，不再写 mission.workspace[node]）。
    """
    from app.schemas.message import Artifact
    from app.services import memory_service, messaging_service, workspace_service
    from app.services.storage_service import make_inmemory_backend, set_storage

    set_storage(make_inmemory_backend())
    try:
        db = db_session
        u = User(username=f"u-{uuid.uuid4().hex[:6]}",
                 email=f"{uuid.uuid4().hex[:6]}@t.io", hashed_password="x")
        db.add(u)
        await db.flush()
        sup = Agent(name=f"sup-{uuid.uuid4().hex[:6]}", category="custom", kind="super",
                    model_id=None, soul_md="x", protocol_md="x")
        db.add(sup)
        await db.flush()
        mission = Mission(name="小红书运营", slug=f"m-{uuid.uuid4().hex[:8]}",
                          supervisor_agent_id=sup.id, created_by=u.id)
        db.add(mission)
        await db.commit()
        await db.refresh(mission)
        mid = mission.id

        # main 线程：用户 + super 对话
        await messaging_service.append_message(db, mid, "main", "user", "本周发 3 条小红书笔记", publish=False)
        await messaging_service.append_message(db, mid, "main", "assistant", "收到，开始派发 worker", publish=False)

        # super↔worker 派发线程（隔离）
        wtk = f"super-{sup.id.hex[:8]}-worker-abcd1234"
        await messaging_service.append_message(db, mid, wtk, "user", "[dispatch] 写第一条笔记", publish=False)
        await messaging_service.append_message(db, mid, wtk, "assistant", "已完成第一条", publish=False)

        # 隔离断言：两条线程互不串
        main_msgs = await messaging_service.list_thread_messages(db, mid, "main")
        worker_msgs = await messaging_service.list_thread_messages(db, mid, wtk)
        assert [m.content for m in main_msgs] == ["本周发 3 条小红书笔记", "收到，开始派发 worker"]
        assert [m.content for m in worker_msgs] == ["[dispatch] 写第一条笔记", "已完成第一条"]

        # worker 记忆持久（按 (mission, thread_key, node)）
        await memory_service.upsert_thread_memory(db, mid, wtk, "xhs-ops-worker",
                                      "## 进度\n已发 1/3", 2)
        mem = await memory_service.get_thread_memory(db, mid, wtk, "xhs-ops-worker")
        assert mem is not None and "已发 1/3" in mem.memory_md
        # 记忆隔离：main 线程查不到该 worker 记忆
        assert await memory_service.get_thread_memory(db, mid, "main", "xhs-ops-worker") is None

        # 交付物上传 S3（ADR-027 · 按 mission_id + capability + label 归档；不再写 mission.workspace）
        art = Artifact(type="markdown", label="第一条笔记", content="# 笔记\n内容...")
        saved = await workspace_service.write_artifact(
            db, mission, art, capability="xhs_ops", is_deliverable=True
        )
        assert saved.s3_key, "交付物应上传 S3"
        assert f"/{mission.id}/xhs_ops/" in saved.s3_key
        await db.refresh(mission, attribute_names=["workspace"])
        assert mission.workspace == {}, "by-node workspace 已退役，不应被写"
    finally:
        set_storage(None)  # type: ignore[arg-type]


# ─────────────────────────── 场景 C ───────────────────────────

async def test_scenario_c_human_in_loop_approval(
    seeded_client, seeded_db, _patched_session_local, monkeypatch
):
    """人在回路：super 发起审批，approval_judge 判 must_human=True 强制人审 → 落 pending_approvals →
    observe 页列出 → 人决策 → status=decided，不再 pending。
    """
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.context import BuiltinToolContext
    from app.skills_builtin.super.supervisor_skills import request_approval_tool
    from app.services import approval_judge_service

    async def _judge_human(db, mission, **kw):
        return True, "irreversible: 发布 + 投放预算"
    monkeypatch.setattr(approval_judge_service, "judge_must_human", _judge_human)

    db = seeded_db
    u = (await db.execute(select(User))).scalars().first()
    sup = Agent(name=f"sup-{uuid.uuid4().hex[:6]}", category="custom", kind="super",
                model_id=None, soul_md="x", protocol_md="x")
    db.add(sup)
    await db.flush()
    mission = Mission(name="运营审批", slug=f"appr-{uuid.uuid4().hex[:8]}",
                      supervisor_agent_id=sup.id, created_by=u.id)
    db.add(mission)
    await db.commit()
    mid = mission.id

    token = (await seeded_client.post(
        "/api/auth/login", data={"username": "admin", "password": "admin123"}
    )).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # super 在 main 线程主动发起审批
    ctx = BuiltinToolContext(mission_id=mid, thread_key="main",
                             agent_node_name="运营审批", db_factory=AsyncSessionLocal)
    res = await request_approval_tool(ctx).coroutine(
        title="是否发布本周运营计划？",
        message="计划：发 3 条笔记 + 投放 ¥500",
        options=["同意发布", "暂缓"],
        context="发布 + 投放预算，不可逆外发，须人工确认",
    )
    assert isinstance(res, str)

    # observe 页列出待审批
    lst = await seeded_client.get(
        f"/api/missions/{mid}/pending-approvals?only_pending=true", headers=auth
    )
    assert lst.status_code == 200
    pend = lst.json()
    assert len(pend) == 1
    assert pend[0]["thread_key"] == "main"
    assert pend[0]["status"] == "pending"
    rid = pend[0]["request_id"]

    # 人决策
    dec = await seeded_client.post(
        f"/api/pending-approvals/{rid}/decide", headers=auth,
        json={"option": "同意发布", "decided_by": "admin"},
    )
    assert dec.status_code == 200, dec.text
    assert dec.json()["status"] == "decided"
    assert dec.json()["decided_option"] == "同意发布"

    # 复核：不再 pending
    lst2 = await seeded_client.get(
        f"/api/missions/{mid}/pending-approvals?only_pending=true", headers=auth
    )
    assert lst2.json() == []
