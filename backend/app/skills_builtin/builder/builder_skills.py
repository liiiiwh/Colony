"""M4: Builder Agent 的内置工具集（meta 层）。

让 Builder AI 能在 /orchestrator 对话里直接创建 / 修改 / 启停其他 project，
而不用用户离开 chat 自己去 admin 后台点。

M4 阶段最小集 + 2026-05-18 补充：
- mission_get（**新增** EDIT 模式：读 project 完整结构）
- mission_create
- mission_update
- mission_delete
- agent_create
- skill_bind
- skill_unbind
- mission_lifecycle_control（start / stop / restart / clear_memory）
- mission_apply_changes（restart project；可选清记忆）
- schedule_create / schedule_update / schedule_delete（**新增** 配 cron / interval / event）

M6 之后会再加 clawhub_search / clawhub_install；M7 加 mission_run_test。
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from langchain_core.tools import StructuredTool

from app.skills_builtin.context import BuiltinToolContext

logger = logging.getLogger(__name__)


# ─────────────────────────── mission_create ───────────────────────────
def mission_create_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        name: str,
        slug: str,
        description: str = "",
        supervisor_agent_id: str = "",
        supervisor_model_id: str = "",
        supervisor_name: str = "",
        supervisor_soul_md: str = "",
        supervisor_protocol_md: str = "",
    ) -> dict:
        """Create a new Mission (**supports creating supervisor + project in one step**, breaks the "chicken-and-egg" deadlock).

        Two modes:
        1. **Pass supervisor_agent_id** (existing supervisor agent): used directly as project.supervisor
        2. **Pass supervisor_model_id** (recommended): the tool internally auto-creates a default supervisor agent,
           then creates the project; the return gives both mission_id and supervisor_agent_id

        Args:
            name: Mission name (≤128 characters)
            slug: URL slug (lowercase letters + digits + hyphens, globally unique)
            description: Short description
            supervisor_agent_id: UUID of an existing supervisor (choose one of this or supervisor_model_id)
            supervisor_model_id: Model identifier (UUID / 'provider_name/model_id' / bare model_id).
                                 Left empty + supervisor_agent_id also empty → use settings.DEFAULT_SUPERVISOR_MODEL_ID
            supervisor_name: Name of the auto-created supervisor agent (default `{slug}-supervisor`)
            supervisor_soul_md: Override the default soul_md when auto-creating the supervisor (required for projects with approval / complex business chains)
            supervisor_protocol_md: Override the default protocol_md when auto-creating the supervisor
                                    (**strongly recommended to pass** a complete template when there is approval / multiple triggers / business chains;
                                    left empty falls back to the generic "dispatch worker" default protocol)
        """
        from sqlalchemy import select

        from app.core.config import settings
        from app.schemas.agent import AgentCreate
        from app.schemas.mission import MissionCreate
        from app.services import agent_service, mission_service
        from app.skills_builtin.llm.llm_skills import resolve_model_id

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        # **前置校验**：acting_user_id 必须有，否则 supervisor auto-create 会落库但
        # mission_create 失败，留下孤儿 agent + 浪费 LLM 重试
        created_by_raw = ctx.extra.get("acting_user_id")
        if created_by_raw is None:
            return {"ok": False, "error": "Missing acting_user_id context (cannot determine created_by)"}
        try:
            created_by = uuid.UUID(str(created_by_raw))
        except (ValueError, TypeError):
            return {"ok": False, "error": f"acting_user_id={created_by_raw!r} is not a valid UUID"}
        async with ctx.db_factory() as db:
            # ── 单 project 不变量：一个 builder mission 只建一个项目；已建过 → 复用，绝不再建 ──
            # ADR-018 step5/S · 复用标记从 session.target_project_id 改成 provenance：
            # 找 supervisor.built_by_mission_id == 本 builder mission 的项目。
            try:
                if ctx.mission_id is not None:
                    from app.models.agent import Agent as _Agent
                    from app.models.mission import Mission as _Proj
                    _bproj = await db.get(_Proj, ctx.mission_id)
                    # 当前 tick 跑在「Builder 设计会话」里？判定 = supervisor 是 Builder
                    # (category='builder')，**而非** slug=='builder'。
                    # 漏判 bug（mission-df779b 事故）：用户 +新建 的设计会话 slug != 'builder'，
                    # 旧 `slug=='builder'` 守卫被跳过 → Builder 在弯路后又调一次 mission_create
                    # 就建出第二个 mission + 第二张「创建完成」CTA。与 build_finalizer 同一判据。
                    _bsup = (
                        await db.get(_Agent, _bproj.supervisor_agent_id)
                        if _bproj is not None and _bproj.supervisor_agent_id
                        else None
                    )
                    if _bsup is not None and _bsup.category == "builder":
                        existing = await mission_service.get_mission_built_by_mission(
                            db, ctx.mission_id
                        )
                        if existing is not None:
                            return {
                                "ok": True, "mission_id": str(existing.id),
                                "slug": existing.slug, "name": existing.name,
                                "status": existing.status, "reused": True,
                                "note": "This builder session already created a project; reused per the single-project invariant, not recreated.",
                            }
            except Exception:  # noqa: BLE001
                logger.exception("[mission_create] 单-project 不变量查询失败（不阻塞创建）")
            try:
                # 1) 决定 supervisor_agent_id
                sup_id: uuid.UUID | None = None
                created_supervisor = False
                if supervisor_agent_id:
                    try:
                        sup_id = uuid.UUID(supervisor_agent_id)
                    except (ValueError, TypeError):
                        return {
                            "ok": False,
                            "error": f"supervisor_agent_id={supervisor_agent_id!r} is not a valid UUID",
                        }
                else:
                    # 自动建一个默认 supervisor —— **硬性默认 deepseek/deepseek-v4-pro**
                    # （强推理；thinking 已在服务侧关闭。只有用户显式传别的 model 才换）
                    model_spec = supervisor_model_id or settings.DEFAULT_SUPERVISOR_MODEL_ID \
                        or settings.DEFAULT_AGENT_MODEL_ID or "deepseek/deepseek-v4-pro"
                    resolved = await resolve_model_id(db, model_spec)
                    if resolved is None:
                        return {
                            "ok": False,
                            "error": f"supervisor_model_id={model_spec!r} cannot be resolved; "
                                     "call list_models() first to see available models",
                        }
                    default_soul = (
                        f"You are the supervisor of project '{name}'. "
                        "Orchestrate platform workers by capability to complete tasks when triggered."
                    )
                    # ADR-027 · capability dispatch 版默认协议（无节点）：按能力派发全平台 worker，
                    # 缺能力升级 Builder。worker 花名册声明在 extra_config.required_capabilities。
                    # ADR-028 D1 · 嵌入「先 invoke approval_judge 再 request_approval」硬门片段。
                    from app.db.system_agent_prompts import APPROVAL_JUDGE_PROTOCOL_SNIPPET
                    default_protocol = (
                        "## Default Protocol (capability dispatch)\n"
                        "1. You orchestrate platform workers **by capability**, not by mission nodes.\n"
                        "2. `invoke_worker('capability:<slug>', action, params)` to run one worker; "
                        "`invoke_workers_parallel([...])` for independent steps. "
                        "Use `list_workers(capability=...)` to discover what's available.\n"
                        "3. If a needed capability has no platform worker → `request_new_capability(capability, why)` "
                        "(you auto-pause until the Builder provisions it, then resume).\n"
                        "4. memory_append to record progress after each step.\n"
                        "5. Use request_approval for human-gated steps (publishing, payments, irreversible actions).\n\n"
                        + APPROVAL_JUDGE_PROTOCOL_SNIPPET
                    )
                    sup_payload = AgentCreate(
                        name=supervisor_name or f"{slug}-supervisor",
                        description=f"Auto-generated supervisor for project '{slug}'",
                        category="builder",
                        model_id=resolved,
                        soul_md=(supervisor_soul_md.strip() or default_soul),
                        protocol_md=(supervisor_protocol_md.strip() or default_protocol),
                        produces_deliverable=False,
                    )
                    # super slug 从 url-safe mission slug 派生（`{slug}-supervisor`），
                    # 不靠中文 supervisor_name slugify 退化成无语义的裸 'supervisor'。
                    sup_agent = await agent_service.create_agent(
                        db, sup_payload, slug_hint=f"{slug}-supervisor",
                    )
                    sup_id = sup_agent.id
                    created_supervisor = True

                    # ⭐ 自动补绑核心编排 skill（ADR-027 capability dispatch）：
                    # invoke_worker / invoke_workers_parallel（按 capability 派发全平台 worker）/
                    # request_new_capability（缺能力升级 Builder）/ list_workers（发现目录）/
                    # request_approval（人审门）/ invoke_aux_model（出图等）。
                    # supervisor 没有这些就无法编排 worker，daemon 永远走不通。
                    from app.models.skill import Skill
                    critical_slugs = [
                        "invoke_worker",
                        "invoke_workers_parallel",
                        "list_workers",
                        "request_new_capability",
                        "request_approval",
                        "invoke_aux_model",
                    ]
                    critical_rows = (await db.execute(
                        select(Skill).where(Skill.slug.in_(critical_slugs))
                    )).scalars().all()
                    for sk in critical_rows:
                        try:
                            await agent_service.add_skill(db, sup_id, sk.id)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "[mission_create] 补绑核心 skill 失败 sup=%s slug=%s",
                                sup_id, sk.slug,
                            )

                # 2) 建 project（created_by 已在前置校验提取）
                payload = MissionCreate(
                    name=name,
                    slug=slug,
                    description=description,
                    supervisor_agent_id=sup_id,
                )
                proj = await mission_service.create_mission(
                    db, payload, created_by=created_by
                )

                # ADR-018 step5/S · 建造来源不再写 session.target_project_id；
                # provenance 由新 super agent 的 built_by_mission_id（= 本 builder mission）承载。
                session_bound = False

                return {
                    "ok": True,
                    "mission_id": str(proj.id),
                    "slug": proj.slug,
                    "name": proj.name,
                    "status": proj.status,
                    "runtime_status": proj.runtime_status,
                    "supervisor_agent_id": str(sup_id),
                    "supervisor_auto_created": created_supervisor,
                    "session_bound": session_bound,
                    "next_steps": (
                        "1) agent_create(...) for each needed worker capability → get agent_id "
                        "(set its `capability` slug; the super dispatches by capability, no node attach);\n"
                        "2) declare the super's roster: agent_update(supervisor_agent_id, "
                        "extra_config={'required_capabilities': [<slug>, ...]}) so it knows what it can call;\n"
                        "3) skill_bind(agent_id, skill_id) × M (first list_models / skill_list_available to get ids);\n"
                        "4) schedule_create(...) (if cron/interval/event triggers are needed)"
                    ),
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                # P6：slug 唯一冲突 → 可操作报错（已有同名项目；换 slug 重试，别静默加后缀）
                low = str(exc).lower()
                if "unique" in low and "slug" in low or "duplicate key" in low:
                    return {
                        "ok": False,
                        "error": f"slug={slug!r} is already taken (a project with the same name exists). Retry with a different slug, "
                                 "or first confirm whether that same-named project is leftover residue that should be deleted.",
                        "error_code": "SLUG_TAKEN",
                    }
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_create",
        description=(
            "Create a new Mission. **Strongly recommended** to pass only name/slug/supervisor_model_id — the tool will auto-create "
            "a default supervisor agent and associate it with the project, all in one step. "
            "Also compatible with the old usage: first agent_create the supervisor yourself, then pass supervisor_agent_id."
        ),
    )


# ─────────────────────────── mission_update ───────────────────────────
def mission_update_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        mission_id: str,
        name: str | None = None,
        description: str | None = None,
        supervisor_agent_id: str | None = None,
    ) -> dict:
        """Update the metadata of an existing Mission."""
        from app.schemas.mission import MissionUpdate
        from app.services import mission_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        update_kwargs: dict = {}
        if name is not None:
            update_kwargs["name"] = name
        if description is not None:
            update_kwargs["description"] = description
        if supervisor_agent_id is not None:
            update_kwargs["supervisor_agent_id"] = uuid.UUID(supervisor_agent_id)
        if not update_kwargs:
            return {"ok": False, "error": "No fields to change"}
        async with ctx.db_factory() as db:
            try:
                pid = await mission_service.resolve_mission_id(db, mission_id)
                if pid is None:
                    return {"ok": False, "error": f"mission_id={mission_id!r} is not a valid UUID or slug"}
                proj = await mission_service.get_mission(db, pid)
                if proj is None:
                    return {"ok": False, "error": "Mission does not exist"}
                payload = MissionUpdate(**update_kwargs)
                updated = await mission_service.update_mission(db, proj, payload)
                return {
                    "ok": True,
                    "mission_id": str(updated.id),
                    "name": updated.name,
                    "description": updated.description,
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_update",
        description="Update a Mission's name / description / supervisor_agent_id.",
    )


# ─────────────────────────── mission_delete ───────────────────────────
def mission_delete_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(mission_id: str, confirmed: bool = False) -> dict:
        """**Dangerous operation**: deletes all of the project's child tables. The tool layer strictly requires confirmed=True.

        Protocol flow:
        1. First call (confirmed default / False) → return refusal + prompt to request_approval first
        2. After request_approval gets the user's reply, retry with confirmed=True
        """
        if not confirmed:
            return {
                "ok": False,
                "error": "DANGER_NOT_CONFIRMED",
                "instruction": (
                    "mission_delete is an irreversible operation. Please first call "
                    "`request_approval(title='⚠️ Confirm project deletion', "
                    "message='Will delete the project + all nodes / schedules / sessions / "
                    "agent_skills / memory / workspace artifacts. **Unrecoverable**.', "
                    "options=['Confirm delete', 'Cancel'])`, "
                    "and after getting the 'Confirm delete' reply, retry this tool with `confirmed=True`."
                ),
            }
        from app.services import mission_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            pid = await mission_service.resolve_mission_id(db, mission_id)
            if pid is None:
                return {"ok": False, "error": f"mission_id={mission_id!r} is not a valid UUID or slug"}
            proj = await mission_service.get_mission(db, pid)
            if proj is None:
                return {"ok": False, "error": "Mission does not exist"}
            # 禁删 builder
            if proj.slug == "builder":
                return {"ok": False, "error": "The Builder Mission cannot be deleted"}
            await mission_service.delete_mission(db, proj)
            logger.warning(
                "[mission_delete] 已删除 project=%s slug=%s by acting_user=%s",
                mission_id, proj.slug, ctx.extra.get("acting_user_id"),
            )
            return {"ok": True, "deleted": mission_id, "deleted_slug": proj.slug}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_delete",
        description=(
            "(**Dangerous and irreversible**) Delete all of a Mission's data. **Must request_approval first**, "
            "and only after getting the user's 'Confirm delete' reply can you pass confirmed=True; the first call (without confirmed) will be forcibly refused."
        ),
    )


# ─────────────────────────── agent_create ───────────────────────────
def agent_create_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        name: str,
        model_id: str = "",
        category: Literal[
            "builder",
            "installer",
            "tester",
            "worker.web",
            "worker.data",
            "worker.io",
            "worker.creative",
            "utility",
            "custom",
        ] = "custom",
        description: str = "",
        soul_md: str = "",
        protocol_md: str = "",
        produces_deliverable: bool = False,
        kind: str = "",
        capability: str = "",
    ) -> dict:
        """Create a new Agent. model_id accepts:
        - Empty → automatically use `DEFAULT_AGENT_MODEL_ID` (default `deepseek/deepseek-v4-pro`)
        - UUID (from list_models' items[i].id)
        - `provider_name/model_id` string (e.g. `deepseek/deepseek-v4-pro`)
        - Bare model_id (e.g. `deepseek-v4-pro`), a duplicate name across providers will error
        """
        from app.core.config import settings
        from app.schemas.agent import AgentCreate
        from app.services import agent_service
        from app.skills_builtin.llm.llm_skills import resolve_model_id

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        # 留空 → worker 默认（deepseek/deepseek-v4-pro）
        model_spec = model_id or settings.DEFAULT_AGENT_MODEL_ID \
            or "deepseek/deepseek-v4-pro"
        async with ctx.db_factory() as db:
            # ── 单 super 不变量：一个 builder 会话只建一个 super；已建过 → 复用，绝不重建 ──
            # （根治跨轮 nudge 时 LLM 从头 agent_create 留下的僵尸 super。用 extra_config 标记，无需迁移。）
            # 整段 try 包住：不变量查询哪怕出 bug，也绝不能拖垮正常的 agent_create。
            # ADR-018 mission-only · 不变量按 provenance：本 builder mission 已建过 super → 复用
            if (kind or "").lower() == "super" and ctx.mission_id is not None:
                try:
                    # 幂等：本 builder 设计会话（主 builder mission 或 +新建 每场景会话）
                    # 已建过 super → 复用，绝不重建第二个（修 +新建 重复建 xxx + xxx-v2）。
                    from app.domain.builder.factory import existing_super_for_builder_mission
                    existing = await existing_super_for_builder_mission(db, ctx.mission_id)
                    if existing is not None:
                        return {
                            "ok": True, "agent_id": str(existing.id), "name": existing.name,
                            "category": existing.category, "kind": "super", "reused": True,
                            "note": "This builder mission already created a super; reused per the single-super invariant.",
                        }
                except Exception:  # noqa: BLE001
                    logger.exception("[agent_create] 单-super 不变量查询失败（不阻塞创建）")
            # 兼容多种 model_id 入参格式
            resolved = await resolve_model_id(db, model_spec)
            if resolved is None:
                return {
                    "ok": False,
                    "error": (
                        f"model_id={model_spec!r} cannot be resolved to an LLMModel UUID. "
                        "Please call list_models() first to get items[i].id, "
                        "or pass a 'provider_name/model_id' string."
                    ),
                }
            try:
                create_kwargs = dict(
                    name=name,
                    description=description,
                    category=category,
                    model_id=resolved,
                    soul_md=soul_md,
                    protocol_md=protocol_md,
                    produces_deliverable=produces_deliverable,
                )
                if kind:
                    create_kwargs["kind"] = kind
                if capability:
                    create_kwargs["capability"] = capability
                payload = AgentCreate(**create_kwargs)
                agent = await agent_service.create_agent(db, payload)
                # ADR-018 D3 · 1:1 provenance：记产出该 agent 的 origin Builder mission。
                # super：单-super 不变量复用 + escalation 路由。
                # worker（2026-07-05）：完整性 gate 的确定性自修复要按「本 builder 会话建的
                # 孤儿 worker」归集花名册——LLM 屡次无视 agent_update 声明指令（e2e 实证
                # 三连拒），provenance 让 finalize 能代劳，不再赌 LLM 自觉。
                if ctx.mission_id is not None:
                    agent.built_by_mission_id = ctx.mission_id
                    await db.commit()
                return {
                    "ok": True,
                    "agent_id": str(agent.id),
                    "name": agent.name,
                    "category": agent.category,
                    "kind": getattr(agent, "kind", None),
                    "capability": getattr(agent, "capability", None),
                    "enable_thinking": agent.enable_thinking,
                    "max_iterations": agent.max_iterations,
                    "model_uuid": str(resolved),
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return StructuredTool.from_function(
        coroutine=_run,
        name="agent_create",
        description=(
            "Create a new Agent. category is required (builder/installer/tester/worker.*/utility/custom). "
            "**model_id accepts 3 formats**: (1) UUID (from list_models) / "
            "(2) 'provider_name/model_id' (e.g. nebula/claude-opus-4-6) / "
            "(3) bare model_id (a duplicate name across providers will be refused). "
            "**v3 R24**: kind='super' automatically enables enable_thinking=True + max_iterations=40 (strong brain); "
            "kind='worker' keeps enable_thinking=False + max_iterations=10 (strong execution); "
            "a worker should fill in a capability slug (e.g. 'xhs_ops') so the super can invoke by capability."
        ),
    )


# ─────────────────────────── agent_update ───────────────────────────
def agent_update_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        agent_id: str,
        name: str | None = None,
        description: str | None = None,
        soul_md: str | None = None,
        protocol_md: str | None = None,
        model_id: str | None = None,
        produces_deliverable: bool | None = None,
        is_enabled: bool | None = None,
        capability_contract: dict | None = None,
    ) -> dict:
        """Update the fields of an existing Agent (most common: overwrite protocol_md / soul_md).

        capability_contract (worker only): write/upgrade the worker's capability contract. It automatically runs the unified governance gate
        (structural validation + self-consistent backward compatibility + cross-super compatibility hard block); if it breaks a super using it, the change is refused and rolled back.

        Typical scenarios:
        - The default protocol of the supervisor auto-created by `mission_create` is too thin → use this tool to change
          `protocol_md` into a complete business-chain template (with trigger routing / approval gate / retry rules)
        - In EDIT mode, adjust a worker agent's protocol or swap the model
        - Rename, disable / restart an agent
        """
        from app.schemas.agent import AgentUpdate
        from app.services import agent_service
        from app.skills_builtin.llm.llm_skills import resolve_model_id

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            aid = uuid.UUID(agent_id)
        except (ValueError, TypeError):
            return {"ok": False, "error": f"agent_id={agent_id!r} is not a valid UUID"}

        update_kwargs: dict = {}
        if name is not None:
            update_kwargs["name"] = name
        if description is not None:
            update_kwargs["description"] = description
        if soul_md is not None:
            update_kwargs["soul_md"] = soul_md
        if protocol_md is not None:
            update_kwargs["protocol_md"] = protocol_md
        if produces_deliverable is not None:
            update_kwargs["produces_deliverable"] = produces_deliverable
        if is_enabled is not None:
            update_kwargs["is_enabled"] = is_enabled
        if not update_kwargs and model_id is None and capability_contract is None:
            return {"ok": False, "error": "No fields to change"}

        async with ctx.db_factory() as db:
            try:
                agent = await agent_service.get_agent(db, aid)
                if agent is None:
                    return {"ok": False, "error": f"agent_id={agent_id} does not exist"}
                # ADR-009 · 写 worker capability_contract → 统一治理闸门（结构 + 自洽 + 跨 super 硬阻断）
                if capability_contract is not None:
                    if getattr(agent, "kind", None) != "worker":
                        return {"ok": False, "error": "capability_contract can only be written to a kind=worker agent"}
                    from app.domain.builder.capability_consumers import govern_worker_contract_change
                    old_c = (agent.extra_config or {}).get("capability_contract")
                    try:
                        await govern_worker_contract_change(
                            db, capability=agent.capability or "", slug=agent.name,
                            old_contract=old_c, new_contract=capability_contract,
                        )
                    except ValueError as gov_exc:
                        return {"ok": False, "error": str(gov_exc), "error_kind": "contract_governance"}
                    new_extra = dict(agent.extra_config or {})
                    new_extra["capability_contract"] = capability_contract
                    update_kwargs["extra_config"] = new_extra
                if model_id is not None:
                    resolved = await resolve_model_id(db, model_id)
                    if resolved is None:
                        return {
                            "ok": False,
                            "error": f"model_id={model_id!r} cannot be resolved; list_models() first",
                        }
                    update_kwargs["model_id"] = resolved
                payload = AgentUpdate(**update_kwargs)
                updated = await agent_service.update_agent(db, agent, payload)
                if capability_contract is not None:
                    # capability_contract.aux_models（图像/视频模型绑定）materialize 到 agent_aux_models 表
                    # ——运行时 invoke_aux_model 的 _resolve_binding 只读表；不落表则「未找到辅助模型」出不了图。
                    try:
                        from app.domain.builder.factory import persist_contract_aux_models
                        if await persist_contract_aux_models(db, updated.id, capability_contract):
                            await db.commit()
                    except ValueError as aux_exc:
                        return {"ok": False, "error": str(aux_exc), "error_kind": "aux_model"}
                    try:
                        from app.domain.builder.capability_index import rebuild_for_worker
                        await rebuild_for_worker(db, worker_agent_id=updated.id)
                    except Exception:
                        logger.exception("[agent_update] capability_index rebuild 失败 (不阻塞)")
                    # ADR-009 G5 · 契约写入也留一条 Builder 工作记录（与 build_* 路径一致）
                    if ctx.mission_id is not None and ctx.db_factory is not None:
                        try:
                            from app.models.builder_governance import BuilderWorkLog
                            from app.domain.builder.capability_consumers import find_supers_using_capability
                            cons = await find_supers_using_capability(db, updated.capability or "")
                            async with ctx.db_factory() as _wdb:
                                _wdb.add(BuilderWorkLog(
                                    session_id=ctx.mission_id, mission_id=ctx.mission_id,
                                    action="write_contract", target_type="worker",
                                    target_id=updated.capability or str(updated.id),
                                    affected_supers=[c["super_slug"] for c in cons],
                                    result="ok",
                                    summary=f"write/upgrade worker {updated.capability} capability_contract",
                                ))
                                await _wdb.commit()
                        except Exception:
                            logger.exception("[agent_update] builder_work_log 写入失败 (不阻塞)")
                return {
                    "ok": True,
                    "agent_id": str(updated.id),
                    "name": updated.name,
                    "fields_updated": sorted(update_kwargs.keys()),
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return StructuredTool.from_function(
        coroutine=_run,
        name="agent_update",
        description=(
            "Update fields of an existing Agent (name/description/soul_md/protocol_md/model_id/"
            "produces_deliverable/is_enabled). "
            "**Most common scenario**: overwrite the protocol_md of the supervisor auto-created by mission_create "
            "into a complete business-chain template."
        ),
    )


# ─────────────────────────── skill_bind / unbind ───────────────────────────
async def _resolve_skill_id(db, skill_id: str) -> uuid.UUID:
    """skill_id accepts a UUID or a slug (e.g. 'invoke_worker'). slug → automatically look up Skill.slug and resolve to a UUID.

    LLMs often pass a slug directly; the old implementation's uuid.UUID(slug) immediately raised 'badly formed hexadecimal UUID',
    forcing an extra round-trip through skill_list_available, wasting a turn's budget → the BUILD doesn't finish. Resolve it in place here.
    """
    from sqlalchemy import select
    from app.models.skill import Skill

    try:
        return uuid.UUID(skill_id)
    except (ValueError, AttributeError, TypeError):
        pass
    row = (await db.execute(select(Skill).where(Skill.slug == skill_id))).scalar_one_or_none()
    if row is None:
        raise ValueError(
            f"skill_id={skill_id!r} is neither a valid UUID nor a known skill slug; "
            "use skill_list_available to look up the slug/UUID"
        )
    return row.id


def _config_looks_like_aux_binding(config: dict | None) -> bool:
    """skill_bind 的 config 是否被误当成「aux 模型绑定」。

    Builder 常见误用：`skill_bind(invoke_aux_model, config={aux_models:[...]})` 或
    `config={role:'image', model_id:...}`，以为这样就绑了出图模型——其实是空操作。
    检测 aux_models 键，或 model_id + 出图/视频/embedding role 的组合。
    """
    if not isinstance(config, dict) or not config:
        return False
    if config.get("aux_models"):
        return True
    if config.get("model_id") and str(config.get("role") or "").lower() in {
        "image", "video", "embedding",
    }:
        return True
    return False


def skill_bind_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(agent_id: str, skill_id: str, config: dict | None = None) -> dict:
        from app.services import agent_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            try:
                # 封 footgun ①：把 aux 模型绑定塞进**任意** skill 的 config（aux_models，或
                # model_id+image/video/embedding role）是空操作——skill config 不是 AgentAuxModel 行，
                # 运行时 invoke_aux_model 读表仍找不到模型。先于 skill 解析拦截、指向直接调用。
                if _config_looks_like_aux_binding(config):
                    return {
                        "ok": False,
                        "error_kind": "aux_binding_in_skill_config",
                        "error": (
                            "把图像/视频/embedding 模型绑定塞进 skill_bind 的 config 不会生效"
                            "（skill config ≠ AgentAuxModel，运行时 invoke_aux_model 读表找不到）。"
                            "请**直接调用** `agent_aux_model_bind(agent_id=..., "
                            "model_id=<image 模型 UUID>, role='image')`；worker 运行时只需 skill_bind "
                            "`invoke_aux_model`（config 留空 {}）。"
                        ),
                    }
                resolved_skill = await _resolve_skill_id(db, skill_id)
                # 封 footgun ②：category='builder' 的是 Builder **构建期工具**（agent_create /
                # agent_aux_model_bind ...），该**直接调用**，不是 worker 运行时
                # 技能。把它们 skill_bind 到 worker 是空操作。直接拒绝并指路。
                from app.models.skill import Skill
                sk = await db.get(Skill, resolved_skill)
                if sk is not None and sk.category == "builder":
                    return {
                        "ok": False,
                        "error_kind": "builder_tool_not_bindable",
                        "error": (
                            f"`{sk.slug}` 是 Builder 构建期工具，应**直接调用**而不是 skill_bind 到 worker"
                            f"（绑了是空操作）。出图绑定请直接调用 `agent_aux_model_bind"
                            f"(agent_id=..., model_id=<image 模型 UUID>, role='image')`；"
                            f"worker 运行时只需绑 `invoke_aux_model` 技能。"
                        ),
                    }
                await agent_service.add_skill(
                    db,
                    uuid.UUID(agent_id),
                    resolved_skill,
                    config or {},
                )
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="skill_bind",
        description="Bind a Skill to an Agent (if it already exists, update its config). skill_id accepts a UUID or a slug (e.g. 'invoke_worker').",
    )


def skill_unbind_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(agent_id: str, skill_id: str) -> dict:
        from app.services import agent_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            try:
                ok = await agent_service.remove_skill(
                    db, uuid.UUID(agent_id), uuid.UUID(skill_id)
                )
                return {"ok": ok}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="skill_unbind",
        description="Unbind a Skill from an Agent.",
    )


# ─────────────────────────── mcp_server_register / agent_mcp_bind ───────────────────────────
def mcp_server_register_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """Register an MCP server into the mcp_servers table (for the Builder Worker).

    Used for ClawHub static-instruction / mcp-server type skills: after installation you **must** register
    the corresponding local/remote MCP server URL here, then use `agent_mcp_bind` to bind it to the worker agent,
    so that when the daemon assembles the worker executor, `load_mcp_tools` can list the real tools.
    """
    async def _run(
        name: str,
        url: str = "",
        server_type: str = "http",
        description: str = "",
        command: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        startup_command: list[str] | None = None,
        startup_cwd: str | None = None,
    ) -> dict:
        from app.schemas.skill import MCPServerCreate
        from app.services import skill_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        if server_type not in ("http", "stdio"):
            return {"ok": False, "error": f"server_type must be http or stdio, received {server_type!r}"}
        if server_type == "http" and not url:
            return {"ok": False, "error": "server_type=http must provide url (e.g. http://localhost:18060/mcp)"}
        if server_type == "stdio" and not command:
            return {"ok": False, "error": "server_type=stdio must provide command (list[str])"}
        # 修 mission-7a9afb：本地 http MCP（localhost/127.0.0.1）是要平台拉起的进程，**必须**带
        # startup_command，否则平台无从 launch → readiness 假 ready → super 以为装好实则没装。
        _is_local_http = server_type == "http" and (
            "localhost" in (url or "") or "127.0.0.1" in (url or "")
        )
        if _is_local_http and not startup_command:
            return {
                "ok": False,
                "error_code": "LOCAL_NEEDS_STARTUP_COMMAND",
                "error": (
                    f"本地 MCP（url={url}）必须带 startup_command，平台才能拉起该进程。"
                    "正确流程：① request_approval 征得安装同意 → ② run_shell 按 SETUP 装好并确认可启动 → "
                    "③ mcp_server_register(url=..., startup_command=[...], startup_cwd=...) → ④ mcp_ensure_ready。"
                ),
            }
        async with ctx.db_factory() as db:
            # 幂等：同 URL（http）/ 同 command（stdio）已存在就直接返回——
            # 否则每次 builder 跑都加一条 xiaohongshu-mcp / xiaohongshu-mcp-2 / xiaohongshu-mcp-v2 ...
            # 越积越多。匹配维度 = 「物理目标」（url 或 command 序列），不看 name。
            from sqlalchemy import select
            from app.models.skill import MCPServer
            existing_q = select(MCPServer).where(MCPServer.server_type == server_type)
            if server_type == "http":
                existing_q = existing_q.where(MCPServer.url == url)
            else:
                # stdio：比对 command JSON（保持顺序）
                existing_q = existing_q.where(MCPServer.command == command)
            existing = (await db.execute(existing_q.limit(1))).scalar_one_or_none()
            if existing is not None:
                return {
                    "ok": True,
                    "mcp_server_id": str(existing.id),
                    "name": existing.name,
                    "server_type": existing.server_type,
                    "url": existing.url,
                    "reused": True,
                    "next_step": (
                        f"Reused the existing MCP server (name={existing.name}). "
                        f"Directly agent_mcp_bind(agent_id=<worker>, mcp_server_id='{existing.id}') to expose it to the worker"
                    ),
                }
            try:
                payload = MCPServerCreate(
                    name=name,
                    description=description,
                    server_type=server_type,
                    url=url or None,
                    command=command,
                    env_vars=env_vars,
                    headers=headers,
                    startup_command=startup_command,
                    startup_cwd=startup_cwd,
                )
                server = await skill_service.create_mcp_server(db, payload)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "mcp_server_id": str(server.id),
                "name": server.name,
                "server_type": server.server_type,
                "url": server.url,
                "reused": False,
                "next_step": (
                    "Call agent_mcp_bind(agent_id=<target worker>, mcp_server_id="
                    f"'{server.id}') to expose this MCP server to the worker"
                ),
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="mcp_server_register",
        description=(
            "Register an MCP server into the system. **Must be called after installing a ClawHub mcp-server / static-instruction type skill**: "
            "record the local (e.g. http://localhost:18060/mcp) or remote MCP service, "
            "then use agent_mcp_bind to bind it to the worker. "
            "For a LOCAL http server (localhost/127.0.0.1) you **must** pass startup_command (+ startup_cwd) so the platform can launch the process; "
            "otherwise it can never become ready (register is rejected with LOCAL_NEEDS_STARTUP_COMMAND). "
            "Params: name(str required, globally unique) / url(required in http mode) / "
            "server_type('http'|'stdio', default http) / description / command(required in stdio mode, list[str]) / "
            "env_vars(dict optional) / headers(dict optional) / "
            "startup_command(list[str], required for local http — how to launch the server, e.g. ['./xhs-mcp']) / startup_cwd(str optional)."
        ),
    )


def mcp_server_restart_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """Restart a local MCP server (http mode, launched via mcp_servers.startup_command).

    Use case: when a worker's MCP tool call times out / connect refused, first call this skill to bring up the local
    server, then retry. stdio-mode servers are spawned by langchain-mcp-adapters itself, so this skill is **not needed**.
    """
    async def _run(mcp_server_id: str, wait_health_seconds: int = 20) -> dict:
        import asyncio as _asyncio
        import subprocess
        import httpx
        from app.models.skill import MCPServer

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            mid = uuid.UUID(mcp_server_id)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"mcp_server_id is not a valid UUID: {exc}"}

        async with ctx.db_factory() as db:
            server = await db.get(MCPServer, mid)
            if server is None:
                return {"ok": False, "error": f"mcp_server_id={mcp_server_id} does not exist"}
            if not server.startup_command:
                return {
                    "ok": False,
                    "error": (
                        f"MCP server '{server.name}' has no startup_command configured, cannot auto-launch. "
                        f"Ask the admin to fill in the startup command at /admin/mcp-servers/{mid}, or start the service manually."
                    ),
                }
            cmd = list(server.startup_command)
            cwd = server.startup_cwd or None
            url = server.url

        # 先看 server 是否已经活着；活的话直接返回 reused=true
        if url:
            try:
                async with httpx.AsyncClient() as cli:
                    r = await cli.get(url.rstrip("/mcp").rstrip("/") + "/", timeout=2.0)
                    # 任何 HTTP 响应就算「活」（200/404/405 都算）
                    if r.status_code < 500:
                        return {
                            "ok": True,
                            "reused": True,
                            "name": server.name,
                            "url": url,
                            "note": "server is already running, no restart needed",
                        }
            except Exception:
                pass  # 不通就 fallthrough 真去 spawn

        # spawn detached subprocess（不阻塞 worker；server 自己在后台跑）
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # 脱离 colony backend 父进程
            )
            pid = proc.pid
            logger.info("[mcp_server_restart] spawned %s pid=%s cmd=%s", server.name, pid, cmd)
        except FileNotFoundError as exc:
            return {"ok": False, "error": f"startup_command executable not found: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # 轮询 health（最长 wait_health_seconds 秒），活了就成功
        if url:
            health_url = url.rstrip("/mcp").rstrip("/") + "/"
            deadline = _asyncio.get_event_loop().time() + max(2, min(int(wait_health_seconds), 60))
            async with httpx.AsyncClient() as cli:
                while _asyncio.get_event_loop().time() < deadline:
                    try:
                        r = await cli.get(health_url, timeout=2.0)
                        if r.status_code < 500:
                            return {
                                "ok": True,
                                "reused": False,
                                "name": server.name,
                                "url": url,
                                "pid": pid,
                                "note": f"Launched, health check passed within {wait_health_seconds}s",
                            }
                    except Exception:
                        pass
                    await _asyncio.sleep(1)
            return {
                "ok": False,
                "error": (
                    f"spawned pid={pid} but {health_url} is still unreachable within {wait_health_seconds}s. "
                    f"Possibly startup_command is wrong / the port is occupied / the server starts slowly."
                ),
                "pid": pid,
            }
        return {"ok": True, "pid": pid, "reused": False, "note": "Spawned (no url, so no health check performed)"}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mcp_server_restart",
        description=(
            "Restart a local MCP server (launched via mcp_servers.startup_command). "
            "Call it when an MCP tool call times out / connect refused, to bring the server up before retrying the real call. "
            "Params: mcp_server_id(str UUID required) / wait_health_seconds(int default 20, max 60). "
            "Once it returns ok=true you can retry the MCP tool call."
        ),
    )


def agent_mcp_bind_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(agent_id: str, mcp_server_id: str) -> dict:
        from app.services import agent_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            aid = uuid.UUID(agent_id)
            mid = uuid.UUID(mcp_server_id)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"Parameter is not a valid UUID: {exc}"}
        async with ctx.db_factory() as db:
            try:
                await agent_service.add_mcp(db, aid, mid)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "agent_id": str(aid),
                "mcp_server_id": str(mid),
                "note": (
                    "The next time the daemon assembles this agent's executor, "
                    "load_mcp_tools will automatically list all tools provided by this MCP server and expose them to the LLM"
                ),
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="agent_mcp_bind",
        description=(
            "Bind an MCP server to an agent. When the daemon runs this agent, langchain-mcp-adapters dynamically "
            "exposes all tools provided by the MCP server to the agent LLM. "
            "**Must** be used together with mcp_server_register: first register to get the mcp_server_id, then bind. "
            "Params: agent_id(str UUID) / mcp_server_id(str UUID)."
        ),
    )


# ─────────────────────────── agent_aux_model_bind ───────────────────────────
def agent_aux_model_bind_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """Bind an auxiliary LLM model to an agent and tag it with a role.

    In the agent protocol, `invoke_aux_model(alias_or_role='image')` finds this binding by alias/role.
    An image-generating worker must first bind a role='image' image model; same for video (role='video'); same for embedding.
    """
    async def _run(
        agent_id: str,
        model_id: str,
        role: Literal["chat", "embedding", "image", "video", "custom"] = "custom",
        alias: str = "",
    ) -> dict:
        from app.services import agent_service
        from app.skills_builtin.llm.llm_skills import resolve_model_id

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            aid = uuid.UUID(agent_id)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"agent_id is not a UUID: {exc}"}
        async with ctx.db_factory() as db:
            resolved = await resolve_model_id(db, model_id)
            if resolved is None:
                return {
                    "ok": False,
                    "error": (
                        f"model_id={model_id!r} cannot be resolved (try a UUID / 'provider/model_id' string). "
                        "First list_models(model_type='image') / 'video' / 'embedding' to find the correct UUID."
                    ),
                }
            try:
                binding = await agent_service.add_aux_model(
                    db, aid, resolved,
                    role=role,
                    alias=(alias or None),
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "agent_id": str(aid),
                "model_uuid": str(resolved),
                "role": binding.role,
                "alias": binding.alias,
                "note": (
                    f"In the agent protocol, invoke_aux_model(alias_or_role='{binding.alias or binding.role}', ...) "
                    "can then reach this model"
                ),
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="agent_aux_model_bind",
        description=(
            "Bind an auxiliary LLM model to an agent. An image-generating worker must bind a role='image' model / a video worker must bind role='video' / "
            "an embedding worker must bind role='embedding'. role must match the model_type (a chat model cannot be bound as role='image'). "
            "Params: agent_id(UUID) / model_id(UUID or 'provider/model_id') / "
            "role('chat'|'embedding'|'image'|'video'|'custom', default 'custom') / "
            "alias(str optional short name; used when the agent protocol's invoke_aux_model(alias_or_role=...) looks up by alias)."
        ),
    )


# ─────────────────────────── mission_lifecycle_control ───────────────────────────
def mission_lifecycle_control_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        mission_id: str,
        action: Literal["start", "stop", "restart", "clear_memory"],
    ) -> dict:
        from app.services import mission_daemon, mission_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            try:
                pid = await mission_service.resolve_mission_id(db, mission_id)
                if pid is None:
                    return {"ok": False, "error": f"mission_id={mission_id!r} is not a valid UUID or slug"}
                if action == "start":
                    # Builder「start」新建的 super = 立刻拉起开始工作（首次激活 kick off 一轮）
                    await mission_daemon.start(db, pid, kickoff=True)
                elif action == "stop":
                    await mission_daemon.stop(db, pid)
                elif action == "restart":
                    await mission_daemon.restart(db, pid)
                elif action == "clear_memory":
                    res = await mission_daemon.clear_memory(db, pid)
                    return {"ok": True, "action": action, **res}
                rs = await mission_daemon.get_runtime(db, pid)
                return {
                    "ok": True,
                    "action": action,
                    "status": rs.status,
                    "started_at": rs.started_at.isoformat() if rs.started_at else None,
                    "last_heartbeat_at": rs.last_heartbeat_at.isoformat() if rs.last_heartbeat_at else None,
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_lifecycle_control",
        description=(
            "Control the target Mission's lifecycle: start / stop / restart / clear_memory (dangerous)."
        ),
    )


# ─────────────────────────── mission_apply_changes ───────────────────────────
def mission_apply_changes_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        mission_id: str,
        clear_memory: bool = False,
        confirmed_clear_memory: bool = False,
    ) -> dict:
        """Submit changes and restart the daemon. By default does not clear memory.
        When clear_memory=True, you **must also pass confirmed_clear_memory=True** (request_approval first).
        """
        from app.services import mission_daemon, mission_service

        # 硬约束：清记忆需 approval
        if clear_memory and not confirmed_clear_memory:
            return {
                "ok": False,
                "error": "DANGER_NOT_CONFIRMED",
                "instruction": (
                    "clear_memory=True is **irreversible**: it will clear all rows of mission_agent_memory. "
                    "Please first call `request_approval(title='⚠️ Confirm clearing project memory', "
                    "message='Will clear the MissionAgentMemory of the supervisor + all workers, "
                    "losing all previously accumulated execution experience. Generally only needed for major protocol/soul changes.', "
                    "options=['Agree to clear memory + restart', 'Just restart without clearing memory', 'Cancel'])`. "
                    "Only after getting the user's 'Agree to clear' reply, retry with `confirmed_clear_memory=True`."
                ),
            }
        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            try:
                pid = await mission_service.resolve_mission_id(db, mission_id)
                if pid is None:
                    return {"ok": False, "error": f"mission_id={mission_id!r} is not a valid UUID or slug"}
                cleared = None
                if clear_memory:
                    cleared = await mission_daemon.clear_memory(db, pid)
                    logger.warning(
                        "[mission_apply_changes] clear_memory=True executed on project=%s by acting_user=%s",
                        mission_id, ctx.extra.get("acting_user_id"),
                    )
                await mission_daemon.restart(db, pid)
                rs = await mission_daemon.get_runtime(db, pid)
                return {
                    "ok": True,
                    "restarted": True,
                    "memory_cleared": cleared,
                    "status": rs.status,
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_apply_changes",
        description=(
            "Call after structurally modifying a Mission's configuration: by default restart the daemon; when clear_memory=True "
            "also clear project-level memory (suitable for scenarios where changing an Agent's soul/protocol etc. invalidates old memory)."
        ),
    )


# ─────────────────────────── skill_list_available ───────────────────────────
def skill_list_available_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        query: str = "",
        category: str | None = None,
        source: Literal["all", "builtin", "installed", "custom"] = "all",
        limit: int = 50,
    ) -> dict:
        """**Search the locally available Skills** — use this as the Builder's first step in selection, **do not** jump straight to clawhub_search.

        Source descriptions:
        - `builtin`: colony's 50 bundled built-in tools (fetch_url / knowledge_search / workspace_write / ...)
        - `installed`: skills installed from ClawHub (mirrored in the skills table)
        - `custom`: instruction skills manually created by the admin
        - `all`: all of the above

        Matching rule: query is case-insensitive and does fuzzy matching against slug + name + description.
        Returns each skill's `slug` / `name` / `description` / `category` / `source` /
        `skill_id` (used when binding). Only when **nothing here is suitable** should you go to `clawhub_search`.
        """
        from sqlalchemy import select
        from app.models.skill import RemoteSkillInstall, Skill

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        q = (query or "").strip().lower()
        async with ctx.db_factory() as db:
            stmt = select(Skill).where(Skill.is_enabled.is_(True))
            if category:
                stmt = stmt.where(Skill.category == category)
            else:
                # 默认不把 Builder 构建期工具（agent_create / agent_aux_model_bind / ...）当
                # 可绑 worker 技能列出来——它们该被 Builder 直接调用，列在这里会诱导 skill_bind
                # 空操作。显式传 category='builder' 仍可查看。
                stmt = stmt.where(Skill.category != "builder")
            rows = (await db.execute(stmt)).scalars().all()

            # 查 RemoteSkillInstall 用来判定哪些 mirror row 是 ClawHub 来源
            ri_rows = (
                await db.execute(
                    select(RemoteSkillInstall.local_skill_id).where(
                        RemoteSkillInstall.local_skill_id.is_not(None)
                    )
                )
            ).all()
            clawhub_skill_ids = {r[0] for r in ri_rows}

            results: list[dict] = []
            for s in rows:
                if s.is_builtin:
                    src = "builtin"
                elif s.id in clawhub_skill_ids:
                    src = "installed"
                else:
                    src = "custom"
                if source != "all" and src != source:
                    continue
                if q:
                    hay = f"{s.slug} {s.name} {s.description}".lower()
                    if q not in hay:
                        continue
                results.append({
                    "skill_id": str(s.id),
                    "slug": s.slug,
                    "name": s.name,
                    "description": s.description,
                    "category": s.category,
                    "source": src,
                    "skill_type": s.skill_type,
                })
            # 内置优先排前；其次 installed；最后 custom
            order = {"builtin": 0, "installed": 1, "custom": 2}
            results.sort(key=lambda r: (order.get(r["source"], 9), r["slug"]))
            return {
                "ok": True,
                "total": len(results),
                "items": results[:limit],
                "hint": (
                    "If any item's description / category looks like a match, use its skill_id directly to call skill_bind. "
                    "If none are suitable, then call clawhub_search to query remotely."
                ),
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="skill_list_available",
        description=(
            "Search the locally available Skills in colony (built-in / installed from ClawHub / custom). "
            "The required first step in the Builder's selection — only go to clawhub_search when nothing local is suitable."
        ),
    )


# ─────────────────────────── mission_get ───────────────────────────
def mission_get_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(mission_id: str, summary_only: bool = True) -> dict:
        """Read the Mission structure.

        ADR-027: workers are platform-global, discovered by capability at runtime
        (invoke_worker('capability:x') / list_workers), not pre-bound as mission nodes.
        mission_get therefore returns the supervisor + schedules only — no node roster.

        Args:
            mission_id: Mission UUID
            summary_only: True (default) returns only the supervisor summary (without skill details);
                          False returns the full supervisor skill list.
        """
        from sqlalchemy import select

        from app.models.agent import Agent, AgentSkill
        from app.models.mission import Mission, MissionSchedule
        from app.models.skill import Skill
        from app.services import mission_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            pid = await mission_service.resolve_mission_id(db, mission_id)
            if pid is None:
                return {"ok": False, "error": f"mission_id={mission_id!r} is not a valid UUID or slug"}
            proj = (
                await db.execute(select(Mission).where(Mission.id == pid))
            ).scalar_one_or_none()
            if proj is None:
                return {"ok": False, "error": "Mission does not exist"}

            # supervisor agent
            sup = (await db.execute(select(Agent).where(Agent.id == proj.supervisor_agent_id))).scalar_one_or_none()
            all_agent_ids = [sup.id] if sup else []
            # agent → skills
            skills_by_agent: dict[uuid.UUID, list[dict]] = {aid: [] for aid in all_agent_ids}
            if all_agent_ids:
                rows = (
                    await db.execute(
                        select(AgentSkill, Skill)
                        .join(Skill, Skill.id == AgentSkill.skill_id)
                        .where(AgentSkill.agent_id.in_(all_agent_ids))
                    )
                ).all()
                for link, sk in rows:
                    skills_by_agent.setdefault(link.agent_id, []).append(
                        {"slug": sk.slug, "name": sk.name, "skill_id": str(sk.id)}
                    )
            # schedules
            sched_rows = (
                await db.execute(
                    select(MissionSchedule).where(MissionSchedule.mission_id == pid)
                )
            ).scalars().all()

            def _serialize_agent(a: Agent | None) -> dict | None:
                if a is None:
                    return None
                base = {
                    "agent_id": str(a.id),
                    "name": a.name,
                    "category": a.category,
                    "produces_deliverable": a.produces_deliverable,
                    "model_id": str(a.model_id) if a.model_id else None,
                    "is_enabled": a.is_enabled,
                }
                # E8：summary_only 模式不返回 skill 详情，只返回数量；大项目可减 90%+ 体积
                if summary_only:
                    base["skill_count"] = len(skills_by_agent.get(a.id, []))
                else:
                    base["skills"] = skills_by_agent.get(a.id, [])
                return base

            return {
                "ok": True,
                "project": {
                    "id": str(proj.id),
                    "slug": proj.slug,
                    "name": proj.name,
                    "description": proj.description,
                    "status": proj.status,
                    "runtime_status": proj.runtime_status,
                    "auto_approve": proj.auto_approve,
                    "context_compression_threshold": proj.context_compression_threshold,
                },
                "supervisor": _serialize_agent(sup),
                "schedules": [
                    {
                        "id": str(s.id),
                        "name": s.name,
                        "kind": s.kind,
                        "expr": s.expr,
                        "enabled": s.enabled,
                        "last_fired_at": s.last_fired_at.isoformat() if s.last_fired_at else None,
                        "next_fire_at": s.next_fire_at.isoformat() if s.next_fire_at else None,
                        "fire_count": s.fire_count,
                    }
                    for s in sched_rows
                ],
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_get",
        description=(
            "Read a Mission's full structure (basic info / supervisor / nodes & each node's agent & bound skills / "
            "schedules). The entry point for EDIT mode: first mission_get to understand the current state, then decide what to change."
        ),
    )


# ─────────────────────────── schedule_create ───────────────────────────
def schedule_create_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        mission_id: str,
        name: str,
        kind: Literal["cron", "interval", "event"],
        expr: str,
        payload_template: dict | None = None,
        enabled: bool = True,
    ) -> dict:
        """Configure a scheduled / periodic / event trigger for a Mission.

        Args:
            mission_id: Target Mission UUID
            name: schedule name (uniquely readable within the same project)
            kind: cron / interval / event
            expr: cron expression (e.g. `0 8 * * *`) / interval string (e.g. `5m` `2h`) / event name (lowercase)
            payload_template: the initial payload passed to the daemon when triggered (JSON dict)
            enabled: whether enabled (default True)
        """
        from app.models.mission import MissionSchedule
        from app.schemas.schedule import _validate_expr
        from app.services import mission_service, scheduler_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            _validate_expr(kind, expr)
        except ValueError as exc:
            return {"ok": False, "error": f"expr is invalid: {exc}"}
        # E9：payload_template 校验 — 必须是 dict 且 stringify ≤ 4KB
        payload_template = payload_template or {}
        if not isinstance(payload_template, dict):
            return {
                "ok": False,
                "error": f"payload_template must be a JSON object (dict), received {type(payload_template).__name__}",
            }
        import json as _json
        try:
            pt_str = _json.dumps(payload_template, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"payload_template cannot be JSON serialized: {exc}"}
        if len(pt_str) > 4096:
            return {
                "ok": False,
                "error": f"payload_template is {len(pt_str)} characters after serialization, exceeding the limit of 4096",
            }
        acting = ctx.extra.get("acting_user_id")
        if not acting:
            return {"ok": False, "error": "Missing acting_user_id context"}
        async with ctx.db_factory() as db:
            pid = await mission_service.resolve_mission_id(db, mission_id)
            if pid is None:
                return {"ok": False, "error": f"mission_id={mission_id!r} is not a valid UUID or slug"}
            proj = await mission_service.get_mission(db, pid)
            if proj is None:
                return {"ok": False, "error": "Mission does not exist"}
            # **幂等**：同 mission_id × kind × expr 已存在就**复用 + 更新 payload**，不重复创建。
            # LLM 在 assembler 失败 retry 时会再次调 schedule_create，无 dedup 就会落出 8 条 schedule
            # 而不是 4 条（v16 实测）。
            from sqlalchemy import select
            existing = (await db.execute(
                select(MissionSchedule).where(
                    MissionSchedule.mission_id == pid,
                    MissionSchedule.kind == kind,
                    MissionSchedule.expr == expr.strip(),
                ).limit(1)
            )).scalar_one_or_none()
            if existing is not None:
                # 同 (kind,expr) 已有 → 用新参数更新（最关键是 payload_template 可能 retry 时补全 task）
                existing.name = name
                existing.payload_template = payload_template
                existing.enabled = enabled
                await db.commit()
                await db.refresh(existing)
                try:
                    scheduler_service.reschedule_one(existing)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[schedule_create] reschedule_one 失败: %s", exc)
                return {
                    "ok": True,
                    "reused": True,
                    "schedule_id": str(existing.id),
                    "kind": existing.kind,
                    "expr": existing.expr,
                    "enabled": existing.enabled,
                    "note": f"Reused the existing schedule (kind={kind} expr={expr}), payload_template updated",
                }
            # ADR-024 S4 · 新建前护栏：数量上限 / 最小间隔 / cron 合法（super 自管调度防烧钱）
            from app.domain.scheduling.schedule_guard import validate_schedule
            from sqlalchemy import func as _func
            _cnt = (await db.execute(
                select(_func.count()).select_from(MissionSchedule).where(MissionSchedule.mission_id == pid)
            )).scalar() or 0
            _ok, _why = validate_schedule(kind=kind, expr=expr.strip(), existing_count=_cnt)
            if not _ok:
                return {"ok": False, "error": _why}
            sched = MissionSchedule(
                mission_id=pid,
                name=name,
                kind=kind,
                expr=expr.strip(),
                payload_template=payload_template,
                enabled=enabled,
                created_by=uuid.UUID(str(acting)),
            )
            db.add(sched)
            await db.commit()
            await db.refresh(sched)
            try:
                scheduler_service.reschedule_one(sched)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[schedule_create] reschedule_one 失败: %s", exc)
            return {
                "ok": True,
                "reused": False,
                "schedule_id": str(sched.id),
                "kind": sched.kind,
                "expr": sched.expr,
                "enabled": sched.enabled,
                "next_fire_at": sched.next_fire_at.isoformat() if sched.next_fire_at else None,
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="schedule_create",
        description=(
            "Add a trigger rule to a Mission: cron (cron expression) / interval (30s/5m/2h/1d) / "
            "event (webhook event name). Takes effect immediately, no restart needed."
        ),
    )


# ─────────────────────────── schedule_update ───────────────────────────
def schedule_update_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        schedule_id: str,
        name: str | None = None,
        kind: Literal["cron", "interval", "event"] | None = None,
        expr: str | None = None,
        enabled: bool | None = None,
        payload_template: dict | None = None,
    ) -> dict:
        from app.models.mission import MissionSchedule
        from app.schemas.schedule import _validate_expr
        from app.services import scheduler_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            sid = uuid.UUID(schedule_id)
        except Exception:
            return {"ok": False, "error": "schedule_id is not a valid UUID"}
        async with ctx.db_factory() as db:
            sched = await db.get(MissionSchedule, sid)
            if sched is None:
                return {"ok": False, "error": "Schedule does not exist"}
            # 校验新 expr
            new_kind = kind or sched.kind
            new_expr = (expr or sched.expr).strip()
            if kind or expr:
                try:
                    _validate_expr(new_kind, new_expr)
                except ValueError as exc:
                    return {"ok": False, "error": f"expr is invalid: {exc}"}
            if name is not None:
                sched.name = name
            if kind is not None:
                sched.kind = kind
            if expr is not None:
                sched.expr = new_expr
            if enabled is not None:
                sched.enabled = enabled
            if payload_template is not None:
                sched.payload_template = payload_template
            await db.commit()
            await db.refresh(sched)
            try:
                scheduler_service.reschedule_one(sched)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[schedule_update] reschedule_one 失败: %s", exc)
            return {
                "ok": True,
                "schedule_id": str(sched.id),
                "kind": sched.kind,
                "expr": sched.expr,
                "enabled": sched.enabled,
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="schedule_update",
        description="Update a Schedule's name / kind / expr / enabled / payload_template (any subset).",
    )


# ─────────────────────────── schedule_delete ───────────────────────────
def schedule_delete_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(schedule_id: str) -> dict:
        from app.models.mission import MissionSchedule
        from app.services import scheduler_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            sid = uuid.UUID(schedule_id)
        except Exception:
            return {"ok": False, "error": "schedule_id is not a valid UUID"}
        async with ctx.db_factory() as db:
            sched = await db.get(MissionSchedule, sid)
            if sched is None:
                return {"ok": False, "error": "Schedule does not exist"}
            try:
                scheduler_service.delete_one(sched.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[schedule_delete] delete_one 失败: %s", exc)
            await db.delete(sched)
            await db.commit()
            return {"ok": True, "deleted": schedule_id}

    return StructuredTool.from_function(
        coroutine=_run,
        name="schedule_delete",
        description="Delete a Schedule.",
    )


async def _recent_approved_decisions(db, mission_id, limit: int = 5) -> list[dict]:
    """ADR-030 · 取本任务最近已决审批（DB 真实用户决定，可信）喂给 shell judge：
    已批准的操作（如「授权安装 X MCP」→ 用户选「同意安装」）→ 判官据此放行对应 run_shell。
    命令内的『已批准』辩解文字仍不可信（judge 提示词已区分）。"""
    if mission_id is None:
        return []
    try:
        from sqlalchemy import select as _select
        from app.models.approvals import PendingApproval
        rows = (await db.execute(
            _select(PendingApproval)
            .where(PendingApproval.mission_id == mission_id,
                   PendingApproval.status == "decided")
            .order_by(PendingApproval.decided_at.desc().nullslast())
            .limit(limit)
        )).scalars().all()
        return [
            {"title": r.title, "option": r.decided_option, "message": r.message}
            for r in rows
        ]
    except Exception:  # noqa: BLE001 — 审批上下文取不到不应阻断 shell（judge 缺审批即按默认判）
        logger.exception("[run_shell] 取审批上下文失败（不阻塞）mission=%s", mission_id)
        return []


def run_shell_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """ADR-010 R3 · (Builder scope) general-purpose shell, executed after gatekeeping.

    Stance: no human authorization, no sandbox (the user makes the call). Before execution, it passes a denylist hard block + a simple fast LLM safety gate
    (judges literal effects, ignores rhetoric, default-deny), and the result is written to an immutable audit log. auto-shell remediation runs through this.
    """
    async def _run(command: str, cwd: str | None = None, reason: str | None = None) -> dict:
        from app.models.agent import Agent
        from app.models.provider import LLMModel
        from app.services import agent_service
        from app.services.shell_exec import execute_guarded_shell
        from app.services.shell_judge import make_shell_judge

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory unavailable"}
        actor = f"builder:{ctx.mission_id}" if ctx.mission_id else "builder:unknown"
        async with ctx.db_factory() as db:
            agent_id = (ctx.extra or {}).get("agent_id")
            agent = await db.get(Agent, uuid.UUID(str(agent_id))) if agent_id else None
            if agent is None:
                return {"ok": False, "error": "Cannot locate the initiating agent to construct the safety gate"}
            # 修 mission-2-3ecbf4：平台 Agent（Builder / mcp_installer 等）一律 model_id=NULL
            # （运行时按 kind 解析平台默认模型）。原来硬要求 agent.model_id → run_shell 对所有
            # 平台 agent 永远报「Cannot locate the initiating agent's model」→ MCP 装不了。
            # 改用 _resolve_agent_model（model_id 缺失时回落平台默认 by kind），与 daemon 一致。
            try:
                model = await agent_service._resolve_agent_model(db, agent)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"无法解析安全门模型（平台默认模型未配置？）：{exc}"}
            llm = await agent_service._build_llm(db, model, agent)
            # ADR-030 · 门提示词后台可编辑（system_settings key `shell_judge.system_prompt`，
            # 缺省回落默认）+ 喂入本任务 DB 已核实的用户审批 → 用户已批准的操作(如授权安装 MCP)放行。
            from app.core import system_settings as _ss
            gate_prompt = await _ss.get(db, "shell_judge.system_prompt", None)
            approvals = await _recent_approved_decisions(db, ctx.mission_id)
            judge = make_shell_judge(
                llm, system_prompt=gate_prompt or None, approvals=approvals,
            )
            return await execute_guarded_shell(
                command, cwd=cwd, reason=reason, judge=judge, db=db, actor=actor,
            )

    return StructuredTool.from_function(
        coroutine=_run,
        name="run_shell",
        description=(
            "(Builder-only) Execute a shell command and return {ok,exit_code,stdout,stderr,audit_id}. "
            "Used for automatic remediation (installing binaries / starting local servers, i.e. auto-shell remediation). "
            "Before execution it passes a denylist + LLM safety gate and may be refused (returns blocked=true); make the command as specific and least-privilege as possible. "
            "Params: command(str required) / cwd(str?) / reason(str? why you want to run it)."
        ),
    )


def mcp_ensure_ready_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """ADR-010 R1+R5 · (Builder) Generate a readiness manifest for an MCP (if none) and run ensure_ready.

    Call it after installing/connecting an MCP: generate a manifest from startup_command + tool introspection + secret_keys →
    auto-shell remediation (bring up the local server) → probe human-* (e.g. not logged in) → create a human-residual card + pause.
    Can also be called again for runtime reactions (login expired, etc.).
    """
    async def _run(mcp_server_id: str, deployment: str = "local",
                   secret_keys: list[str] | None = None,
                   target_project_id: str | None = None) -> dict:
        from app.domain.readiness import generate_manifest
        from app.models.skill import MCPServer
        from app.services import readiness as rd

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory unavailable"}
        try:
            sid = uuid.UUID(str(mcp_server_id))
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"mcp_server_id is invalid: {exc}"}
        # ADR-012 R3：扫码/密钥卡落到指定项目（新建 super）的会话；缺省回退到当前 ctx 项目
        card_pid = ctx.mission_id
        if target_project_id:
            try:
                card_pid = uuid.UUID(str(target_project_id))
            except (ValueError, TypeError):
                pass

        async with ctx.db_factory() as db:
            server = await db.get(MCPServer, sid)
            if server is None:
                # P2：常见误用——传的是 ClawHub 安装的 **skill id**，不是 MCPServer 行。
                # 给可操作指引，而不是干巴巴的"不存在"。
                from app.models.skill import Skill as _Skill
                maybe_skill = await db.get(_Skill, sid)
                if maybe_skill is not None:
                    return {
                        "ok": False,
                        "error_code": "NOT_AN_MCPSERVER",
                        "error": (
                            f"id={mcp_server_id} is the skill \"{maybe_skill.slug}\", not an MCPServer. "
                            "Readiness flow for a local-server type MCP: ① request_approval to get the user's consent to install third-party components "
                            "(including a suggested tag) → ② run_shell to install+compile+start per that skill's SETUP.md → "
                            "③ mcp_server_register(startup_command=...) to register it as an MCPServer → ④ then call mcp_ensure_ready "
                            "on that MCPServer id. Put QR-code login type items into a human card during the super's operating phase."
                        ),
                    }
                return {
                    "ok": False,
                    "error_code": "MCPSERVER_NOT_FOUND",
                    "error": f"mcp_server {mcp_server_id} does not exist (first mcp_server_register to register, then ensure_ready)",
                }

            # 修 mission-7a9afb 假 ready：本地部署的 http MCP 是平台要拉起的进程，无 startup_command
            # 就无从 launch → generate_manifest 不会产 server_up 要求 → 探针无事可查 → 误判 ready。
            # 这里**硬失败**，逼 super 走「装好 → 带 startup_command 注册」正流程，而非静默假成功。
            # （stdio 由 client 用 command 拉起、远端 http 是已跑的服务，均无需 startup_command。）
            if deployment == "local" and server.server_type == "http" and not server.startup_command:
                return {
                    "ok": False,
                    "error_code": "NO_STARTUP_COMMAND",
                    "error": (
                        f"本地 http MCP「{server.name}」没有 startup_command，平台无法拉起 → 不可能 ready。"
                        "请先 ① request_approval 征得安装同意 → ② run_shell 按 SETUP 装好并确认可启动 → "
                        "③ 用 mcp_server_register(url=..., startup_command=[...], startup_cwd=...) 重新注册（或更新）→ "
                        "④ 再调 mcp_ensure_ready。不要在未真正安装启动前就当作 ready。"
                    ),
                }

            # 1. 初始 manifest（server_up 不需内省，从 startup_command 即可）
            if not server.readiness_manifest:
                m = generate_manifest(deployment=deployment, tool_names=[],
                                      startup_command=server.startup_command,
                                      secret_keys=secret_keys or [])
                server.readiness_manifest = m.to_dict()
                await db.commit()

            # 2. 先跑一轮：拉起 server（auto-shell）
            await rd.ensure_ready_for_server(db, sid, mission_id=card_pid)

            # 3. server 起来后内省工具，补 logged_in（若有登录类工具且尚无该 req）
            try:
                from langchain_mcp_adapters.client import MultiServerMCPClient
                if server.url:
                    client = MultiServerMCPClient({server.name: {
                        "url": server.url, "transport": "streamable_http",
                        "headers": server.headers or {}}})
                    names = [t.name for t in await client.get_tools()]
                    has_login = any(n.endswith("check_login_status") or n.endswith("get_login_qrcode") for n in names)
                    cur = server.readiness_manifest or {}
                    have = {r["id"] for r in cur.get("requirements", [])}
                    if has_login and "logged_in" not in have:
                        cur.setdefault("requirements", []).append({
                            "id": "logged_in", "kind": "human-qr",
                            "probe": {"type": "mcp_tool", "tool": "check_login_status"},
                            "remediation": {"type": "human-qr", "tool": "get_login_qrcode"},
                        })
                        server.readiness_manifest = dict(cur)
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(server, "readiness_manifest")
                        await db.commit()
            except Exception:  # noqa: BLE001
                logger.warning("[mcp_ensure_ready] 工具内省失败（server 可能未起），跳过补 logged_in")

            # 4. 终轮：探针所有 requirement，human-* → 建卡 + 暂停
            res = await rd.ensure_ready_for_server(db, sid, mission_id=card_pid)
            return {"ok": True, **res}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mcp_ensure_ready",
        description=(
            "(Builder · ADR-010) Generate a readiness manifest for an MCP and ensure it is ready: "
            "auto-launch the local server, introspect and add login requirements, and for human-required items (QR code/secret/terms) create a card + pause the project. "
            "Call it to wrap up after installing the MCP. Params: mcp_server_id(required) / deployment('local'|'cloud') / secret_keys(list?) / "
            "target_project_id(str?, the QR-code/secret card lands in that project's session; pass the new project id when creating a super)."
        ),
    )


def activate_super_first_run_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """(Builder · ADR-018 mission-only) After the project is built, kickoff the super's first tick.

    daemon 直接跑 (mission_id, 'main')；不再建 session 容器、不再挂 ADR-011 首跑中继
    （relay 子系统已随 builder-chat-as-session 模型退役）。super 在自己的 mission 里 propose-and-confirm。
    """
    async def _run(mission_id: str) -> dict:
        from app.models.mission import Mission
        from app.services import mission_daemon, messaging_service

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory unavailable"}
        try:
            pid = uuid.UUID(str(mission_id))
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"mission_id is invalid: {exc}"}

        async with ctx.db_factory() as db:
            # 2026-07-03 完整性 gate（与 build_finalizer 共用）：LLM 曾直接调本技能
            # 自我认证激活残废 super（roster 空 + 8 孤儿 worker），写下 super_activated
            # 后 finalize 永远 already_finalized 短路 → gate 形同虚设。技能路径必须过
            # 同一 gate；不完整 → 拒绝 + 缺口回流（错误驱动自愈）。
            _proj0 = await db.get(Mission, pid)
            if _proj0 is not None and _proj0.supervisor_agent_id is not None:
                from app.models.agent import Agent as _Agent
                from app.services.build_finalizer import (
                    auto_adopt_orphan_workers,
                    check_build_completeness,
                )
                _sup0 = await db.get(_Agent, _proj0.supervisor_agent_id)
                if _sup0 is not None:
                    # 确定性自修复：本 builder 会话建的孤儿 worker 先收编进花名册
                    try:
                        await auto_adopt_orphan_workers(db, _sup0, ctx.mission_id)
                    except Exception:  # noqa: BLE001
                        pass
                    comp = await check_build_completeness(db, _sup0)
                    if not comp["complete"]:
                        return {
                            "ok": False,
                            "error": "BUILD_INCOMPLETE",
                            "missing_workers": comp["missing_workers"],
                            "orphan_workers": comp["orphan_workers"],
                            "instruction": (
                                "构建不完整，拒绝激活。请补齐后重试：为 missing_workers 里的每个能力 "
                                "agent_create worker；把 orphan_workers 里的能力用 agent_update("
                                "agent_id=<super>, extra_config={'required_capabilities': [<全部能力>]}) "
                                "声明进 super 花名册。"
                            ),
                        }
            # kickoff 首跑（super soul §0 会提案-确认问定位）。ADR-018：daemon 直接跑 (mission, 'main')
            await mission_daemon.start(db, pid, kickoff=True)
            proj = await db.get(Mission, pid)
            slug = proj.slug if proj else None
            # ADR-012 R2b：在 Builder 会话写一条「super 已激活」消息（skill 控制 meta，可靠），
            # 前端据 meta.type=super_activated 渲染「进入 super →」按钮（新标签打开）。
            if ctx.mission_id and ctx.thread_key and slug:
                await messaging_service.append_message(
                    db, ctx.mission_id, ctx.thread_key, role="agent_log",
                    content=f"✅ The super has been built and activated: {proj.name or slug}. Click 'Enter super →' to go to its workspace; it will give you an operating plan that you just confirm or fine-tune.",
                    meta={"type": "super_activated", "project_slug": slug,
                          "project_name": proj.name if proj else slug},
                )
        return {
            "ok": True, "project_slug": slug,
            "note": "The super's first run is activated; an 'Enter super' button has been shown in the Builder session. Onboarding happens in the super's own session.",
        }

    return StructuredTool.from_function(
        coroutine=_run,
        name="activate_super_first_run",
        description=(
            "(Builder · ADR-012) Call it after the project is built: start the super's first operating session + kickoff the first run, "
            "and show an 'Enter super →' button in the Builder session (the user jumps in via a new tab to view the plan card). "
            "The super does propose-and-confirm positioning in its own session, not via relay. Params: mission_id."
        ),
    )
