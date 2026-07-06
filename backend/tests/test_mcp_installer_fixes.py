"""grill 2026-07-01 · mcp_installer 修复：run_shell 平台默认模型 / 忽略 human-only / worker 过程落库。"""
from __future__ import annotations
import pytest
pytestmark = pytest.mark.asyncio


def test_installer_protocol_overrides_human_only_setup():
    """installer 协议须硬性要求：忽略 SETUP 里 human-only 说法，自己 run_shell 装。"""
    from app.db.system_agent_prompts import MCP_INSTALLER_PROTOCOL as p
    assert "run_shell" in p
    # 关键：明确「文档说 human-only 也要自己执行」+「只有 QR 登录是人类的事」
    assert ("human-only" in p) or ("manual install" in p) or ("just docs" in p)
    assert "QR" in p
    assert ("Never" in p and "card" in p)  # 禁止把可安装步骤 punt 成人类卡


def test_run_shell_no_longer_hard_requires_model_id():
    """run_shell 源码不再对 agent.model_id 缺失直接报 'Cannot locate ... model'（改走平台默认解析）。"""
    import inspect
    from app.skills_builtin.builder import builder_skills
    src = inspect.getsource(builder_skills.run_shell_tool)
    assert "_resolve_agent_model" in src, "run_shell 应用 _resolve_agent_model 兜底 model_id=None"
    assert "not agent.model_id" not in src, "不应再因 model_id 为空直接拒"


async def test_persist_worker_trace_writes_tool_steps(monkeypatch):
    """__persist_worker_trace 把 AI tool_call + ToolMessage 落成前端可重建的 agent_log（meta 形状正确）。"""
    import contextlib
    from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
    import app.skills_builtin.super.super_dispatch_skills as sd
    import app.services.messaging_service as ms

    calls = []
    async def _fake_append(db, mission_id, thread_key, *, role, content, meta=None):
        calls.append({"role": role, "content": content, "meta": meta or {}})
    monkeypatch.setattr(ms, "append_message", _fake_append)

    class _FakeFactory:
        def __call__(self):
            @contextlib.asynccontextmanager
            async def _cm():
                yield object()
            return _cm()

    msgs = [
        HumanMessage(content="do it"),
        AIMessage(content="let me call the tool", tool_calls=[{"name": "run_shell", "args": {"command": "ls"}, "id": "tc1"}]),
        ToolMessage(content="file1\nfile2", tool_call_id="tc1"),
        AIMessage(content="done"),
    ]
    trace_fn = getattr(sd, "__persist_worker_trace")
    await trace_fn(_FakeFactory(), "mid", "worker::x", msgs, 1, "w1")

    ets = [c["meta"].get("event_type") for c in calls]
    assert "tool-input-available" in ets
    assert "tool-output-available" in ets
    # tool-call meta.raw 形状（前端按 toolCallId 配对重建卡）
    ti = next(c for c in calls if c["meta"].get("event_type") == "tool-input-available")
    assert ti["meta"]["raw"]["toolName"] == "run_shell"
    assert ti["meta"]["raw"]["toolCallId"] == "tc1"
