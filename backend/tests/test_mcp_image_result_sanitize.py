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
async def test_wrapped_coroutine_no_ctx_falls_back_to_inline():
    """无 ctx（拿不到会话）→ 退回内联 markdown，至少不崩、不丢图。"""
    async def _orig(**kw):
        return [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]

    wrapped = wrap_tool_coroutine(_orig, tool_name="get_login_qrcode",
                                  server_name="xhs-mcp", ctx=None)
    out = await wrapped()
    assert isinstance(out, str) and f"data:image/png;base64,{B64}" in out


class _FakeCtx:
    """带 db_factory + mission_id 的最小 ctx，用于验证图片旁路发会话。"""
    def __init__(self, mission_id, published):
        import uuid as _u
        self.mission_id = mission_id if isinstance(mission_id, _u.UUID) else _u.UUID(str(mission_id))
        self.thread_key = "worker-sub"
        self._published = published

        class _DB:
            async def __aenter__(_s): return _s
            async def __aexit__(_s, *a): return False
        self._db = _DB()

    def db_factory(self):
        return self._db


@pytest.mark.asyncio
async def test_content_and_artifact_tool_keeps_tuple_shape(monkeypatch):
    """回归（2026-07-06 实证）：langchain-mcp-adapters 用 response_format='content_and_artifact'，
    工具必须返回 2-元组 (content, artifact)。图片旁路后若返回纯字符串占位符 → LangChain 报
    'Since response_format=content_and_artifact a two-tuple is expected' → worker 调用直接失败。
    修：原始是元组时，占位符也要保元组形状 (placeholder, None)。"""
    import uuid

    from app.services import messaging_service

    async def _noop_append(db, mission_id, thread_key, role, content, **kw):
        pass
    monkeypatch.setattr(messaging_service, "append_message", _noop_append)

    async def _orig(**kw):
        # adapter 形状：(content_blocks, artifact)
        return ([{"type": "image", "base64": B64, "mime_type": "image/png"}], {"structured": 1})

    ctx = _FakeCtx(uuid.uuid4(), [])
    wrapped = wrap_tool_coroutine(_orig, tool_name="get_login_qrcode",
                                  server_name="xiaohongshu-mcp", ctx=ctx)
    out = await wrapped()
    assert isinstance(out, tuple) and len(out) == 2, "content_and_artifact 工具必须回 2-元组"
    assert isinstance(out[0], str) and B64 not in out[0], "content 侧是不含 base64 的占位符"


@pytest.mark.asyncio
async def test_image_published_as_artifact_placeholder_to_llm(monkeypatch):
    """2026-07-06 用户实证：get_login_qrcode 的 5000+ 字符 base64 被 worker/super LLM
    逐 token 吐出来（浪费 token + 可能被改坏 + 不瞬时）。修：MCP 工具返回图片 → **旁路
    直接发到 super 主会话渲染**，只给 LLM 一个**不含 base64** 的短占位符。"""
    import uuid

    from app.services import messaging_service

    calls = []

    async def _fake_append(db, mission_id, thread_key, role, content, **kw):
        calls.append({"mission_id": mission_id, "thread_key": thread_key, "content": content,
                      "meta": kw.get("meta")})

    monkeypatch.setattr(messaging_service, "append_message", _fake_append)

    async def _orig(**kw):
        return [
            {"type": "text", "text": "扫码登录"},
            {"type": "image", "base64": B64, "mime_type": "image/png"},
        ]

    mid = uuid.uuid4()
    ctx = _FakeCtx(mid, calls)
    wrapped = wrap_tool_coroutine(_orig, tool_name="get_login_qrcode",
                                  server_name="xiaohongshu-mcp", ctx=ctx)
    out = await wrapped()

    # 给 LLM 的返回：短占位符，绝不含 base64
    assert isinstance(out, str)
    assert B64 not in out, "base64 绝不能进 LLM 上下文（会被逐 token 吐 + 改坏 + 烧 token）"
    assert "二维码" in out or "图片" in out or "image" in out.lower()

    # 图片旁路直接发到会话（含 data:image，前端渲染）
    assert len(calls) == 1, "应有且仅有一条图片 artifact 消息发到会话"
    assert f"data:image/png;base64,{B64}" in calls[0]["content"]
    assert calls[0]["thread_key"] == "main", "发到 super 主会话（用户看的地方），不是 worker 子线程"
    assert str(mid) == str(calls[0]["mission_id"])
