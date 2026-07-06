"""修 mission-7a9afb 暴露的 MCP 假 ready：
- create_mcp_server 落 startup_command（原漏传 → 恒 None）
- mcp_server_register 工具能收 startup_command
- mcp_ensure_ready 对「本地 MCP 无 startup_command」硬失败（不再假 ready）
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_create_mcp_server_persists_startup_command(db_session):
    from app.schemas.skill import MCPServerCreate
    from app.services import skill_service
    s = await skill_service.create_mcp_server(db_session, MCPServerCreate(
        name="xhs", server_type="http", url="http://localhost:18060/mcp",
        startup_command=["./xhs-mcp"], startup_cwd="/tmp/xhs"))
    assert s.startup_command == ["./xhs-mcp"]
    assert s.startup_cwd == "/tmp/xhs"


async def test_register_tool_accepts_startup_command(db_session, _patched_session_local):
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.context import BuiltinToolContext
    from app.skills_builtin.builder.builder_skills import mcp_server_register_tool
    ctx = BuiltinToolContext(mission_id=None, thread_key="main", agent_node_name="builder",
                             db_factory=AsyncSessionLocal)
    res = await mcp_server_register_tool(ctx).coroutine(
        name="xhs-local", server_type="http", url="http://localhost:18070/mcp",
        startup_command=["./xhs-mcp"], startup_cwd="/tmp/xhs")
    assert res["ok"] is True
    # 回读落库确认 startup_command 真的存了
    from sqlalchemy import select
    from app.models.skill import MCPServer
    import uuid
    row = await db_session.get(MCPServer, uuid.UUID(res["mcp_server_id"]))
    assert row is not None and row.startup_command == ["./xhs-mcp"]


async def test_register_local_without_startup_command_rejected(db_session, _patched_session_local):
    """本地 MCP（localhost url）无 startup_command → 拒（否则平台没法拉起 → 后续假 ready）。"""
    from app.db.session import AsyncSessionLocal
    from app.skills_builtin.context import BuiltinToolContext
    from app.skills_builtin.builder.builder_skills import mcp_server_register_tool
    ctx = BuiltinToolContext(mission_id=None, thread_key="main", agent_node_name="builder",
                             db_factory=AsyncSessionLocal)
    res = await mcp_server_register_tool(ctx).coroutine(
        name="xhs-nolaunch", server_type="http", url="http://localhost:18080/mcp")
    assert res["ok"] is False
    assert res.get("error_code") == "LOCAL_NEEDS_STARTUP_COMMAND"


async def test_ensure_ready_local_without_startup_command_hard_fails(db_session, _patched_session_local):
    """本地部署 + 无 startup_command → mcp_ensure_ready 硬失败，不再假 ready。"""
    from app.db.session import AsyncSessionLocal
    from app.schemas.skill import MCPServerCreate
    from app.services import skill_service
    from app.skills_builtin.context import BuiltinToolContext
    from app.skills_builtin.builder.builder_skills import mcp_ensure_ready_tool
    s = await skill_service.create_mcp_server(db_session, MCPServerCreate(
        name="xhs-noready", server_type="http", url="http://localhost:18090/mcp"))
    ctx = BuiltinToolContext(mission_id=None, thread_key="main", agent_node_name="builder",
                             db_factory=AsyncSessionLocal)
    res = await mcp_ensure_ready_tool(ctx).coroutine(mcp_server_id=str(s.id), deployment="local")
    assert res["ok"] is False
    assert res.get("error_code") == "NO_STARTUP_COMMAND"
