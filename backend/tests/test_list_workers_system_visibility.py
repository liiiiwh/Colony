"""list_workers 对系统调用方开放系统 worker 可见性。

真出过（v3 e2e，2026-07-02）：Builder 按 ADR-031 想委派 mcp_installer，先 list_workers
验证——目录排除 is_system worker → Builder 判定「installer 不存在」→ 转头弹人工卡。
验证后再派发是好行为，不该被目录骗。规则：调用方自己是 is_system（Builder / 系统
super）→ 目录含系统 worker（带 is_system 标记）；业务 super 维持不可见（防误派）。
"""
from __future__ import annotations

import json

import pytest

from app.models.agent import Agent
from app.skills_builtin.context import BuiltinToolContext
from app.skills_builtin.super.super_dispatch_skills import list_workers_tool


def _ctx(db_session, agent_id):
    def factory():
        return db_session

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *a):
            return False

    return BuiltinToolContext(db_factory=_Factory(), agent_id=agent_id)


async def _seed(db):
    sys_caller = Agent(name="builder-like", kind="super", category="builder", is_system=True)
    biz_caller = Agent(name="biz-super", kind="super", category="custom")
    sys_worker = Agent(
        name="installer-like", kind="worker", capability="mcp_installer_x",
        category="installer", is_system=True,
    )
    biz_worker = Agent(name="biz-worker", kind="worker", capability="biz_cap_x", category="custom")
    db.add_all([sys_caller, biz_caller, sys_worker, biz_worker])
    await db.commit()
    return sys_caller, biz_caller


@pytest.mark.asyncio
async def test_system_caller_sees_system_workers(db_session):
    sys_caller, _ = await _seed(db_session)
    tool = list_workers_tool(_ctx(db_session, sys_caller.id))

    out = json.loads(await tool.coroutine())

    caps = {i["capability"] for i in out["items"]}
    assert "mcp_installer_x" in caps, "系统调用方该看到系统 worker"
    assert "biz_cap_x" in caps


@pytest.mark.asyncio
async def test_business_caller_does_not_see_system_workers(db_session):
    _, biz_caller = await _seed(db_session)
    tool = list_workers_tool(_ctx(db_session, biz_caller.id))

    out = json.loads(await tool.coroutine())

    caps = {i["capability"] for i in out["items"]}
    assert "mcp_installer_x" not in caps, "业务 super 不该看到系统 worker"
    assert "biz_cap_x" in caps
