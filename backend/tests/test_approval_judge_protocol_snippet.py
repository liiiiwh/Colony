"""ADR-028 D1 · 「先 invoke approval_judge 再 request_approval」协议片段。

抽成可复用 fragment（system_agent_prompts.APPROVAL_JUDGE_PROTOCOL_SNIPPET），
同时进入：
- Builder super-design 协议（init_db Builder protocol）
- mission_create 自动建 super 的默认 protocol_md（builder_skills 的 default_protocol）
"""
from __future__ import annotations


def test_snippet_fragment_exists_and_is_context_driven():
    """ADR-028 D1（修订）· snippet 教 super：人工门由平台(approval_judge)自动判，super 只填 context，
    不再手调 judge、无 force_human 参数。"""
    from app.db.system_agent_prompts import APPROVAL_JUDGE_PROTOCOL_SNIPPET

    s = APPROVAL_JUDGE_PROTOCOL_SNIPPET
    assert "approval_judge" in s
    assert "context" in s
    # 修订后不再让 super 主动传播 force_human（旧 'force_human=must_human' propagation 模式已移除）
    assert "force_human=must_human" not in s
    assert "invoke_worker('capability:approval_judge'" not in s  # super 不再手调 judge


def test_mission_create_default_protocol_includes_snippet():
    """mission_create 自动建 super 时，默认 protocol_md 必须嵌入 approval_judge 门规则。

    通过读取 builder_skills 源码确认 default_protocol 引用了 fragment（避免实跑 LLM/DB）。
    """
    import inspect

    from app.skills_builtin.builder import builder_skills

    src = inspect.getsource(builder_skills.mission_create_tool)
    assert "APPROVAL_JUDGE_PROTOCOL_SNIPPET" in src, (
        "mission_create 默认 protocol 必须 include APPROVAL_JUDGE_PROTOCOL_SNIPPET"
    )


def test_judge_policy_read_display_ops_never_gate():
    """2026-07-05 用户决议：读/展示类操作（取登录二维码、查登录状态、拉数据）不出审批卡——
    直接执行并把结果渲染进会话，扫码本身就是用户动作，无需先「批准展示」。
    旧规则把 'scan a QR' 列为硬停点 → 每次取码弹一张卡（e2e 实证连弹 3 张）。"""
    from app.db.system_agent_prompts import _APPROVAL_JUDGE_PROTOCOL as p

    assert "Read / display operations" in p or "read/display" in p.lower(), \
        "协议必须有读/展示类免卡规则"
    assert "must_human=false" in p
    # 取码/查状态被明确点名为免卡
    assert "QR" in p and ("render" in p or "display" in p.lower())
    # 硬停点 1 不再把「展示二维码给用户扫」当成必须人工
    assert "merely displaying" in p.lower() or "NOT this" in p
