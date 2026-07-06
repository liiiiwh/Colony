"""内置 Skill 工具注册表。

每个工具导出为 `tool_factory(context: BuiltinToolContext) -> BaseTool` 工厂函数。
运行时在 `services/agent_service.py::build_agent_executor` 中按 Agent 绑定的 Skill
选择工厂生成 BaseTool 并注入 LangChain `create_agent`（v0.4.1 起）。

当前注册 27 个内置 Skill：
- workspace_read / workspace_write / workspace_write_batch / workspace_list
- memory_read / memory_write
- s3_upload / s3_download / s3_list
- knowledge_search / knowledge_index / list_knowledge_bases / archive_to_knowledge
- set_branch_description / record_decision / rollback_to_node / request_approval
- request_structured_input
- invoke_aux_model / fetch_url
"""

from __future__ import annotations

from app.skills_builtin.llm.aux_model_skills import (
    invoke_aux_model_tool,
    parallel_invoke_aux_model_tool,
)
from app.skills_builtin.llm.media_skills import merge_videos_tool
from app.skills_builtin.builder.builder_skills import (  # noqa: E501
    agent_aux_model_bind_tool,
    agent_create_tool,
    agent_mcp_bind_tool,
    agent_update_tool,
    activate_super_first_run_tool,
    mcp_ensure_ready_tool,
    mcp_server_register_tool,
    mcp_server_restart_tool,
    mission_apply_changes_tool,
    mission_create_tool,
    mission_delete_tool,
    mission_get_tool,
    run_shell_tool,
    mission_lifecycle_control_tool,
    mission_update_tool,
    schedule_create_tool,
    schedule_delete_tool,
    schedule_update_tool,
    skill_bind_tool,
    skill_list_available_tool,
    skill_unbind_tool,
)
from app.skills_builtin.channel.wechat_push_skills import wechat_push_notification_tool
from app.skills_builtin.channel.clawbot_skills import (
    clawbot_login_confirm_tool,
    clawbot_login_start_tool,
    list_clawbot_accounts_tool,
    mission_set_approval_channel_tool,
)
from app.skills_builtin.channel.clawhub_skills import (
    clawhub_install_tool,
    clawhub_inspect_tool,
    clawhub_list_installed_tool,
    clawhub_search_tool,
    clawhub_uninstall_tool,
    remote_skill_invoke_tool,
)
from app.skills_builtin.context import BuiltinToolContext
from app.skills_builtin.builder.tester_skills import mission_run_test_tool
from app.skills_builtin.channel.fetch_skills import fetch_url_tool
from app.skills_builtin.knowledge.experience_skills import experience_record_tool
from app.skills_builtin.knowledge.knowledge_skills import (
    knowledge_index_tool,
    knowledge_search_tool,
    list_knowledge_bases_tool,
)
from app.skills_builtin.llm.llm_skills import list_models_tool, list_providers_tool
from app.skills_builtin.worker_io.memory_skills import (
    memory_append_tool,
    memory_read_tool,
    memory_write_tool,
)
from app.skills_builtin.builder.escalation_skills import (
    mission_escalate_to_builder_tool,
    mission_escalation_dismiss_tool,
    mission_escalation_list_tool,
    mission_escalation_resolve_tool,
)
from app.skills_builtin.quality.quality_skills import (
    output_quality_check_force_override_tool,
    output_quality_check_tool,
)
from app.skills_builtin.registry import BUILTIN_SKILL_METADATA, BUILTIN_TOOL_REGISTRY
from app.skills_builtin.builder.self_tune_skills import (
    agent_protocol_apply_tool,
    agent_protocol_evaluate_tool,
    agent_protocol_propose_tool,
    agent_protocol_revert_tool,
)
from app.skills_builtin.super.super_dispatch_skills import (
    emit_redirect_suggestion_tool,
    invoke_worker_tool,
    invoke_workers_parallel_tool,
    list_supers_tool,
    list_workers_tool,
    report_worker_issue_tool,
    request_new_capability_tool,
)
from app.skills_builtin.super.worker_opt_skills import (
    optimization_continue_tool,
    optimization_done_tool,
)
from app.skills_builtin.worker_io.worker_io_skills import return_result_tool
from app.skills_builtin.builder.builder_lifecycle_skills import (
    resume_super_agent_tool,
    validate_backward_compat_tool,
)
from app.skills_builtin.builder.builder_factory_skills import (
    create_skill_from_template_tool,
    release_work_claim_tool,
)
from app.skills_builtin.knowledge.knowledge_promote_skills import (
    platform_knowledge_search_tool,
    promote_to_platform_tool,
)
from app.skills_builtin.worker_io.s3_skills import s3_download_tool, s3_list_tool, s3_upload_tool
from app.skills_builtin.super.supervisor_skills import (
    archive_to_knowledge_tool,
    record_decision_tool,
    request_approval_tool,
    request_structured_input_tool,
)
from app.skills_builtin.worker_io.workspace_skills import (
    workspace_list_tool,
    workspace_read_tool,
    workspace_write_batch_tool,
    workspace_write_tool,
)

