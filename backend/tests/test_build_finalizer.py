"""ADR-013 · 构建确定性收尾：Builder tick 后给新建 super 补默认 schedule + 激活首跑。

bug：finalize_super_build 第 80 行引用了不存在的 notify_session_id（ADR-018 删 sessions 后
的死变量）→ NameError 在「补默认 schedule + kickoff」之前就崩，被调用处 try/except 吞掉，
导致 super 建好却没 schedule、永远 stopped。修：用 notify_mission_id（origin 即 builder mission）。
"""
import uuid

import pytest
from sqlalchemy import text

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.services.build_finalizer import finalize_super_build


@pytest.mark.asyncio
async def test_finalize_creates_default_schedule_and_origin(db_session):
    user = User(username="u", email="u@x.com", hashed_password="x", role="admin")
    db_session.add(user)
    sup = Agent(name="Srv Mon Super", kind="super", category="custom",
                slug="srv-mon", display_name="服务器运维监控",
                extra_config={"required_capabilities": ["srv_monitor"]})
    db_session.add(sup)
    # 完整构建：声明的能力有对应 worker（否则 2026-07-06 空 roster gate 会挡下激活）
    db_session.add(Agent(name="w-srv", kind="worker", category="custom", capability="srv_monitor"))
    await db_session.flush()
    built = Mission(name="服务器运维监控", slug="srv-mon", supervisor_agent_id=sup.id,
                    created_by=user.id, workflow_config={})
    builder = Mission(name="Colony Builder", slug="builder", supervisor_agent_id=sup.id,
                      created_by=user.id, workflow_config={})
    db_session.add_all([built, builder])
    await db_session.commit()

    # 不崩（修 NameError）+ 写 origin_session_id=builder mission id。
    # 调度由 Builder 按场景决定，finalizer **不再强制补默认 cron**（事件驱动 super 不该被强加周期）。
    res = await finalize_super_build(db_session, built.id, builder.id, "main")
    assert res.get("ok") is True
    assert "default_schedule" not in res["actions"]

    from sqlalchemy import select, func, text as _t
    from app.models.mission import MissionSchedule
    sched = (await db_session.execute(
        select(func.count()).select_from(MissionSchedule).where(MissionSchedule.mission_id == built.id)
    )).scalar()
    assert sched == 0  # Builder 没建调度（本测试未建）→ finalizer 也不强加

    # CTA 幂等：再 finalize 一次（同 super）→ already-finalized 短路 → 仍只 1 张 super_activated 卡
    res2 = await finalize_super_build(db_session, built.id, builder.id, "main")
    assert res2.get("skipped") == "already_finalized"
    cta = (await db_session.execute(_t(
        "SELECT count(*) FROM messages WHERE meta->>'type'='super_activated' AND meta->>'project_slug'=:s"
    ), {"s": "srv-mon"})).scalar()
    assert cta == 1  # 不重复发卡片

    m = await db_session.get(Mission, built.id)
    assert (m.workflow_config or {}).get("origin_session_id") == str(builder.id)


@pytest.mark.asyncio
async def test_finalize_runs_for_plus_new_builder_mission(db_session):
    """+新建 builder mission（slug != 'builder' 但 supervisor 是 Builder super）也要触发收尾
    （ensure_ready + kickoff + origin），否则「每场景新建 mission」流程建出的 super 不收尾。
    调度由 Builder 按场景决定，finalizer 不强加。"""
    from app.services.build_finalizer import maybe_finalize_after_builder_tick

    user = User(username="u2", email="u2@x.com", hashed_password="x", role="admin")
    db_session.add(user)
    builder_sup = Agent(name="Builder Supervisor", kind="super", category="builder",
                        slug="builder", display_name="Colony Builder")
    new_super = Agent(name="Legal Reviewer", kind="super", category="custom",
                      slug="contract-review", display_name="合同条款审查助理",
                      extra_config={"required_capabilities": ["contract_review"]})
    db_session.add_all([builder_sup, new_super])
    # 完整构建：声明的能力有对应 worker（2026-07-06 空 roster gate）
    db_session.add(Agent(name="w-cr", kind="worker", category="custom", capability="contract_review"))
    await db_session.flush()
    # +新建 builder mission（slug 非 'builder'，但 supervisor 是 builder super）
    plus_new = Mission(name="合同条款审查", slug="mission-abc", supervisor_agent_id=builder_sup.id,
                       created_by=user.id, workflow_config={})
    db_session.add(plus_new)
    await db_session.flush()
    # 它建出的 super（built_by_mission_id 指向 +新建 mission）+ super 的 mission
    new_super.built_by_mission_id = plus_new.id
    built = Mission(name="合同条款审查", slug="contract-review", supervisor_agent_id=new_super.id,
                    created_by=user.id, workflow_config={})
    db_session.add(built)
    await db_session.commit()

    res = await maybe_finalize_after_builder_tick(db_session, plus_new.id)
    assert res is not None and res.get("ok") is True  # 不再因 slug!='builder' 跳过
    from sqlalchemy import select, func
    from app.models.mission import MissionSchedule
    sched = (await db_session.execute(
        select(func.count()).select_from(MissionSchedule).where(MissionSchedule.mission_id == built.id)
    )).scalar()
    assert sched == 0  # finalizer 不强制补默认调度（Builder 按场景决定）
