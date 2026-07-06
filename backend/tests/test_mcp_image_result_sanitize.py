"""MCP 工具返回图片块 → 文本化消毒（2026-07-05 get_login_qrcode 实证）。

事故：execute 兜底链全通（可见性→审批→派发→worker 真调 get_login_qrcode），
但 MCP 返回的二维码是 image_url/image content block，回喂 worker LLM 时
litellm.BadRequestError: unknown variant 'image_url'（DeepSeek 家族不吃多模态
tool 结果）→ 最后一米翻车。

修：wrap_mcp_tools 成功路径上 sanitize——图片块转 `![image](data:<mime>;base64,...)`
markdown 文本（worker LLM 可读、落会话后前端 urlTransform 已能内联渲染）。
"""
from __future__ import annotations

import pytest

from app.services.mcp_self_repair import sanitize_mcp_result, wrap_tool_coroutine

B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_image_url_block_becomes_markdown_text():
    res = [
        {"type": "text", "text": "扫码登录"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
    ]
    out = sanitize_mcp_result(res)
    assert isinstance(out, str)
    assert "image_url" not in out or "![image](" in out  # 不残留 LLM 不认识的块结构
    assert f"data:image/png;base64,{B64}" in out
    assert "扫码登录" in out


def test_mcp_raw_image_block_becomes_markdown_text():
    res = [{"type": "image", "data": B64, "mimeType": "image/png"}]
    out = sanitize_mcp_result(res)
    assert isinstance(out, str)
    assert f"data:image/png;base64,{B64}" in out


def test_langchain_adapter_image_block_base64_key():
    """langchain-mcp-adapters 实际形状（2026-07-05 二次实证）：create_image_block →
    {"type":"image","base64":...,"mime_type":...}（不是 data/mimeType）。"""
    res = [{"type": "image", "base64": B64, "mime_type": "image/png"}]
    out = sanitize_mcp_result(res)
    assert isinstance(out, str)
    assert f"data:image/png;base64,{B64}" in out


def test_langchain_adapter_image_block_url_key():
    res = [{"type": "image", "url": "https://x/qr.png", "mime_type": "image/png"}]
    out = sanitize_mcp_result(res)
    assert isinstance(out, str) and "![image](https://x/qr.png)" in out


def test_content_and_artifact_tuple_sanitized():
    """adapter 工具 response_format='content_and_artifact' → coroutine 返回
    (content, artifact) 元组——消毒 content、artifact 原样保留（首次实证时
    消毒器只认 list，tuple 直接漏过 → 错误依旧）。"""
    content = [{"type": "image", "base64": B64, "mime_type": "image/png"}]
    artifact = {"structured": True}
    out = sanitize_mcp_result((content, artifact))
    assert isinstance(out, tuple) and len(out) == 2
    assert isinstance(out[0], str) and f"data:image/png;base64,{B64}" in out[0]
    assert out[1] is artifact


def test_plain_string_passthrough():
    assert sanitize_mcp_result("hello") == "hello"


def test_list_without_images_passthrough():
    res = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    out = sanitize_mcp_result(res)
    assert out == res, "无图片块的结果不动（保持原始结构）"


@pytest.mark.asyncio
async def test_wrapped_coroutine_sanitizes_success_result():
    async def _orig(**kw):
        return [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]

    wrapped = wrap_tool_coroutine(_orig, tool_name="get_login_qrcode",
                                  server_name="xhs-mcp", ctx=None)
    out = await wrapped()
    assert isinstance(out, str) and f"data:image/png;base64,{B64}" in out
