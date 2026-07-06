"""Supervisor 专用工具：审批 / 调度 Worker / 并行派发 / 表单征询 / 知识沉淀。

（ADR-018 step5/X：set_branch_description / rollback_to_node 已删 —— rewind 自 ADR-006 废弃，
mission 单一 workspace 无分支可回退/切换。）
- `request_approval`：向 SSE 推 data-approval-request 事件 + 落库为 agent_log 消息
- `request_structured_input`：向用户发起结构化表单征询（JSON Schema → 前端渲染）
- `archive_to_knowledge`：把当前分支的交付物索引到指定知识库
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid

from langchain_core.tools import StructuredTool

from app.services import mission_service, messaging_service
from app.skills_builtin.context import BuiltinToolContext

logger = logging.getLogger(__name__)


def _extract_first_json_object(raw: str) -> str | None:
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def _repair_loose_json_quotes(raw: str) -> str:
    def _next_non_whitespace(index: int) -> str:
        for i in range(index + 1, len(raw)):
            char = raw[i]
            if not char.isspace():
                return char
        return ""

    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char != '"':
            result.append(char)
            continue
        if not in_string:
            in_string = True
            result.append(char)
            continue
        next_char = _next_non_whitespace(index)
        is_closing_quote = next_char in {":", ",", "}", "]"}
        if is_closing_quote:
            in_string = False
            result.append(char)
            continue
        result.append(r'\"')
    return "".join(result)


def _normalize_approval_message(message: str) -> str:
    """LLM 偶尔吐出来的结构化审批 JSON 单引号 / 转义不规范，做一次容错修复。

    判定"是结构化审批"的条件（项目无关，不写死特定 type 字面量）：
    - 消息含一个 JSON 对象块
    - 解析后是 dict 且有 `type: str` 字段（任何字符串都行）
    符合就重新规范化吐出 minified JSON；不符合原样返回不动。
    """
    trimmed = (message or "").strip()
    if "{" not in trimmed:
        return message
    candidate = _extract_first_json_object(trimmed)
    if not candidate:
        return message
    for payload in (candidate, _repair_loose_json_quotes(candidate)):
        try:
            parsed = json.loads(payload)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        # 只修结构化审批（带 type 字段），普通 markdown 里的 `{...}` 例子不动
        if not isinstance(parsed.get("type"), str):
            continue
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return message


def record_decision_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """（Supervisor 专用）把用户做的关键选择固化到当前 thread（messages，meta.type='decision'）。

    为什么需要：
    - 用户在面对审批/approval 时做的选择（"基础档" / "拒绝" / "修改为 XXX"）对后续流程至关重要
    - 旧行为：没有地方记录这个选择。一旦后续某步失败、用户要求重试，Supervisor 会重新读
      上下文 → 看不到用户已选 → 重新让用户再次选择（用户被迫重复说"基础档"N 次）
    - 写入 thread decision 消息后，Supervisor 下轮加载 thread 历史就能看到用户已选，按既定
      选项继续，不再重问

    ADR-027 D3：退役 by-node workspace 簿记。决策只活在 thread（与 request_approval 的
    auto_decision 落消息一致），不再写 `workspace[node].state.decision`。

    Supervisor 使用时机：
    - 每次用户对 `request_approval` 回复之后、推进下一步**之前**，把用户选择记录下来
    - 用户在对话里手动给出关键参数（例如"改成 150 元以内"）也应 record_decision，
      details 里带上新数值

    数据结构（落 message.meta）：
      {"type": "decision", "topic": <str>, "value": <str>, "recorded_at": <ISO8601>, "details": <dict|null>}

    Supervisor 协议层还是可以主动选择**不**记录（有些决定是暂时的/临时的）。
    """
    from datetime import UTC, datetime

    async def _record(
        topic: str,
        decision: str,
        details: dict | None = None,
    ) -> str:
        if ctx.mission_id is None or ctx.db_factory is None:
            return "❌ 工具上下文缺失（mission_id / db_factory）"
        if not topic or not decision:
            return "❌ topic 和 decision 均不能为空"
        recorded_at = datetime.now(UTC).isoformat()
        async with ctx.db_factory() as db:
            await messaging_service.append_message(
                db,
                ctx.mission_id,
                ctx.thread_key,
                role="agent_log",
                content=f"[decision] {topic} → {decision}",
                meta={
                    "type": "decision",
                    "topic": str(topic),
                    "value": str(decision),
                    "recorded_at": recorded_at,
                    "details": details or None,
                },
            )
        logger.info(
            "📝 record_decision: mission=%s topic=%s decision=%s",
            ctx.mission_id,
            topic,
            str(decision)[:80],
        )
        # 前端可以用这个事件刷新决策角标；未接也不影响
        await ctx.emit(
            {
                "type": "data-decision-recorded",
                "data": {
                    "topic": str(topic),
                    "value": str(decision),
                    "details": details or None,
                },
            }
        )
        return (
            f"✅ 已记录用户选择「{topic}」：{decision}。"
            "后续加载 thread 历史会看到这条 decision；不要再对同一个选择重复向用户提问。"
        )

    return StructuredTool.from_function(
        coroutine=_record,
        name="record_decision",
        description=(
            "（Supervisor 专用）把用户做的关键选择固化到当前 thread，防止后续重试时忘记用户已选。\n"
            "参数：topic(str，被决策的主题，如 'plan_tier') / decision(str，用户选择的规范化值，"
            "如 '基础档' '拒绝' '同意') / details(dict 可选，额外上下文如 {bom_ceiling: 150})。\n"
            "**务必调用时机**：用户对 request_approval / 自然语言表达了关键选择之后、推进下一步之前。"
            "同一 topic 可多次 record_decision，最新一条为准。"
        ),
    )


def request_approval_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _request(
        title: str,
        message: str,
        options: list[str] | None = None,
        context: str = "",
    ) -> str:
        if ctx.mission_id is None or ctx.db_factory is None:
            return "❌ 工具上下文缺失"
        message = _normalize_approval_message(message)
        opts = options or ["同意", "拒绝"]
        # 用短 ID（8 字符），方便微信审批人回复时手敲
        from app.services import pending_approval_service as _pa
        request_id = _pa._short_request_id()

        # ADR-028 D1（修订）· 是否必须真人审批由 **approval_judge 唯一裁决**（不再接收 force_human）。
        # request_approval 服务端自动咨询 approval_judge（喂上下文 + auto_approve 开启状态），
        # must_human=True → 凌驾 auto_approve 强制停；False → 按 auto_approve 走。
        # 把"咨询+套用"做成确定性内部步骤，避免 super 忘传 force_human 导致人工门被 auto 放行。
        # ctx.extra['force_auto_approve']：系统级后台会话（无真人盯卡）强制 auto，但 must_human 仍压过。
        from app.domain.auto_approve import resolve_auto_approve
        from app.services import approval_judge_service
        project_auto = False
        must_human = True
        if ctx.mission_id is not None:
            async with ctx.db_factory() as db:
                project = await mission_service.get_mission(db, ctx.mission_id)
                project_auto = bool(project and project.auto_approve)
                if project is not None:
                    must_human, _jr = await approval_judge_service.judge_must_human(
                        db, project, title=title, message=message, options=opts,
                        auto_approve_on=project_auto, context=context,
                    )
        auto_approve = resolve_auto_approve(
            must_human=must_human,
            ctx_force_auto=bool((ctx.extra or {}).get("force_auto_approve")),
            project_auto_approve=project_auto,
        )

        # auto_approve 时挑"肯定/推进"项，**不盲取 options[0]**——选项由 LLM 自由生成、顺序无约束，
        # 盲取第一个会在"取消/放弃"被放前时点错（卡死/白跑）。见 domain/auto_approve。
        from app.domain.auto_approve import pick_auto_option
        auto_opt = pick_auto_option(opts) if auto_approve else opts[0]

        payload = {
            "request_id": request_id,
            "title": title,
            "message": message,
            "options": opts,
            "auto_approved": auto_approve,
            "auto_approved_option": auto_opt if auto_approve else None,
        }
        await ctx.emit({"type": "data-approval-request", "data": payload})
        async with ctx.db_factory() as db:
            await messaging_service.append_message(
                db,
                ctx.mission_id,
                ctx.thread_key,
                role="agent_log",
                content=(
                    f"[审批请求] {title}\n\n{message}\n\n选项：{' / '.join(opts)}"
                    + (f"\n\n⚡ 已自动通过（auto_approve=true）→ 选项 '{auto_opt}'" if auto_approve else "")
                ),
                meta={"type": "approval_request", **payload},
            )
        logger.info(
            "📝 request_approval: %s (auto_approved=%s)", title, auto_approve
        )

        # ⭐ daemon 模式下没人看 SSE 卡片；落 pending_approvals 一等公民 + 可选发微信
        # orchestrator 模式也落，便于 observe 页 / WeChat 渠道统一审批（不影响原 SSE 流程）
        if not auto_approve and ctx.mission_id is not None:
            try:
                async with ctx.db_factory() as db_pa:
                    # ADR-025 D3 · 走 create_pending 统一落卡：去重 + 微信分发 + **暂停 mission
                    # (paused_clarification)**。复用业务 request_id；dedup 命中时以返回行为准。
                    row = await _pa.create_pending(
                        db_pa,
                        mission_id=ctx.mission_id,
                        title=title,
                        message=message,
                        options=list(opts),
                        thread_key=ctx.thread_key,
                        agent_node_name=ctx.agent_node_name,
                        dispatch_wechat=True,
                        request_id=request_id,
                    )
                    # ADR-029 · approval_request 的 SSE 推送已收敛到规范点 create_pending
                    # （带重放缓冲、覆盖所有调用方）——此处不再重复 publish。
                    _ = row
            except Exception:  # noqa: BLE001
                logger.exception("[request_approval] 落 pending_approvals 失败（不阻塞）")
        if auto_approve:
            # E15：auto_approve 自动 record_decision，避免 Supervisor 重读 workspace 看不到这次决策
            if ctx.agent_node_name:
                try:
                    async with ctx.db_factory() as db_rec:
                        await messaging_service.append_message(
                            db_rec,
                            ctx.mission_id,
                            ctx.thread_key,
                            role="agent_log",
                            content=f"[auto_decision] {title} → {auto_opt}",
                            meta={
                                "type": "decision",
                                "request_id": request_id,
                                "title": title,
                                "option": auto_opt,
                                "auto_approved": True,
                            },
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("[request_approval] auto_approve 落 decision 失败（继续）")
            return (
                f"✅ 项目设置 auto_approve=true，已自动选择 '{auto_opt}'（已 record_decision）。"
                f"请继续下一个 tool call，不要等待用户输入。"
            )
        # 卡片已到前端，立刻提示 _drive_llm 提前终止本轮 ReAct 循环——
        # 避免 LLM 再 iterate 一轮生成"请在卡片上选择"那种 7-8 秒废话回复，
        # 也避免"supervisor 说一段 + 弹卡 + 又说一段"的 UX。
        if ctx.cancel_event is not None:
            ctx.cancel_event.set()
        return (
            f"✅ 已向用户发起审批请求「{title}」（request_id={request_id}）。"
            f"请结束本轮回复，等待用户在下一条消息中选择："
            f"{' / '.join(opts)}。"
        )

    return StructuredTool.from_function(
        coroutine=_request,
        name="request_approval",
        description=(
            "向用户发起审批请求。参数：title(str，简短标题) / message(str，详细说明) /"
            " options(list[str]，可选，默认 ['同意','拒绝']，**第一项放推进/肯定项**) /"
            " context(str，可选，**强烈建议填**：这次审批的背景，尤其'用户要求过发布前必须人工确认'"
            "/'这是不可逆外发(发帖/付款)'/'跑到X条件停下问我'这类——系统据此判定是否必须真人)。\n"
            "**是否必须真人审批由系统(approval_judge)自动判定，你无需也无法手动指定**：\n"
            "- 判为必须人工（不可逆外发 / 用户要求人审 / 阻塞需人介入）→ 无视 auto_approve 强制停等真人；\n"
            "- 否则按项目 auto_approve：开则自动选最肯定项直接返回(不等用户)，关则等用户。\n"
            "你只管把背景在 context/message 里讲清楚，停不停交给系统裁决。"
        ),
    )


# ============================================================
# request_structured_input：向用户发起结构化表单征询
# ============================================================
def request_structured_input_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """让用户一次性填写 / 补全多字段结构化信息。

    典型场景：
    - 需求 7 字段缺失 → 表单展示已填项 + 空项，用户补齐后提交
    - 方案档位确认 → 单选按钮（但 request_approval 更合适）

    参数：
    - title: 表单标题（前端 Dialog 顶部）
    - description: 给用户的说明文案
    - schema: JSON Schema 定义字段（properties / required / fieldLabels）
    - prefilled: 当前已填的值（用于回显、用户修改）
    - submit_label: 提交按钮文字（默认"提交"）

    行为：
    - 推送 data-form-request SSE，前端渲染 shadcn Form
    - 落库为 agent_log 消息（可供后续审计）
    - Agent **必须在本轮回复里结束**，等待用户下一条消息（带 form_response）
    """

    async def _request(
        title: str,
        description: str,
        schema: dict,
        prefilled: dict | None = None,
        submit_label: str = "提交",
    ) -> str:
        if ctx.mission_id is None or ctx.db_factory is None:
            return "❌ 工具上下文缺失"
        request_id = str(_uuid.uuid4())
        payload = {
            "request_id": request_id,
            "title": title,
            "description": description,
            "schema": schema,
            "prefilled": prefilled or {},
            "submit_label": submit_label,
        }
        await ctx.emit({"type": "data-form-request", "data": payload})
        async with ctx.db_factory() as db:
            await messaging_service.append_message(
                db,
                ctx.mission_id,
                ctx.thread_key,
                role="agent_log",
                content=f"[表单请求] {title}",
                meta={"type": "form_request", **payload},
            )
            # ADR-018 mission-only · 删 ADR-011 首跑中继（builder-chat-as-session 模型已退役；
            # relay_to_session_id 随 sessions 表删除）。表单在本 mission thread 内征询即可。
        logger.info("📋 request_structured_input: %s (%d fields)", title, len(schema.get("properties", {})))
        # 同 request_approval：卡片已到前端，提前终止本轮 LLM 循环。
        if ctx.cancel_event is not None:
            ctx.cancel_event.set()
        return (
            f"✅ 已向用户发起表单「{title}」（request_id={request_id}）。"
            f"请结束本轮回复，等待用户在下一轮消息中提交表单数据。"
            f"用户提交后，其消息 meta 将含 form_response={{request_id, values}}。"
        )

    return StructuredTool.from_function(
        coroutine=_request,
        name="request_structured_input",
        description=(
            "向用户发起【结构化表单】征询，一次性收集/修正多字段信息（例如需求 7 字段的追问）。\n"
            "参数：title(str) / description(str) / schema(JSON Schema dict，含 properties/required/fieldLabels) / "
            "prefilled(dict 可选，当前已填值) / submit_label(str，默认'提交')。\n"
            "调用后本轮必须结束等待用户响应。"
        ),
    )


# ============================================================
# archive_to_knowledge：把当前分支的交付物索引到知识库
# ============================================================
def archive_to_knowledge_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """把当前 Mission 所有交付物索引到指定知识库，供未来项目参考。

    功能（ADR-027：交付物只活在 S3，按 capability 归档）：
    1. list_objects(colony/workspace/{mission_id}/) 列出所有交付物
    2. 下载文本类（md/txt/json/html）内容，调 knowledge_service.index_text 写入
    3. 二进制（图片/视频/模型）跳过

    参数：
    - kb_id: 目标知识库 UUID
    - include_metadata: 预留（当前 S3 来源无 by-node 元信息可带；保留参数以免破坏调用方）
    """

    async def _archive(kb_id: str, include_metadata: bool = True) -> str:
        if ctx.mission_id is None or ctx.db_factory is None:
            return "❌ 工具上下文缺失"
        try:
            kb_uuid = _uuid.UUID(kb_id)
        except (TypeError, ValueError):
            return "❌ kb_id 非合法 UUID"

        from app.services import knowledge_service
        from app.services.storage_service import get_storage

        text_exts = {"md", "txt", "json", "html"}
        ext_to_type = {"md": "markdown", "txt": "text", "json": "json", "html": "html"}
        storage = get_storage()
        prefix = f"colony/workspace/{ctx.mission_id}/"
        objects = [o for o in await storage.list_objects(prefix) if o.get("key")]

        async with ctx.db_factory() as db:
            mission = await mission_service.get_mission(db, ctx.mission_id)
            if not mission:
                return "❌ Mission 不存在"
            kb = await knowledge_service.get_knowledge_base(db, kb_uuid)
            if not kb:
                return f"❌ 知识库 {kb_id} 不存在"

            indexed_chunks = 0
            errors: list[str] = []
            for o in objects:
                key = o["key"]
                filename = key.rsplit("/", 1)[-1]
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if ext not in text_exts:
                    continue  # 跳过图片等二进制
                rest = key[len(prefix):]
                capability = rest.split("/", 1)[0] if "/" in rest else "default"
                try:
                    body = await storage.download(key)
                    content = body.decode("utf-8")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{key}: 下载/解码失败 {exc}")
                    continue
                if not content.strip():
                    continue
                doc_title = f"[{capability}] {filename}"
                try:
                    await knowledge_service.index_text(
                        db,
                        kb,
                        title=doc_title,
                        text=content,
                        metadata={
                            "thread_key": ctx.thread_key,
                            "capability": capability,
                            "s3_key": key,
                            "artifact_type": ext_to_type.get(ext, "text"),
                        },
                    )
                    indexed_chunks += 1
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
                    logger.exception("archive_to_knowledge 索引失败")

        logger.info(
            "📚 archive_to_knowledge: kb=%s thread=%s indexed=%d errors=%d",
            kb_id,
            ctx.thread_key,
            indexed_chunks,
            len(errors),
        )
        if errors:
            return (
                f"⚠️ 已索引 {indexed_chunks} 条；{len(errors)} 条失败：\n"
                + "\n".join(errors[:5])
            )
        return f"✅ 已把当前 Mission {indexed_chunks} 条交付物索引到知识库 {kb_id}"

    return StructuredTool.from_function(
        coroutine=_archive,
        name="archive_to_knowledge",
        description=(
            "把当前 Mission 所有交付物（S3 上的 deliverable artifacts）索引到指定知识库，供未来项目检索参考。\n"
            "参数：kb_id(str，知识库 UUID) / include_metadata(bool，默认 True，预留)。\n"
            "仅处理 markdown/text/json/html 类产物；image 等二进制跳过。"
        ),
    )
