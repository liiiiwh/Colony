"""内置 Skill 元数据 + 工具注册表。

- `BUILTIN_SKILL_METADATA`：启动时 seed 到 DB（`init_db.seed_builtin_skills`）
- `BUILTIN_TOOL_REGISTRY`：Skill slug → 工具工厂函数（`context -> BaseTool`）
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.skills_builtin.context import BuiltinToolContext


ToolFactory = Callable[["BuiltinToolContext"], Any]


BUILTIN_TOOL_REGISTRY: dict[str, ToolFactory] = {}


BUILTIN_SKILL_METADATA: list[dict] = [
    {
        "slug": "workspace_read",
        "name": "Workspace Read",
        "description": "读取当前分支指定节点的产物内容",
        "builtin_ref": "workspace_read",
    },
    {
        "slug": "workspace_write",
        "name": "Workspace Write",
        "description": "向 Workspace 写入或更新产物（Markdown / JSON / 图片 / 3D 模型等）",
        "builtin_ref": "workspace_write",
    },
    {
        "slug": "workspace_write_batch",
        "name": "Workspace Write Batch",
        "description": "一次性写入多个 artifact 到同一节点（覆盖该节点旧 artifacts）；适合三视图、多附件等多文件交付物场景",
        "builtin_ref": "workspace_write_batch",
    },
    {
        "slug": "workspace_list",
        "name": "Workspace List",
        "description": "列出当前分支所有节点的产物概览",
        "builtin_ref": "workspace_list",
    },
    {
        "slug": "memory_read",
        "name": "Memory Read",
        "description": "读取当前分支当前 Agent 的压缩记忆 memory.md",
        "builtin_ref": "memory_read",
    },
    {
        "slug": "memory_write",
        "name": "Memory Write",
        "description": "（慎用）覆写式更新当前 Agent 记忆——通常应用 memory_append",
        "builtin_ref": "memory_write",
    },
    {
        "slug": "memory_append",
        "name": "Memory Append",
        "description": "追加带时间戳的事件记录到 memory.md（主用：自动落计划进展 / 产物 / 决策）",
        "builtin_ref": "memory_append",
    },
    {
        "slug": "list_models",
        "name": "List LLM Models",
        "description": "列出可用 LLM 模型 UUID（agent_create 必前置；Worker 不要再瞎试 model_id）",
        "builtin_ref": "list_models",
        "category": "builder",
    },
    {
        "slug": "list_providers",
        "name": "List LLM Providers",
        "description": "列出已配置的 LLM Provider（如 nebula / e2e-gemini）",
        "builtin_ref": "list_providers",
        "category": "builder",
    },
    {
        "slug": "s3_upload",
        "name": "S3 Upload",
        "description": "上传文件到对象存储",
        "builtin_ref": "s3_upload",
    },
    {
        "slug": "s3_download",
        "name": "S3 Download",
        "description": "从对象存储下载文件内容",
        "builtin_ref": "s3_download",
    },
    {
        "slug": "s3_list",
        "name": "S3 List",
        "description": "列出对象存储中文件",
        "builtin_ref": "s3_list",
    },
    {
        "slug": "knowledge_search",
        "name": "Knowledge Search",
        "description": "在指定知识库做向量检索，返回 Top-K 相关片段",
        "builtin_ref": "knowledge_search",
    },
    {
        "slug": "knowledge_index",
        "name": "Knowledge Index",
        "description": "将文本写入知识库索引（用于会话过程中动态增量知识）",
        "builtin_ref": "knowledge_index",
    },
    {
        "slug": "list_knowledge_bases",
        "name": "List Knowledge Bases",
        "description": "列出系统内所有可用知识库（id + 名称），配合 knowledge_search 使用",
        "builtin_ref": "list_knowledge_bases",
    },
    {
        "slug": "experience_record",
        "name": "Experience Record",
        "description": (
            "（Builder Supervisor）把一次项目经验归档到 KB 供未来 knowledge_search 召回。"
            "**必须先 request_approval 才能 confirmed=True 真写。**"
        ),
        "builtin_ref": "experience_record",
    },
    {
        "slug": "record_decision",
        "name": "Record Decision",
        "description": (
            "（Supervisor 专用）把用户在节点上的关键选择固化到 workspace.state.decision，"
            "防止重试时忘记已选项再次向用户提问"
        ),
        "builtin_ref": "record_decision",
    },
    {
        "slug": "request_approval",
        "name": "Request Approval",
        "description": "（Supervisor 专用）向用户发起审批请求，等待用户下一轮回复",
        "builtin_ref": "request_approval",
    },
    {
        "slug": "invoke_aux_model",
        "name": "Invoke Aux Model",
        "description": "调用 Agent 绑定的辅助模型（image / video / embedding / chat 等）",
        "builtin_ref": "invoke_aux_model",
    },
    {
        "slug": "parallel_invoke_aux_model",
        "name": "Parallel Invoke Aux Model",
        "description": "批量并行调辅助模型（N 个 prompt 同时提交，自带限流自愈）；多分镜视频 / 多视图图像首选",
        "builtin_ref": "parallel_invoke_aux_model",
    },
    {
        "slug": "merge_videos",
        "name": "Merge Videos",
        "description": "用 ffmpeg 把 N 个 mp4 视频按顺序合并成一个（stream copy 优先，编码不一致回退重编码），上传 S3 返回签名 URL。视频流水线最后一步合成成片",
        "builtin_ref": "merge_videos",
    },
    {
        "slug": "fetch_url",
        "name": "Fetch URL",
        "description": "下载 HTTP(S) URL 内容并返回文本；Agent 读附件/S3 预签名 URL 用",
        "builtin_ref": "fetch_url",
    },
    {
        "slug": "request_structured_input",
        "name": "Request Structured Input",
        "description": "向用户发起结构化表单征询（JSON Schema → 前端渲染 Form）",
        "builtin_ref": "request_structured_input",
    },
    {
        "slug": "archive_to_knowledge",
        "name": "Archive To Knowledge",
        "description": "把当前分支所有交付物索引到指定知识库，用于跨项目经验沉淀",
        "builtin_ref": "archive_to_knowledge",
    },
    # ─── M4 Builder Agent 工具 ───
    {
        "slug": "skill_list_available",
        "name": "Skill List Available",
        "description": "（Builder 选型）搜索 colony 本地 Skill（builtin / installed / custom）；ClawHub 之前的第一步",
        "builtin_ref": "skill_list_available",
        "category": "builder",
    },
    {
        "slug": "mission_get",
        "name": "Mission Get",
        "description": "（Builder 专用）读取 Mission 完整结构（nodes / agents / skills / schedules），EDIT 模式入口",
        "builtin_ref": "mission_get",
        "category": "builder",
    },
    {
        "slug": "mission_create",
        "name": "Mission Create",
        "description": "（Builder 专用）新建一个 Mission（要求 slug 全局唯一）",
        "builtin_ref": "mission_create",
        "category": "builder",
    },
    {
        "slug": "mission_update",
        "name": "Mission Update",
        "description": "（Builder 专用）更新 Mission 的 name / description / supervisor_agent_id",
        "builtin_ref": "mission_update",
        "category": "builder",
    },
    {
        "slug": "mission_delete",
        "name": "Mission Delete",
        "description": "（Builder 危险）删除 Mission 及其全部 nodes / schedules / sessions",
        "builtin_ref": "mission_delete",
        "category": "builder",
    },
    {
        "slug": "agent_create",
        "name": "Agent Create",
        "description": "（Builder 专用）新建 Agent。category 必填；model_id 接受 UUID 或 'provider/model_id' 字符串",
        "builtin_ref": "agent_create",
        "category": "builder",
    },
    {
        "slug": "agent_update",
        "name": "Agent Update",
        "description": (
            "（Builder 专用）更新已有 Agent 的 protocol_md/soul_md/model_id 等字段。"
            "最常用：把 mission_create 自动建的 supervisor 协议改成完整业务链模板。"
        ),
        "builtin_ref": "agent_update",
        "category": "builder",
    },
    {
        "slug": "mcp_server_register",
        "name": "MCP Server Register",
        "description": (
            "（Builder 专用）注册一个 MCP server 到系统。ClawHub mcp-server / static-instruction "
            "类 skill 装完后必须调它（再配套 agent_mcp_bind）才能让 daemon 真正调到 MCP 工具。"
        ),
        "builtin_ref": "mcp_server_register",
        "category": "builder",
    },
    {
        "slug": "agent_mcp_bind",
        "name": "Agent MCP Bind",
        "description": (
            "（Builder 专用）把 MCP server 绑给 agent。daemon 装配 agent 时 langchain-mcp-adapters "
            "自动把 MCP 提供的 tools 暴露给 LLM。和 mcp_server_register 配套使用。"
        ),
        "builtin_ref": "agent_mcp_bind",
        "category": "builder",
    },
    {
        "slug": "agent_aux_model_bind",
        "name": "Agent Aux Model Bind",
        "description": (
            "（Builder 专用）给 agent 绑辅助 LLM 模型并打 role 标记。"
            "出图 worker 必绑 role='image' 模型 / 视频 worker 必绑 role='video' / "
            "embedding worker 必绑 role='embedding'；invoke_aux_model(alias_or_role=...) 按 role 找。"
            "**role 必须与 model_type 匹配**，chat 模型不能拿来出图。"
        ),
        "builtin_ref": "agent_aux_model_bind",
        "category": "builder",
    },
    {
        "slug": "mcp_server_restart",
        "name": "MCP Server Restart",
        "description": (
            "（worker 通用）重启本地 MCP server（http 模式 + 已配 startup_command）。"
            "调 MCP 工具 timeout / connect refused 时调它把服务拉起，再 retry 工具调用。"
            "category=custom：所有 worker 默认绑，protocol 里教 worker 故障自愈。"
        ),
        "builtin_ref": "mcp_server_restart",
        # 故意 category=custom 不是 builder —— 让 auto-bind 默认就给所有 worker，
        # 实现「MCP 失联 → worker 自动调 restart → 重试」这条故障自愈路径。
        "category": "custom",
    },
    {
        "slug": "run_shell",
        "name": "Run Shell (guarded)",
        "description": (
            "（Builder 专用 · ADR-010）守门后执行 shell 命令，用于 auto-shell 自动补救"
            "（装二进制 / 起本地 server）。denylist 硬拦 + LLM 安全门 default-deny + 不可变审计。"
        ),
        "builtin_ref": "run_shell",
        "category": "builder",  # scope=builder（skill_scope 另对 slug 显式兜底）
    },
    {
        "slug": "mcp_ensure_ready",
        "name": "MCP Ensure Ready",
        "description": (
            "（Builder · ADR-010）为 MCP 生成 readiness manifest 并确保就绪：自动拉起本地 server、"
            "补登录需求、对扫码/密钥/条款建人类残留卡 + 暂停。装好 MCP 后调它收尾。"
        ),
        "builtin_ref": "mcp_ensure_ready",
        "category": "builder",
    },
    {
        "slug": "activate_super_first_run",
        "name": "Activate Super First Run",
        "description": (
            "（Builder · ADR-011）建完项目后激活 super 首个运营 session 并挂首跑中继；super 首跑"
            "问账号定位会透传到 Builder 会话问用户、答后回灌 super。"
        ),
        "builtin_ref": "activate_super_first_run",
        "category": "builder",
    },
    {
        "slug": "clawbot_login_start",
        "name": "WeChat Clawbot Login Start",
        "description": (
            "（Builder 专用）启动微信 Clawbot 扫码登录，返回二维码 URL。Builder 在 chat 里"
            "把 URL 给用户扫；扫码后调 clawbot_login_confirm 把账号入库。"
        ),
        "builtin_ref": "clawbot_login_start",
        "category": "builder",
    },
    {
        "slug": "clawbot_login_confirm",
        "name": "WeChat Clawbot Login Confirm",
        "description": (
            "（Builder 专用）阻塞等用户扫码完成，确认后把微信账号入库（凭证 fernet 加密）。"
            "参数：qrcode_session / name / reviewers(可选)。"
        ),
        "builtin_ref": "clawbot_login_confirm",
        "category": "builder",
    },
    {
        "slug": "list_clawbot_accounts",
        "name": "List Clawbot Accounts",
        "description": (
            "（Builder 专用）列已绑定的微信 Clawbot 账号；多项目可共用一个账号。"
        ),
        "builtin_ref": "list_clawbot_accounts",
        "category": "builder",
    },
    {
        "slug": "mission_set_approval_channel",
        "name": "Mission Set Approval Channel",
        "description": (
            "（Builder 专用）配置 worker project 的审批渠道：绑 clawbot 账号 + 项目审批人。"
            "之后 request_approval 会同步发到指定 WeChat 审批人。"
        ),
        "builtin_ref": "mission_set_approval_channel",
        "category": "builder",
    },
    {
        "slug": "wechat_push_notification",
        "name": "WeChat Push Notification",
        "description": (
            "主动推送一段消息到项目绑定的微信审批人（**不创建审批，纯通知**）。"
            "用于数据日报 / 实时告警 / 运行总结。Builder 可以把这个 skill 绑给某 worker，"
            "用 schedule 定时触发让它推送。"
        ),
        "builtin_ref": "wechat_push_notification",
        "category": "custom",
    },
    {
        "slug": "skill_bind",
        "name": "Skill Bind",
        "description": "（Builder 专用）给 Agent 绑一个 Skill",
        "builtin_ref": "skill_bind",
        "category": "builder",
    },
    {
        "slug": "skill_unbind",
        "name": "Skill Unbind",
        "description": "（Builder 专用）给 Agent 解绑一个 Skill",
        "builtin_ref": "skill_unbind",
        "category": "builder",
    },
    {
        "slug": "mission_lifecycle_control",
        "name": "Mission Lifecycle Control",
        "description": "（Builder 专用）控制目标 Mission 的生命周期：start / stop / restart / clear_memory",
        "builtin_ref": "mission_lifecycle_control",
        "category": "builder",
    },
    {
        "slug": "mission_apply_changes",
        "name": "Mission Apply Changes",
        "description": "（Builder 专用）改完 Mission 后调用：默认 restart；clear_memory=True 时一并清记忆",
        "builtin_ref": "mission_apply_changes",
        "category": "builder",
    },
    {
        "slug": "schedule_create",
        "name": "Schedule Create",
        "description": "（Builder 专用）为 Mission 配置 cron / interval / event 触发器",
        "builtin_ref": "schedule_create",
        "category": "builder",
    },
    {
        "slug": "schedule_update",
        "name": "Schedule Update",
        "description": "（Builder 专用）修改一条 Schedule 的 kind / expr / enabled / payload_template",
        "builtin_ref": "schedule_update",
        "category": "builder",
    },
    {
        "slug": "schedule_delete",
        "name": "Schedule Delete",
        "description": "（Builder 专用）删除一条 Schedule",
        "builtin_ref": "schedule_delete",
        "category": "builder",
    },
    # ─── M6 ClawHub 工具 ───
    {
        "slug": "clawhub_search",
        "name": "ClawHub Search",
        "description": "在 ClawHub 搜索 skill（Builder/Installer 选型用）",
        "builtin_ref": "clawhub_search",
        "category": "installer",
    },
    {
        "slug": "clawhub_inspect",
        "name": "ClawHub Inspect",
        "description": "查看 ClawHub skill 详情 + 安全摘要（含 high_risk_tags / blocked）",
        "builtin_ref": "clawhub_inspect",
        "category": "installer",
    },
    {
        "slug": "clawhub_install",
        "name": "ClawHub Install",
        "description": "下载 + 解压 + 镜像 ClawHub skill 到本地；高危 capability 需先 approval",
        "builtin_ref": "clawhub_install",
        "category": "installer",
    },
    {
        "slug": "clawhub_uninstall",
        "name": "ClawHub Uninstall",
        "description": "卸载已安装的 ClawHub skill（按 install_id）",
        "builtin_ref": "clawhub_uninstall",
        "category": "installer",
    },
    {
        "slug": "clawhub_list_installed",
        "name": "ClawHub List Installed",
        "description": "列出已安装的 ClawHub skill",
        "builtin_ref": "clawhub_list_installed",
        "category": "installer",
    },
    # remote_skill_invoke 不再作为独立 skill 行播种：它只是 clawhub 安装件的
    # builtin_ref 执行模板，工厂保留在 BUILTIN_TOOL_REGISTRY（安装件按 ref 查工厂）。
    # ─── M7 Tester 工具 ───
    {
        "slug": "mission_run_test",
        "name": "Mission Run Test",
        "description": "（Tester / Builder）对 project 跑一次 sandbox smoke test + LLM judge",
        "builtin_ref": "mission_run_test",
        "category": "tester",
    },
    # ── L1 输出质量门 ──────────────────────────────────────────────────
    {
        "slug": "output_quality_check",
        "name": "Output Quality Check",
        "description": (
            "（L1 质量门）LLM 评审上游 worker 产物，输出结构化 verdict (pass/warn/block)。"
            "支持 factual_grounding / policy / consistency / safety / freshness checks。"
            "高风险 domain (financial/irreversible/regulated_content) 自动双 judge。"
            "judge LLM 不可用时 fail-open 返回 warn 而非死锁。"
            "供 quality_gate_* worker 专用。"
        ),
        "builtin_ref": "output_quality_check",
        "category": "utility",
    },
    {
        "slug": "output_quality_check_force_override",
        "name": "Output Quality Check Force Override",
        "description": (
            "（L1 高门槛 override）强制覆盖 quality_check verdict。"
            "要求 justification ≥100 字符且必须引用 verdict.issues 中的 evidence；admin 红色显示。"
        ),
        "builtin_ref": "output_quality_check_force_override",
        "category": "utility",
    },
    # ── L2 自调优 (supervisor-only) ─────────────────────────────────
    {
        "slug": "agent_protocol_propose",
        "name": "Agent Protocol Propose",
        "description": (
            "（L2 自调优）提议修改 worker protocol。仅入 proposals 表不改 agent；"
            "supervisor 后续 request_approval → agent_protocol_apply 走审批落库。"
            "**禁止**对自己 agent propose（H15）。"
        ),
        "builtin_ref": "agent_protocol_propose",
        "category": "utility",
    },
    {
        "slug": "agent_protocol_apply",
        "name": "Agent Protocol Apply",
        "description": (
            "（L2 自调优）把 pending proposal 应用到 agent + 写 history + 抓 metrics_baseline。"
            "必须 confirmed=True；24h 内同 agent ≤3 次 apply（H4）。"
        ),
        "builtin_ref": "agent_protocol_apply",
        "category": "utility",
    },
    {
        "slug": "agent_protocol_revert",
        "name": "Agent Protocol Revert",
        "description": "（L2 自调优）回退 agent protocol 到指定 history version，默认上一版。",
        "builtin_ref": "agent_protocol_revert",
        "category": "utility",
    },
    {
        "slug": "agent_protocol_evaluate",
        "name": "Agent Protocol Evaluate",
        "description": (
            "（L2 自调优）apply 后评估 quality_gate pass-rate 变化；"
            "delta < -0.1 且 samples ≥ 5 → 建议 revert。"
        ),
        "builtin_ref": "agent_protocol_evaluate",
        "category": "utility",
    },
    # ── L3 升级到 Builder ──────────────────────────────────────────
    {
        "slug": "mission_escalate_to_builder",
        "name": "Mission Escalate To Builder",
        "description": (
            "（L3 supervisor-only）向 origin Builder Chat session 发升级信封（quota / dedup / fire-and-forget）。"
            "summary≤280 / evidence≤4KB；3/day/项目；同 fingerprint 同天 dedup；"
            "超 3 条 unresolved 自动暂停 schedule（H7）。"
        ),
        "builtin_ref": "mission_escalate_to_builder",
        "category": "utility",
    },
    {
        "slug": "mission_escalation_resolve",
        "name": "Mission Escalation Resolve",
        "description": "（L3 Builder Chat 专用）处理完一条 project_escalation 后闭环。",
        "builtin_ref": "mission_escalation_resolve",
        "category": "builder",
    },
    {
        "slug": "mission_escalation_dismiss",
        "name": "Mission Escalation Dismiss",
        "description": "（L3）取消一条 escalation（supervisor / admin 主动）。",
        "builtin_ref": "mission_escalation_dismiss",
        "category": "utility",
    },
    {
        "slug": "mission_escalation_list",
        "name": "Mission Escalation List",
        "description": "（L3）列自己项目最近 N 条 escalation 状态（supervisor 决策时参考）。",
        "builtin_ref": "mission_escalation_list",
        "category": "utility",
    },
    # ── v3 super-only dispatch + worker IO ────────────────────────
    {"slug": "invoke_worker", "name": "Invoke Worker",
     "description": "（super-only v3）调度一个平台 worker（capability:slug 或 agent_id）+ 持久 thread 加载。",
     "builtin_ref": "invoke_worker", "category": "utility"},
    {"slug": "invoke_workers_parallel", "name": "Invoke Workers Parallel",
     "description": "（super-only v3）并发调多个 worker；同 worker 串行 (V53)。",
     "builtin_ref": "invoke_workers_parallel", "category": "utility"},
    {"slug": "list_workers", "name": "List Workers",
     "description": "（super-only v3）查平台 worker 目录（按 capability 过滤 + 分页）。",
     "builtin_ref": "list_workers", "category": "utility"},
    {"slug": "request_new_capability", "name": "Request New Capability",
     "description": "（super-only v3）缺 capability 时升级 Builder；super 自动 paused_waiting_capability。",
     "builtin_ref": "request_new_capability", "category": "utility"},
    {"slug": "report_worker_issue", "name": "Report Worker Issue",
     "description": "（super-only ADR-009）现有 worker 反复失败时上报 Builder 求修，默认停工等修；Builder 修好 resume 唤醒。",
     "builtin_ref": "report_worker_issue", "category": "utility"},
    {"slug": "optimization_continue", "name": "Optimization Continue",
     "description": "（work-order ADR-025）worker 优化未完成时入队续跑；有 force_human 卡则拒绝；非 work-order 无效。",
     "builtin_ref": "optimization_continue", "category": "utility"},
    {"slug": "optimization_done", "name": "Optimization Done",
     "description": "（work-order ADR-025）worker 优化完成时软关闭本 mission + 按 capability 唤醒等待者 + 注销调度；非 work-order 无效。",
     "builtin_ref": "optimization_done", "category": "utility"},
    # v6.B · super-only · Q7 mismatch 重定向（当 mission 与自己能力不匹配时引导用户去其它 super）
    {"slug": "list_supers", "name": "List Supers (peers)",
     "description": "（super-only v6.B）查平台其它 super 候选；按 keyword 模糊；自动排除自己。Q7 mismatch 重定向第一步。",
     "builtin_ref": "list_supers", "category": "utility"},
    {"slug": "emit_redirect_suggestion", "name": "Emit Redirect Suggestion",
     "description": "（super-only v6.B）当 mission goal 不适合自己时调；写一张 redirect 卡到 chat 流 + 起 REDIRECT activity；用户选跳哪去/继续/取消。",
     "builtin_ref": "emit_redirect_suggestion", "category": "utility"},
    {"slug": "return_result", "name": "Return Result",
     "description": "（worker-only v3）输出契约：text / structured / artifact_bytes_b64 / needs_clarification。",
     "builtin_ref": "return_result", "category": "utility"},
    # ── v3 Builder-only（DESIGN_WORKER 模式收尾用） ────────────────
    {"slug": "resume_super_agent", "name": "Resume Super Agent",
     "description": "（Builder-only v3）唤醒 paused 的 super：lifecycle → running + 立即触发 1 tick + 关 pending escalation。",
     "builtin_ref": "resume_super_agent", "category": "builder"},
    {"slug": "validate_backward_compat", "name": "Validate Backward Compat",
     "description": "（Builder-only v3）R9 升级 worker 前 dry-run capability_contract 兼容校验。",
     "builtin_ref": "validate_backward_compat", "category": "builder"},
    {"slug": "release_work_claim", "name": "Release Work Claim",
     "description": "（Builder-only ADR-009）处理完某 worker/super/skill 后释放本 session 的处理锁，让其它 session 接手。",
     "builtin_ref": "release_work_claim", "category": "builder"},
    {"slug": "create_skill_from_template", "name": "Create Skill From Template",
     "description": "（Builder-only ADR-009）从白名单模板（http_api_call/mcp_proxy/prompt_macro）创建新 skill，不跑任意代码；补 build_* 缺失 skill。",
     "builtin_ref": "create_skill_from_template", "category": "builder"},
    {"slug": "promote_to_platform", "name": "Promote to Platform KB",
     "description": "v6 · 把一条经验/规则推到平台共享 KB，所有 super 都能 search。super/Builder 都可调。",
     "builtin_ref": "promote_to_platform", "category": "utility"},
    {"slug": "platform_knowledge_search", "name": "Platform Knowledge Search",
     "description": "v6 · 仅查平台共享 KB（跨 project 经验复用）。knowledge_search 已含项目 KB。",
     "builtin_ref": "platform_knowledge_search", "category": "utility"},
]
