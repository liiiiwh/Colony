"""Tiered self-repair for MCP tool calls.

An MCP server going unreachable used to either silently drop the tool (load time) or bubble a
raw error to the LLM (call time) with no healing. This wraps each MCP tool so a failure is
handled in tiers:

  1. **Retry / reconnect** — retry the call a few times (each MCP call opens a fresh session, so a
     retry naturally reconnects); for local servers, attempt an autostart respawn first.
  2. **Report to Worker-Optimization** — on persistent failure, append a lightweight degradation
     signal to the Colony Worker Optimization mission (no per-call LLM turn) so the worker-opt
     super addresses it on its next tick.
  3. **Escalate to Builder** — a load-time total outage (server unreachable + autostart failed)
     escalates to Builder to fix the integration/binding.

The wrapper returns a structured error string to the LLM rather than raising, so the agent learns
the tool is degraded (and that self-repair was triggered) and can adapt this turn.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_RETRIES = 2


def _image_block_to_markdown(block: dict) -> str | None:
    """图片 content block → `![image](data:...)` markdown；不是图片块返回 None。"""
    btype = block.get("type")
    if btype == "image_url":
        url = ((block.get("image_url") or {}).get("url")
               if isinstance(block.get("image_url"), dict) else block.get("image_url"))
        if isinstance(url, str) and url:
            return f"![image]({url})"
        return None
    if btype == "image":
        # langchain-mcp-adapters create_image_block: {"type":"image","base64"/"url","mime_type"}；
        # MCP 原生: {"type":"image","data","mimeType"}。两种都吃。
        data = block.get("base64") or block.get("data")
        mime = block.get("mime_type") or block.get("mimeType") or "image/png"
        if isinstance(data, str) and data:
            return f"![image](data:{mime};base64,{data})"
        url = block.get("url")
        if isinstance(url, str) and url:
            return f"![image]({url})"
    return None


def sanitize_mcp_result(res):
    """MCP 工具结果的多模态消毒（2026-07-05 get_login_qrcode 实证）。

    图片 content block（image_url / MCP 原生 image）回喂 LLM 会炸非多模态模型
    （litellm.BadRequestError: unknown variant 'image_url'，DeepSeek 家族）。转成
    `![image](data:<mime>;base64,...)` markdown 文本：worker LLM 可读可转述，
    落会话后前端内联渲染（markdown-viewer urlTransform 放行 data:image）。
    无图片块的结果原样返回，不动结构。"""
    # adapter 工具 response_format='content_and_artifact' → (content, artifact) 元组：
    # 消毒 content，artifact 原样（首次实证：只认 list 让 tuple 漏过，错误依旧）。
    if isinstance(res, tuple) and len(res) == 2:
        return (sanitize_mcp_result(res[0]), res[1])
    if not isinstance(res, list):
        return res
    has_image = any(
        isinstance(b, dict) and b.get("type") in ("image", "image_url") for b in res
    )
    if not has_image:
        return res
    parts: list[str] = []
    for b in res:
        if isinstance(b, dict):
            md = _image_block_to_markdown(b)
            if md is not None:
                parts.append(md)
                continue
            if b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
                continue
            parts.append(str(b))
        else:
            parts.append(str(b))
    return "\n\n".join(p for p in parts if p)


async def _report_to_worker_opt(server_name: str, err: Exception, ctx: Any) -> None:
    """Tier 2 — best-effort lightweight signal to the worker-opt mission. Never raises."""
    try:
        if ctx is None or getattr(ctx, "db_factory", None) is None:
            return
        from app.services import worker_health_service
        async with ctx.db_factory() as db:
            await worker_health_service.record_worker_issue(
                db,
                capability=f"mcp:{server_name}",
                evidence=f"MCP 工具调用持续失败：{type(err).__name__}: {err}",
                severity="warn",
                source="mcp_self_repair",
            )
    except Exception:
        logger.exception("[mcp_self_repair] report_to_worker_opt failed (不阻塞)")


def wrap_tool_coroutine(
    orig_coro,
    *,
    tool_name: str,
    server_name: str,
    ctx: Any,
    retries: int = _DEFAULT_RETRIES,
    sleep=asyncio.sleep,
):
    """Return a coroutine wrapping `orig_coro` with retry → report-to-worker-opt. Pure enough to
    unit-test: inject `sleep` to avoid real backoff and a fake `orig_coro` to drive failures."""

    async def _wrapped(*args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return sanitize_mcp_result(await orig_coro(*args, **kwargs))
            except Exception as e:  # noqa: BLE001 — any MCP transport/tool error
                last_exc = e
                logger.warning(
                    "[mcp_self_repair] %s@%s call failed (attempt %d/%d): %s",
                    tool_name, server_name, attempt + 1, retries + 1, e,
                )
                if attempt < retries:
                    await sleep(0.5 * (attempt + 1))  # backoff; next call opens a fresh session
        # exhausted → tier 2
        await _report_to_worker_opt(server_name, last_exc, ctx)
        return (
            f"⚠️ MCP 工具 `{tool_name}`（server={server_name}）调用失败，已自动重试 {retries} 次并"
            f"上报 Colony Worker Optimization 自修复。错误：{type(last_exc).__name__}: {last_exc}。"
            f"请改用其它可用工具继续，或稍后再试该 MCP。"
        )

    return _wrapped


def wrap_mcp_tools(tools: list, *, ctx: Any, server_of=None) -> list:
    """Mutate each MCP tool's coroutine in place with the self-repair wrapper (preserves the
    tool's name/description/args_schema so the LLM contract is unchanged). `server_of(tool)` maps
    a tool to its server name; defaults to the tool name."""
    for t in tools:
        orig = getattr(t, "coroutine", None)
        if orig is None:
            continue
        name = getattr(t, "name", "mcp_tool")
        server = (server_of(t) if server_of else None) or name
        t.coroutine = wrap_tool_coroutine(orig, tool_name=name, server_name=server, ctx=ctx)
    return tools
