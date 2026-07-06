"""ADR-010 R3 · LLM 安全门输出解析（纯逻辑）。

快模型返回常含噪声/代码围栏；解析必须稳，且**解析失败一律 default-deny**
（误判为 allow 是灾难）。
"""
from app.services.shell_judge import parse_judge_response


def test_garbage_defaults_deny():
    assert parse_judge_response("我觉得应该没问题吧")["allow"] is False


def test_clean_allow():
    r = parse_judge_response('{"allow": true, "reason": "本地启动命令"}')
    assert r["allow"] is True
    assert "本地启动" in r["reason"]


def test_clean_deny():
    assert parse_judge_response('{"allow": false, "reason": "可疑外联"}')["allow"] is False


def test_fenced_json_extracted():
    r = parse_judge_response('```json\n{"allow": true, "reason": "ok"}\n```')
    assert r["allow"] is True


def test_empty_defaults_deny():
    assert parse_judge_response("")["allow"] is False
    assert parse_judge_response(None)["allow"] is False


# ─────────── ADR-030 · 审批感知 + 可编辑提示词 ───────────


class _FakeLLM:
    """捕获 ainvoke 收到的 messages，返回固定放行。"""
    def __init__(self):
        self.seen = None

    async def ainvoke(self, messages):
        self.seen = messages
        class _R:
            content = '{"allow": true, "reason": "ok"}'
        return _R()


def test_default_system_prompt_exported():
    """默认提示词需可被 settings 兜底引用（后台可编辑，缺省回落到它）。"""
    from app.services.shell_judge import JUDGE_DEFAULT_SYSTEM_PROMPT
    assert "shell" in JUDGE_DEFAULT_SYSTEM_PROMPT.lower() or "命令" in JUDGE_DEFAULT_SYSTEM_PROMPT


def test_judge_uses_editable_system_prompt():
    """make_shell_judge 接受 system_prompt 覆盖（后台可编辑的提示词注入）。"""
    import asyncio
    from langchain_core.messages import SystemMessage
    from app.services.shell_judge import make_shell_judge

    llm = _FakeLLM()
    judge = make_shell_judge(llm, system_prompt="自定义门提示词-XYZ")
    asyncio.run(judge("echo hi", None))
    sys_msgs = [m for m in llm.seen if isinstance(m, SystemMessage)]
    assert sys_msgs and "自定义门提示词-XYZ" in sys_msgs[0].content


def test_judge_input_includes_verified_approvals():
    """ADR-030 核心：judge 输入携带 DB 已核实的用户审批记录（可信来源）→ 已批准的操作可放行。"""
    import asyncio
    from langchain_core.messages import HumanMessage
    from app.services.shell_judge import make_shell_judge

    llm = _FakeLLM()
    approvals = [{"title": "授权安装小红书 MCP", "option": "同意安装", "message": "clone+build+启动"}]
    judge = make_shell_judge(llm, approvals=approvals)
    asyncio.run(judge("git clone https://x/xhs-mcp && go build", "安装用户已批准的 MCP"))
    human = [m for m in llm.seen if isinstance(m, HumanMessage)]
    blob = " ".join(m.content for m in human)
    assert "授权安装小红书 MCP" in blob and "同意安装" in blob


def test_judge_no_approvals_still_works():
    """无审批记录时（approvals=None）行为不变，不报错。"""
    import asyncio
    from app.services.shell_judge import make_shell_judge
    llm = _FakeLLM()
    judge = make_shell_judge(llm)
    r = asyncio.run(judge("ls", None))
    assert r["allow"] is True
