"""ADR-028 D2 → ADR-031 修订：MCP 安装链从 Builder 协议**移到 MCP Installer worker**。

Builder 不再自己 run_shell/register/ensure_ready，而是 invoke_worker(capability:mcp_installer)
委派。startup_command / QR / install→register→ensure_ready 的硬规则现落在 Installer 协议里。

纯文本断言协议源码字面量，不实跑 LLM/DB。
"""
from __future__ import annotations

import inspect

from app.db import init_db
from app.db.system_agent_prompts import MCP_INSTALLER_PROTOCOL


def _builder_protocol_text() -> str:
    return inspect.getsource(init_db.seed_builder_project)


# ── Builder 侧：委派，不再自己碰 shell/MCP ──

def test_builder_delegates_mcp_to_installer():
    text = _builder_protocol_text()
    assert "mcp_installer" in text, "Builder 协议须委派给 mcp_installer"
    assert "invoke_worker(capability:mcp_installer" in text, "须用 capability dispatch 委派"


# (Builder 实际解绑 run_shell/mcp_* 的行为由 test_adr031_mcp_installer::test_builder_unbound_from_shell_mcp_tools 在 DB 层断言。)


# ── Installer 侧：完整安装链的硬规则现在在这里 ──

def test_installer_protocol_has_full_chain():
    p = MCP_INSTALLER_PROTOCOL
    assert "run_shell" in p
    assert "mcp_server_register" in p and "startup_command" in p
    assert "agent_mcp_bind" in p
    assert "mcp_ensure_ready" in p


def test_installer_protocol_links_startup_command_to_qr():
    p = MCP_INSTALLER_PROTOCOL
    assert "startup_command" in p
    assert ("QR" in p or "二维码" in p or "qr" in p)


def test_installer_protocol_requires_consent_first():
    """安装前先 request_approval 征得同意（shell 门据此放行 — ADR-030）。"""
    p = MCP_INSTALLER_PROTOCOL
    assert "request_approval" in p
