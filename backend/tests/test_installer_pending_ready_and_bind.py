"""2026-07-06 · installer 快返 pending-ready + Builder 强制必绑（协议契约）。

e2e 实证两处：
1. installer 前台跑 go build/launch >invoke 超时(600s) + Builder tick 墙钟(900s) → 整个
   构建被装机拖垮，回退后没轮到建 worker。修：installer 后台起服务 + 快返 pending-ready
   （QR 就绪交给平台 finalize 驱动），不阻塞 invoke。
2. Builder 建完 worker 漏 agent_mcp_bind → worker 用不上 MCP。修：协议强化"建完必绑" +
   gate 结构化硬查（test_build_completeness_gate.py 已覆盖 unbound_mcp）。
"""
from __future__ import annotations


def test_installer_protocol_backgrounds_and_returns_pending():
    """installer 协议：后台起服务、快返 pending-ready、QR 交平台，不前台阻塞。"""
    from app.db.system_agent_prompts import MCP_INSTALLER_PROTOCOL as p

    low = p.lower()
    assert "background" in low or "后台" in p or "nohup" in low, "应指示后台起服务"
    assert "pending" in low or "pending-ready" in low, "应快返 pending-ready 状态"
    # 明确不在 installer 前台阻塞等 QR（QR 由平台 finalize 驱动）
    assert "mcp_server_id" in p


def test_builder_protocol_enforces_bind_after_worker():
    """Builder install-first 协议：建完 worker 必 agent_mcp_bind，且点明 gate 会硬查。"""
    import inspect

    from app.db import init_db

    src = inspect.getsource(init_db)
    assert "agent_mcp_bind" in src
    # 强化措辞：gate 现在结构化强制"必绑"（装了没绑会被退回）
    assert "must bind" in src.lower() or "必绑" in src or "bind every" in src.lower()
