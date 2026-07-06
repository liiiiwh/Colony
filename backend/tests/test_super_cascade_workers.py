"""删 super 级联时对「独占 worker」的识别与处理。

真出过的坑：删 xhs-supervisor 的确认预览显示「0 个该 super 独占的 Worker」，
但它的 protocol 明明派发 4 个专属 capability worker。ADR-027 把 worker 一刀切
成"平台级共享资源"后 preview 恒返回空、级联也不删 worker → 删 super 留一堆孤儿。

成员关系的活链接 = super.protocol_md 里的 capability 引用（invoke_worker 的依据），
不依赖会被删掉的 builder mission（built_by_mission_id 是 SET NULL，靠不住）。
独占 = 非 is_system 且没有其他 super 的 protocol 引用同一 capability。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.services import mission_service


async def _admin(db, name: str) -> uuid.UUID:
    u = User(username=name, email=f"{name}@x.com", hashed_password="x", role="admin")
    db.add(u)
    await db.flush()
    return u.id


def _worker(name: str, capability: str, **kw) -> Agent:
    return Agent(name=name, kind="worker", capability=capability, category="custom", **kw)


@pytest.mark.asyncio
async def test_preview_counts_exclusive_workers(db_session):
    """preview 按 protocol 的 capability 引用找出独占 worker；词边界防误配。"""
    sup = Agent(
        name="pub-super", kind="super", category="custom",
        protocol_md=(
            "流程：先 invoke_worker('capability:xhs_writer', ...) 写文案，"
            "再 invoke_worker('capability:xhs_publisher', ...) 发布。"
        ),
    )
    w1 = _worker("xhs-writer", "xhs_writer")
    w2 = _worker("xhs-publisher", "xhs_publisher")
    # 陷阱：capability='xhs' 是 'xhs_writer' 的前缀子串，但 protocol 没引用它本身
    trap = _worker("xhs-bare", "xhs")
    db_session.add_all([sup, w1, w2, trap])
    await db_session.commit()

    preview = await mission_service.preview_super_cascade(db_session, sup)

    assert sorted(preview["workers_to_delete"]) == ["xhs-publisher", "xhs-writer"]
    assert all(k["name"] != "xhs-bare" for k in preview["workers_to_keep"])


@pytest.mark.asyncio
async def test_preview_keeps_shared_and_system_workers(db_session):
    """被其他 super 引用 → shared 保留；is_system → system 保留。"""
    sup = Agent(
        name="pub-super2", kind="super", category="custom",
        protocol_md="invoke_worker('capability:cap_shared') / invoke_worker('capability:cap_sys')",
    )
    other = Agent(
        name="other-super", kind="super", category="custom",
        protocol_md="我也用 invoke_worker('capability:cap_shared')",
    )
    shared = _worker("w-shared", "cap_shared")
    sysw = _worker("w-sys", "cap_sys", is_system=True)
    db_session.add_all([sup, other, shared, sysw])
    await db_session.commit()

    preview = await mission_service.preview_super_cascade(db_session, sup)

    assert preview["workers_to_delete"] == []
    keep = {k["name"]: k["reason"] for k in preview["workers_to_keep"]}
    assert keep == {"w-shared": "shared", "w-sys": "system"}


@pytest.mark.asyncio
async def test_preview_matches_capabilities_in_extra_config(db_session):
    """super 的花名册也可能只在 extra_config.required_capabilities（agent_update 写入），
    protocol 里不含 slug——真出过：级联删只删掉 super 本体、4 个 worker 全漏。"""
    sup = Agent(
        name="cfg-super", kind="super", category="custom",
        protocol_md="每日：选题规划 → 内容写作 → 发布（协议不含 slug）",
        extra_config={"required_capabilities": ["cap_cfg_a", "cap_cfg_b"]},
    )
    wa = _worker("w-cfg-a", "cap_cfg_a")
    wb = _worker("w-cfg-b", "cap_cfg_b")
    db_session.add_all([sup, wa, wb])
    await db_session.commit()

    preview = await mission_service.preview_super_cascade(db_session, sup)

    assert sorted(preview["workers_to_delete"]) == ["w-cfg-a", "w-cfg-b"]


@pytest.mark.asyncio
async def test_delete_super_cascade_deletes_exclusive_workers(db_session):
    """真删：独占 worker 随 super 级联删；shared / system 幸存。"""
    admin_id = await _admin(db_session, "adm-casc-w")
    sup = Agent(
        name="pub-super3", kind="super", category="custom",
        protocol_md=(
            "invoke_worker('capability:cap_only') + invoke_worker('capability:cap_both')"
            " + invoke_worker('capability:cap_sys2')"
        ),
    )
    other = Agent(
        name="other-super2", kind="super", category="custom",
        protocol_md="invoke_worker('capability:cap_both')",
    )
    only = _worker("w-only", "cap_only")
    both = _worker("w-both", "cap_both")
    sysw = _worker("w-sys2", "cap_sys2", is_system=True)
    db_session.add_all([sup, other, only, both, sysw])
    await db_session.flush()
    m = Mission(
        name="pub-mission", slug="pub-mission-x",
        supervisor_agent_id=sup.id, created_by=admin_id, status="active",
    )
    db_session.add(m)
    await db_session.commit()
    only_id, both_id, sys_id = only.id, both.id, sysw.id

    res = await mission_service.delete_super_with_cascade(db_session, sup)

    assert await db_session.get(Agent, only_id) is None, "独占 worker 该被级联删"
    assert await db_session.get(Agent, both_id) is not None, "shared worker 不该被删"
    assert await db_session.get(Agent, sys_id) is not None, "is_system worker 不该被删"
    assert any("w-only" in x for x in res["deleted_agents"])
    assert (await db_session.execute(
        select(Mission).where(Mission.slug == "pub-mission-x")
    )).scalar_one_or_none() is None
