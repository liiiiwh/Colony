"""agent_create 建 worker 时确定性合成最小 capability_contract。

真出过（v2 e2e，2026-07-02）：Builder BUILD 建了 4 个 worker 全没写 contract
（"必须 agent_update 写 contract"只写在 DESIGN_WORKER 协议段，BUILD 段没有），
新 super 首跑 list_workers 看到 advertises 全空 → 无法调度 → escalation 卡人工。
多步不变量不能靠 LLM 自觉——create 时平台兜底合成，LLM 想精化再 agent_update 覆盖。
"""
from __future__ import annotations

import pytest

from app.schemas.agent import AgentCreate
from app.services import agent_service


@pytest.mark.asyncio
async def test_worker_gets_minimal_contract_by_default(db_session):
    agent = await agent_service.create_agent(db_session, AgentCreate(
        name="w-auto-contract", category="custom", kind="worker",
        capability="demo_capability", model_id=None,
    ))

    contract = (agent.extra_config or {}).get("capability_contract")
    assert contract, "worker 该有自动合成的最小 contract"
    assert contract["capability"] == "demo_capability"
    actions = [a.get("action") for a in contract.get("advertises", [])]
    assert actions, "advertises 不能为空（super 靠它调度）"


@pytest.mark.asyncio
async def test_explicit_contract_not_overwritten(db_session):
    explicit = {
        "capability": "demo_cap2", "version": "2.0.0",
        "advertises": [{"action": "publish", "requires_approval": True}],
    }
    agent = await agent_service.create_agent(db_session, AgentCreate(
        name="w-explicit-contract", category="custom", kind="worker",
        capability="demo_cap2", model_id=None,
        extra_config={"capability_contract": explicit},
    ))

    assert (agent.extra_config or {}).get("capability_contract") == explicit


@pytest.mark.asyncio
async def test_non_worker_gets_no_contract(db_session):
    agent = await agent_service.create_agent(db_session, AgentCreate(
        name="s-no-contract", category="custom", kind="super", model_id=None,
    ))

    assert not (agent.extra_config or {}).get("capability_contract")