# 注册全部工厂
BUILTIN_TOOL_REGISTRY.update(
    {
        "workspace_read": workspace_read_tool,
        "workspace_write": workspace_write_tool,
        "workspace_write_batch": workspace_write_batch_tool,
        "workspace_list": workspace_list_tool,
        "memory_read": memory_read_tool,
        "memory_write": memory_write_tool,
        "memory_append": memory_append_tool,
        "list_models": list_models_tool,
        "list_providers": list_providers_tool,
        "s3_upload": s3_upload_tool,
        "s3_download": s3_download_tool,
        "s3_list": s3_list_tool,
        "knowledge_search": knowledge_search_tool,
        "knowledge_index": knowledge_index_tool,
        "list_knowledge_bases": list_knowledge_bases_tool,
        "experience_record": experience_record_tool,
        "record_decision": record_decision_tool,
        "request_approval": request_approval_tool,
        "request_structured_input": request_structured_input_tool,
        "archive_to_knowledge": archive_to_knowledge_tool,
        "invoke_aux_model": invoke_aux_model_tool,
        "parallel_invoke_aux_model": parallel_invoke_aux_model_tool,
        "merge_videos": merge_videos_tool,
        "fetch_url": fetch_url_tool,
        # M4 Builder Agent 工具
        "skill_list_available": skill_list_available_tool,
        "mission_get": mission_get_tool,
        "mission_create": mission_create_tool,
        "mission_update": mission_update_tool,
        "mission_delete": mission_delete_tool,
        "agent_create": agent_create_tool,
        "agent_update": agent_update_tool,
        "skill_bind": skill_bind_tool,
        "skill_unbind": skill_unbind_tool,
        "mcp_server_register": mcp_server_register_tool,
        "mcp_server_restart": mcp_server_restart_tool,
        "mcp_ensure_ready": mcp_ensure_ready_tool,
        "activate_super_first_run": activate_super_first_run_tool,
        "run_shell": run_shell_tool,
        "agent_mcp_bind": agent_mcp_bind_tool,
        "agent_aux_model_bind": agent_aux_model_bind_tool,
        "clawbot_login_start": clawbot_login_start_tool,
        "clawbot_login_confirm": clawbot_login_confirm_tool,
        "list_clawbot_accounts": list_clawbot_accounts_tool,
        "mission_set_approval_channel": mission_set_approval_channel_tool,
        "wechat_push_notification": wechat_push_notification_tool,
        "mission_lifecycle_control": mission_lifecycle_control_tool,
        "mission_apply_changes": mission_apply_changes_tool,
        "schedule_create": schedule_create_tool,
        "schedule_update": schedule_update_tool,
        "schedule_delete": schedule_delete_tool,
        # M6 ClawHub
        "clawhub_search": clawhub_search_tool,
        "clawhub_inspect": clawhub_inspect_tool,
        "clawhub_install": clawhub_install_tool,
        "clawhub_uninstall": clawhub_uninstall_tool,
        "clawhub_list_installed": clawhub_list_installed_tool,
        "remote_skill_invoke": remote_skill_invoke_tool,
        # M7 Tester
        "mission_run_test": mission_run_test_tool,
        # L1 输出质量门（Factory 自动给副作用节点前插 quality_gate worker）
        "output_quality_check": output_quality_check_tool,
        "output_quality_check_force_override": output_quality_check_force_override_tool,
        # L2 自调优（supervisor-only：propose → approve → apply → evaluate → auto-revert）
        "agent_protocol_propose": agent_protocol_propose_tool,
        "agent_protocol_apply": agent_protocol_apply_tool,
        "agent_protocol_revert": agent_protocol_revert_tool,
        "agent_protocol_evaluate": agent_protocol_evaluate_tool,
        # L3 升级到 Builder（quota / dedup / fire-and-forget 投递）
        "mission_escalate_to_builder": mission_escalate_to_builder_tool,
        "mission_escalation_resolve": mission_escalation_resolve_tool,
        "mission_escalation_dismiss": mission_escalation_dismiss_tool,
        "mission_escalation_list": mission_escalation_list_tool,
        # v3 super-only dispatch + worker IO
        "invoke_worker": invoke_worker_tool,
        "invoke_workers_parallel": invoke_workers_parallel_tool,
        "list_workers": list_workers_tool,
        "request_new_capability": request_new_capability_tool,
        "report_worker_issue": report_worker_issue_tool,
        # ADR-025 · work-order 自驱续跑/收尾（work-order mission 自守卫）
        "optimization_continue": optimization_continue_tool,
        "optimization_done": optimization_done_tool,
        "return_result": return_result_tool,
        # v6.B · super-only · Q7 mismatch 重定向
        "list_supers": list_supers_tool,
        "emit_redirect_suggestion": emit_redirect_suggestion_tool,
        # v3 Builder-only
        "resume_super_agent": resume_super_agent_tool,
        "validate_backward_compat": validate_backward_compat_tool,
        # ADR-009 G4 · Builder 多 session 互斥锁释放
        "release_work_claim": release_work_claim_tool,
        # ADR-009 G6 · Builder 受限模板化 skill 创建
        "create_skill_from_template": create_skill_from_template_tool,
        # v6 · 平台共享经验 KB (super/Builder 都可调)
        "promote_to_platform": promote_to_platform_tool,
        "platform_knowledge_search": platform_knowledge_search_tool,
    }
)


__all__ = [
    "BUILTIN_SKILL_METADATA",
    "BUILTIN_TOOL_REGISTRY",
    "BuiltinToolContext",
]
