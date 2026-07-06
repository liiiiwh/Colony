"""绑定 MCP 后能力可见性 + execute 万能兜底（2026-07-05 xhs_publisher 实证）。

事故：worker 绑了 xhs-mcp（运行时工具全量注入、tool_filter=null），但
1) worker-opt 的 write_contract 把合同收窄成单 `publish` action 并把 `execute` 标
   deprecated → invoke_worker 硬校验拒掉一切其它 action，super 永远没有通道让
   worker 用 MCP 的登录/取码/搜索/评论等方法（super 现场结论「只有 publish」）。
2) worker 系统提示词里没有「已绑定 MCP 能力清单+说明」，list_workers 也不展示 →
   super/worker 双盲。

修复合同：
- `execute`（goal 自然语言）是**不可废除的万能兜底 action**：合同没登记也可派发；
  requires_approval 继承「任一 advertised action 要审批」则要（防绕过 publish 审批）。
- worker 提示词注入「已绑定 MCP 能力」清单（read-time 从 mcp_inventory TTL 缓存取）。
- mcp_inventory：进程内 TTL 缓存，取失败回旧值/空，不抛。
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


# ── 1 · execute 万能兜底（纯函数校验层）─────────────────────────────────────────

def test_execute_fallback_always_dispatchable():
    from app.skills_builtin.super.super_dispatch_skills import resolve_action_spec

    contract = {
        "capability": "xhs_publisher",
        "deprecated_actions": ["execute"],  # 优化器想废也废不掉
        "advertises": [{"action": "publish", "requires_approval": True,
                        "side_effects": ["external_write"]}],
    }
    spec, err = resolve_action_spec(contract, "execute")
    assert err is None and spec is not None, "execute 必须始终可派发（万能兜底）"
    assert spec.get("requires_approval") is True, \
        "任一 advertised action 要审批 → execute 兜底也要（防绕过 publish 审批门）"

    # 无审批语义的合同 → execute 不强加审批
    spec2, err2 = resolve_action_spec({"advertises": [{"action": "analyze"}]}, "execute")
    assert err2 is None and spec2.get("requires_approval") is not True


def test_unknown_action_error_mentions_execute():
    from app.skills_builtin.super.super_dispatch_skills import resolve_action_spec

    spec, err = resolve_action_spec({"advertises": [{"action": "publish"}]}, "login")
    assert spec is None and err
    assert "execute" in err, "错误信息要提示 execute 兜底通道"


def test_advertised_action_still_resolves():
    from app.skills_builtin.super.super_dispatch_skills import resolve_action_spec

    spec, err = resolve_action_spec(
        {"advertises": [{"action": "publish", "requires_approval": True}]}, "publish")
    assert err is None and spec["requires_approval"] is True


# ── 2 · mcp_inventory TTL 缓存 ─────────────────────────────────────────────────

async def test_inventory_cache_hits_within_ttl(monkeypatch):
    from app.services import mcp_inventory as inv

    calls = {"n": 0}

    async def _fake_fetch(server):
        calls["n"] += 1
        return [{"name": "get_login_qrcode", "description": "获取登录二维码"}]

    monkeypatch.setattr(inv, "_fetch_tools", _fake_fetch)
    inv._CACHE.clear()

    class _Srv:
        id = uuid.uuid4()
        name = "xhs-mcp"

    t1 = await inv.get_tools_cached(_Srv)
    t2 = await inv.get_tools_cached(_Srv)
    assert calls["n"] == 1, "TTL 内第二次应命中缓存"
    assert t1 == t2 and t1[0]["name"] == "get_login_qrcode"


async def test_inventory_fetch_failure_returns_empty_not_raise(monkeypatch):
    from app.services import mcp_inventory as inv

    async def _boom(server):
        raise RuntimeError("server down")

    monkeypatch.setattr(inv, "_fetch_tools", _boom)
    inv._CACHE.clear()

    class _Srv:
        id = uuid.uuid4()
        name = "dead-mcp"

    tools = await inv.get_tools_cached(_Srv)
    assert tools == [], "取失败不得抛，返回空"


# ── 3 · worker 提示词注入 MCP 能力清单 ─────────────────────────────────────────

async def test_worker_prompt_includes_mcp_inventory(db_session, monkeypatch):
    from app.models.agent import Agent, AgentMCPServer
    from app.models.skill import MCPServer
    from app.services import agent_service, mcp_inventory as inv
    from app.skills_builtin.context import BuiltinToolContext

    srv = MCPServer(name="xhs-mcp", server_type="http", url="http://x/mcp")
    db_session.add(srv)
    await db_session.flush()
    ag = Agent(name="w-pub", kind="worker", category="custom", capability="xhs_publisher",
               soul_md="发布 worker", protocol_md="")
    db_session.add(ag)
    await db_session.flush()
    db_session.add(AgentMCPServer(agent_id=ag.id, mcp_server_id=srv.id))
    await db_session.commit()

    async def _fake_tools_for_agent(agent):
        return {"xhs-mcp": [
            {"name": "get_login_qrcode", "description": "获取登录二维码"},
            {"name": "publish_content", "description": "发布图文笔记"},
            {"name": "post_comment_to_feed", "description": "发表评论"},
        ]}

    monkeypatch.setattr(inv, "tools_for_agent", _fake_tools_for_agent)

    # 重新加载带 selectinload 的 agent（模拟 executor 路径）
    ag2 = await agent_service.get_agent(db_session, ag.id)
    ctx = BuiltinToolContext(mission_id=None, thread_key=None, agent_node_name="worker",
                             memory_scope="project", db_factory=None)
    prompt = await agent_service.assemble_system_prompt_async(db_session, ag2, ctx)

    assert "get_login_qrcode" in prompt, "提示词应含绑定 MCP 的工具清单"
    assert "post_comment_to_feed" in prompt
    assert "MCP" in prompt
