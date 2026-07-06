"""MCP 工具清单 · 进程内 TTL 缓存（2026-07-05 xhs_publisher 能力双盲事故）。

绑定了 MCP 的 worker 运行时工具全量注入，但 super（派发决策）和 worker 提示词
（能力认知）都看不见清单 → super 断言 worker「只会 publish」。本模块提供
read-time 新鲜的工具清单：assemble_system_prompt / list_workers 消费。

- TTL 缓存（默认 10 分钟）：MCP 本地服务，tools/list 便宜，但每 tick 都打一次没必要。
- 取失败**永不抛**：回上次缓存值，没有则空列表（server down 不能炸提示词组装）。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: server_id → (monotonic_ts, tools)
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL_SEC = 600.0
#: 描述截断（提示词里一行一个工具，不要长文）
_DESC_MAX = 200


async def _fetch_tools(server) -> list[dict]:
    """连 server 取 tools/list → [{name, description}]。可抛（由 get_tools_cached 兜）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    if server.server_type == "http":
        if not server.url:
            return []
        conn = {"url": server.url, "transport": "streamable_http",
                "headers": getattr(server, "headers", None) or {}}
    elif server.server_type == "stdio":
        cmd = server.command
        if not cmd:
            return []
        conn = {
            "command": cmd[0] if isinstance(cmd, list) else cmd,
            "args": cmd[1:] if isinstance(cmd, list) and len(cmd) > 1 else [],
            "transport": "stdio",
            "env": getattr(server, "env_vars", None) or {},
        }
    else:
        return []
    client = MultiServerMCPClient({server.name: conn})
    tools = await client.get_tools()
    out = []
    for t in tools:
        desc = (t.description or "").strip().splitlines()
        out.append({"name": t.name, "description": (desc[0] if desc else "")[:_DESC_MAX]})
    return out


async def get_tools_cached(server) -> list[dict]:
    """server 的工具清单（TTL 缓存；失败回旧值/空，不抛）。"""
    key = str(server.id)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and (now - hit[0]) < _TTL_SEC:
        return hit[1]
    try:
        tools = await _fetch_tools(server)
    except Exception:  # noqa: BLE001
        logger.warning("[mcp_inventory] 取 %s 工具清单失败（回缓存/空）", server.name, exc_info=True)
        return hit[1] if hit is not None else []
    _CACHE[key] = (now, tools)
    return tools


async def tools_for_agent(agent) -> dict[str, list[dict]]:
    """agent 绑定的全部 enabled MCP → {server_name: [{name, description}]}。"""
    out: dict[str, list[dict]] = {}
    for b in list(getattr(agent, "mcp_servers", None) or []):
        m = getattr(b, "mcp_server", None)
        if m is None or not m.is_enabled:
            continue
        tools = await get_tools_cached(m)
        if tools:
            out[m.name] = tools
    return out
