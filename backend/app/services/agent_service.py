"""Agent 业务服务 + LangGraph 构建。

核心函数 `build_agent_executor`：
1. 拼接 System Prompt（soul_md + protocol_md + instruction Skills + 分支 memory）
2. 装配工具：内置 Skill 工厂（tool_builtin）+ MCP 工具（Phase 8 接入）
3. 通过 `langchain.agents.create_agent` 组装可执行 Agent
   （该函数底层返回 LangGraph `CompiledStateGraph`，保留 checkpointer / interrupt /
    astream_events 等 LangGraph 能力；仅 API 迁移，不更换运行时）
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent, AgentAuxModel, AgentMCPServer, AgentSkill
from app.models.provider import LLMModel
from app.models.skill import Skill
from app.schemas.agent import AgentCreate, AgentUpdate
from app.skills_builtin import BUILTIN_TOOL_REGISTRY, BuiltinToolContext

logger = logging.getLogger(__name__)


# ──────────────────── CRUD ────────────────────
async def list_agents(db: AsyncSession) -> Sequence[Agent]:
    result = await db.execute(select(Agent).order_by(Agent.created_at))
    return result.scalars().all()


async def get_agent(db: AsyncSession, agent_id: uuid.UUID) -> Agent | None:
    result = await db.execute(
        select(Agent)
        .options(
            selectinload(Agent.skills).joinedload(AgentSkill.skill),
            selectinload(Agent.mcp_servers).joinedload(AgentMCPServer.mcp_server),
            selectinload(Agent.aux_models).joinedload(AgentAuxModel.model),
        )
        .where(Agent.id == agent_id)
    )
    return result.scalar_one_or_none()


class LLMNotConfiguredError(RuntimeError):
    """An agent with no explicit model_id can't resolve the platform default model
    (none configured yet). Callers (daemon tick / super chat) catch this to skip running
    and prompt the user to finish onboarding instead of crashing."""


async def get_agent_model(db: AsyncSession, agent: Agent) -> LLMModel | None:
    if agent.model_id is None:
        from app.domain.onboarding.default_model import resolve_default_model
        role = "supervisor" if getattr(agent, "kind", None) == "super" else "agent"
        return await resolve_default_model(db, role)
    return await db.get(LLMModel, agent.model_id)


async def _validate_model(db: AsyncSession, model_id: uuid.UUID) -> LLMModel:
    result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise ValueError(f"LLMModel {model_id} 不存在")
    if not model.is_enabled:
        raise ValueError(f"LLMModel {model.model_id} 已停用")
    return model


async def _resolve_agent_model(db: AsyncSession, agent: Agent) -> LLMModel:
    """Resolve an agent's primary model: an explicit model_id, else the platform default
    model for its role (NULL = "use default"), else raise LLMNotConfiguredError."""
    if agent.model_id is not None:
        return await _validate_model(db, agent.model_id)
    from app.domain.onboarding.default_model import resolve_default_model
    role = "supervisor" if getattr(agent, "kind", None) == "super" else "agent"
    model = await resolve_default_model(db, role)
    if model is None:
        raise LLMNotConfiguredError(
            "No default LLM model is configured. This agent uses the platform default "
            "model; add an LLM provider and pick default models in onboarding to run it."
        )
    return model


# v6.M (R2-4) · DEFAULT_AUTO_BIND_SKILL_EXCLUDE 已删（migration 049 后 Skill.scope 是真相源）
# auto-bind 只看 Skill.scope IN (agent_kind, 'all')；新 super-only skill 注册时填 scope='super' 即可。
# 历史：v3-v5 在这里 hardcode 黑名单防 worker 误绑 super tool；v6 改 declarative scope。

#: 自动绑定时按 category 整体排除——builder/installer/tester 类 skill 是
#: Builder/Installer/Tester Agent 的专用工具；Worker LLM 看到它们会乱调，
#: 比如 worker 把 `remote_skill_invoke` 当 MCP 入口调出 UUID 错误。
#: 这类 skill **只**能通过 `seed_builder_project` 显式绑或 Builder Worker 主动 `skill_bind`。
DEFAULT_AUTO_BIND_CATEGORY_EXCLUDE: set[str] = {"builder", "installer", "tester"}


async def _v42_check_worker_protocol(db: AsyncSession, kind: str | None, protocol_md: str | None) -> None:
    """V42 · worker.protocol_md 不允许包含 super-only 词；命中即抛 ValueError。

    forbidden_words 列表存在 system_settings 'factory.worker_protocol_forbidden_words' 里 admin 可调。
    """
    if kind != "worker" or not protocol_md:
        return
    try:
        from app.core import system_settings as _ss
        words = await _ss.get_list(db, "factory.worker_protocol_forbidden_words", [])
    except Exception:
        words = []
    if not words:
        return
    text = (protocol_md or "").lower()
    hits = [w for w in words if isinstance(w, str) and w.lower() in text]
    if hits:
        raise ValueError(
            f"V42 worker.protocol_md 含 super-only 词：{hits[:5]}（已配置 factory.worker_protocol_forbidden_words）"
        )


async def create_agent(
    db: AsyncSession, payload: AgentCreate, *, slug_hint: str | None = None
) -> Agent:
    # model_id None = use the platform default model (resolved at runtime). ADR-017.
    if payload.model_id is not None:
        await _validate_model(db, payload.model_id)
    data = payload.model_dump()
    # ── v3 R24 强落地默认（kind-aware）──
    # super：max_iterations 默认 40（除非 user 显式覆写）。
    # 思考档位（thinking_level）所有 kind 默认 off（最省 token / 最快首 token）——
    # 需要更强思考由用户在 agent 设置里手动调高一档（off/low/medium/high）。
    kind = data.get("kind")
    if kind is None:
        cat = (data.get("category") or "").lower()
        if cat.startswith("worker"):
            kind = "worker"
        elif cat in {"builder", "installer", "tester"}:
            kind = cat
        else:
            kind = "utility"
        data["kind"] = kind
    if kind == "super":
        if not payload.model_fields_set.intersection({"max_iterations"}):
            data["max_iterations"] = 40
        # super 身份：M2/agent_create 路径补 slug + display_name（apply_super_spec 自己已设）。
        # 否则 M2 建的 super slug/display_name 为空，/mission/<super>/<mission> 路由与标题只能回退 agent 名。
        if not data.get("slug"):
            import re as _re
            # slug_hint（如 mission 的 url-safe slug 派生）优先于 name —— 中文 name
            # slugify 会退化成无语义的裸 'supervisor'（多个中文领域 super 全撞成
            # supervisor/-2/-3，URL 不可辨识）。hint 给出领域可读 slug。
            _src = slug_hint if (slug_hint and slug_hint.strip()) else (data.get("name") or "")
            _base = _re.sub(r"[^a-z0-9]+", "-", _src.lower()).strip("-") or "super"
            _slug, _n = _base, 2
            while (await db.execute(select(Agent.id).where(Agent.slug == _slug).limit(1))).scalar() is not None:
                _slug, _n = f"{_base}-{_n}", _n + 1
            data["slug"] = _slug
        if not data.get("display_name"):
            data["display_name"] = data.get("name")
    # ── DeepSeek 家族：强制 thinking_level='off'（覆盖 LLM 误传 / 用户高档）──
    # DeepSeek V4 thinking 只能靠 extra_body={"thinking":{"type":"disabled"}} 关；其 reasoning_content
    # 流式当前与链路不兼容（泄漏、变慢）。所以 deepseek 建出的 agent 一律 thinking off
    #（ADR-014 配套：选 deepseek 即接受其 thinking 由服务侧关）。
    _mrow = await db.get(LLMModel, data.get("model_id"))
    if _mrow is not None and "deepseek" in (_mrow.model_id or "").lower():
        data["enable_thinking"] = False
        data["thinking_level"] = "off"
    # V42 worker.protocol_md forbidden-word check
    await _v42_check_worker_protocol(db, kind, data.get("protocol_md"))
    # ── worker 最小 capability_contract 兜底（确定性，不靠 LLM 自觉）──
    # 无 contract 的 worker super 无法调度（list_workers advertises 空）；真出过 Builder
    # BUILD 建 4 个 worker 全漏写 → super 首跑即 escalation。缺省合成单 'execute' action，
    # LLM 需要精确 actions/审批语义时再 agent_update(capability_contract=...) 覆盖。
    if kind == "worker" and data.get("capability"):
        _extra = dict(data.get("extra_config") or {})
        if not _extra.get("capability_contract"):
            _extra["capability_contract"] = {
                "capability": data["capability"],
                "version": "0.1.0",
                "advertises": [{
                    "action": "execute",
                    "input_schema": {"goal": "string", "params": "object?"},
                    "output_schema": {"result": "object"},
                    "side_effects": [],
                    "requires_approval": False,
                }],
            }
            data["extra_config"] = _extra
    agent = Agent(**data)
    db.add(agent)
    await db.flush()  # 拿到 agent.id，用于 AgentSkill 外键

    # v6 · auto-bind 优先看 declarative SkillScope（migration 049 给 32 个 builtin 都 backfill 了）
    # 双层守门：scope 匹配 + 老黑名单 fallback（双重保险，逐步移除黑名单）
    # scope 语义：
    #   - 'all'           → 任何 kind 都绑（默认）
    #   - 'super'         → 仅 super kind 绑
    #   - 'worker'        → 仅 worker kind 绑
    #   - 'builder'       → 仅 builder/installer/tester kind 绑（同 category）
    target_kind = kind or "utility"
    if target_kind in ("super",):
        scope_allowed = ["super", "all"]
    elif target_kind in ("worker",):
        scope_allowed = ["worker", "all"]
    elif target_kind in ("builder", "installer", "tester"):
        scope_allowed = ["builder", "all"]
    else:
        scope_allowed = ["all"]
    # scope 是真相源：显式 scope 命中 scope_allowed → 绑（即便 category 在黑名单，如
    # schedule_update category=builder 但 scope=super，PM 该拿到）。category 黑名单只兜底
    # NULL-scope 老数据。
    builtin_skills = (
        (
            await db.execute(
                select(Skill).where(
                    Skill.is_builtin.is_(True),
                    sa.or_(
                        Skill.scope.in_(scope_allowed),
                        sa.and_(
                            Skill.scope.is_(None),
                            Skill.category.notin_(DEFAULT_AUTO_BIND_CATEGORY_EXCLUDE),
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for skill in builtin_skills:
        db.add(AgentSkill(agent_id=agent.id, skill_id=skill.id, config={}))

    await db.commit()
    return await get_agent(db, agent.id)  # type: ignore[return-value]


async def update_agent(db: AsyncSession, agent: Agent, payload: AgentUpdate) -> Agent:
    data = payload.model_dump(exclude_unset=True)
    if data.get("model_id") is not None:
        await _validate_model(db, data["model_id"])
    # V42 protocol_md change → re-check forbidden words
    if "protocol_md" in data:
        new_kind = data.get("kind") or agent.kind
        await _v42_check_worker_protocol(db, new_kind, data["protocol_md"])
    for field, value in data.items():
        setattr(agent, field, value)
    await db.commit()
    return await get_agent(db, agent.id)  # type: ignore[return-value]


async def delete_agent(db: AsyncSession, agent: Agent) -> None:
    await db.delete(agent)
    await db.commit()


# ──────────────────── Skill 绑定 ────────────────────
async def add_skill(
    db: AsyncSession, agent_id: uuid.UUID, skill_id: uuid.UUID, config: dict | None = None
) -> AgentSkill:
    # 校验 Skill 存在
    skill = await db.execute(select(Skill).where(Skill.id == skill_id))
    if not skill.scalar_one_or_none():
        raise ValueError("skill 不存在")
    existing = await db.get(AgentSkill, (agent_id, skill_id))
    if existing:
        existing.config = config or {}
        await db.commit()
        return existing
    binding = AgentSkill(agent_id=agent_id, skill_id=skill_id, config=config or {})
    db.add(binding)
    await db.commit()
    return binding


async def remove_skill(db: AsyncSession, agent_id: uuid.UUID, skill_id: uuid.UUID) -> bool:
    binding = await db.get(AgentSkill, (agent_id, skill_id))
    if not binding:
        return False
    await db.delete(binding)
    await db.commit()
    return True


# ──────────────────── MCP 绑定 ────────────────────
async def add_mcp(
    db: AsyncSession,
    agent_id: uuid.UUID,
    mcp_server_id: uuid.UUID,
    tool_filter: list[str] | None = None,
) -> AgentMCPServer:
    existing = await db.get(AgentMCPServer, (agent_id, mcp_server_id))
    if existing:
        existing.tool_filter = tool_filter
        await db.commit()
        return existing
    binding = AgentMCPServer(
        agent_id=agent_id, mcp_server_id=mcp_server_id, tool_filter=tool_filter
    )
    db.add(binding)
    await db.commit()
    return binding


async def remove_mcp(db: AsyncSession, agent_id: uuid.UUID, mcp_server_id: uuid.UUID) -> bool:
    binding = await db.get(AgentMCPServer, (agent_id, mcp_server_id))
    if not binding:
        return False
    await db.delete(binding)
    await db.commit()
    return True


# ──────────────────── Aux Model 绑定 ────────────────────
async def add_aux_model(
    db: AsyncSession,
    agent_id: uuid.UUID,
    model_id: uuid.UUID,
    *,
    role: str = "custom",
    alias: str | None = None,
    config: dict | None = None,
) -> AgentAuxModel:
    await _validate_model(db, model_id)
    existing = await db.get(AgentAuxModel, (agent_id, model_id))
    if existing:
        existing.role = role
        existing.alias = alias
        existing.config = config or {}
        await db.commit()
        return existing
    binding = AgentAuxModel(
        agent_id=agent_id,
        model_id=model_id,
        role=role,
        alias=alias,
        config=config or {},
    )
    db.add(binding)
    await db.commit()
    return binding


async def remove_aux_model(db: AsyncSession, agent_id: uuid.UUID, model_id: uuid.UUID) -> bool:
    binding = await db.get(AgentAuxModel, (agent_id, model_id))
    if not binding:
        return False
    await db.delete(binding)
    await db.commit()
    return True


async def list_aux_models(db: AsyncSession, agent: Agent) -> list[AgentAuxModel]:
    return list(agent.aux_models)


# ──────────────────── Prompt 装配 ────────────────────
def _collect_static_prompt_parts(agent: Agent) -> list[str]:
    """全局段：当前时间 + Soul + Protocol + 已绑定的 instruction Skills。

    V7.0 · 当前时间放最前（每次调用新鲜生成）；agent 做时间相关决策（cron 自判今日是否已做）
    必须知道现在几点。
    """
    from app.domain.prompt_time import current_time_section
    parts: list[str] = [current_time_section()]
    if agent.soul_md:
        parts.append(agent.soul_md.strip())
    if agent.protocol_md:
        parts.append(agent.protocol_md.strip())
    for binding in agent.skills:
        s = binding.skill
        if s.skill_type == "instruction" and s.is_enabled and s.content_md.strip():
            parts.append(s.content_md.strip())
    return parts


def assemble_system_prompt(agent: Agent) -> str:
    """同步入口：仅用全局静态段（供 `/api/agents/{id}/test` dry-run 与单元测试）。"""
    parts = _collect_static_prompt_parts(agent)
    if agent.domain_memory_md:
        parts.append(agent.domain_memory_md.strip())
    return "\n\n".join(parts)


async def assemble_system_prompt_async(
    db: AsyncSession, agent: Agent, ctx: BuiltinToolContext
) -> str:
    """运行时入口：拼接全局段 + 当前分支 × Agent 的 memory.md。

    优先级：
    1. 全局 Soul / Protocol / instruction Skills（**所有分支共享**）
    2. 当前分支该 Agent 节点的 `BranchAgentMemory.memory_md`
    3. 若分支 memory 不存在且 `agent.domain_memory_md` 非空，把它作为初始模板
       （下次压缩时才会被真实记忆覆盖）
    """
    from app.services import memory_service

    parts = _collect_static_prompt_parts(agent)

    # ── 已绑定 MCP 能力清单（2026-07-05 xhs_publisher 双盲事故）──
    # MCP 工具虽然全量注入工具位，但提示词里没有清单+说明 → worker 声称/被认为
    # 「不具备」绑定 MCP 的能力。read-time 从 mcp_inventory（TTL 缓存）取，永不抛。
    try:
        from sqlalchemy import inspect as _sa_inspect
        if "mcp_servers" not in _sa_inspect(agent).unloaded and (agent.mcp_servers or []):
            from app.services import mcp_inventory as _mcp_inv
            _inv = await _mcp_inv.tools_for_agent(agent)
            if _inv:
                _lines = ["## 已绑定 MCP 能力（这些工具就在你的工具列表里，直接函数调用）"]
                for _srv, _tools in _inv.items():
                    _lines.append(f"### {_srv}")
                    _lines.extend(
                        f"- `{t['name']}`：{t.get('description') or ''}" for t in _tools
                    )
                _lines.append(
                    "以上是你的实际运行时能力——不要声称不具备；调用失败时把错误原文回报并重试合理参数。"
                )
                parts.append("\n".join(_lines))
    except Exception:  # noqa: BLE001
        logger.warning("[prompt] MCP 能力清单注入失败（跳过）agent=%s", agent.name, exc_info=True)

    # M3 + 2026-05-19：双轨记忆注入
    # - memory_scope='project'（daemon 模式）：只读 MissionAgentMemory
    # - memory_scope='branch'（orchestrator 模式）：**同时读** BranchAgentMemory + MissionAgentMemory，
    #   让 Builder Super 既能延续本次会话进度（branch），也能拿到跨 session 的项目长期学习（project）。
    branch_mem_md: str | None = None
    project_mem_md: str | None = None
    if ctx.agent_node_name:
        if ctx.memory_scope == "project" and ctx.mission_id:
            mem = await memory_service.get_project_memory(
                db, ctx.mission_id, ctx.agent_node_name
            )
            if mem and mem.memory_md:
                project_mem_md = mem.memory_md
        elif ctx.mission_id and ctx.thread_key:
            mem = await memory_service.get_thread_memory(
                db, ctx.mission_id, ctx.thread_key, ctx.agent_node_name
            )
            if mem and mem.memory_md:
                branch_mem_md = mem.memory_md
            # 额外读项目长期记忆（仅在 ctx.mission_id 存在时），让 builder super
            # 拿到「跨 session 学到的事」。
            if ctx.mission_id:
                pmem = await memory_service.get_project_memory(
                    db, ctx.mission_id, ctx.agent_node_name
                )
                if pmem and pmem.memory_md:
                    project_mem_md = pmem.memory_md

    if project_mem_md:
        parts.append("## 项目长期记忆（跨 session 累积）\n\n" + project_mem_md.strip())
    if branch_mem_md:
        parts.append("## 当前分支记忆（本会话累积）\n\n" + branch_mem_md.strip())
    if not project_mem_md and not branch_mem_md and agent.domain_memory_md and agent.domain_memory_md.strip():
        parts.append("## 领域初始记忆（模板）\n\n" + agent.domain_memory_md.strip())

    # ── Builder 专属：用户新开 builder mission（带 goal_hint）→ 注入「DESIGN_SUPER 会话 + 用户需求」。
    # 修 opened_by 死路由：builder 协议靠 opened_by 路由，但它从没被 surface 到 prompt，导致
    # builder 把每个 spawned mission 当空项目、落回 legacy 小红书模板。这里显式告诉它这是用户设计会话。
    if agent.category == "builder" and ctx.mission_id:
        from app.models.mission import Mission as _Mission
        # 已建过 super → 待命：本设计会话是一次性的，建完即转待命，防被 tick 重入重复提案/重建。
        # 但会话保活（super 升级 escalation 回投入口，见 escalation_dispatcher），仅升级时处理。
        from app.domain.builder.factory import existing_super_for_builder_mission
        _built = await existing_super_for_builder_mission(db, ctx.mission_id)
        if _built is not None:
            parts.append(
                "## ✅ 本设计会话已完成构建（待命模式）\n"
                f"本会话已建好 super=`{_built.display_name or _built.name}`（agent_id={_built.id}）。\n"
                "**已建完，进入待命：🚫 不要重复提案 / 不要重建 super / 不要再发『确认构建』审批卡。**\n"
                "仅当收到该 super 的升级请求（消息含 `[project-escalation from ...]`）时，才进入 "
                "DESIGN_WORKER 模式处理（升级/新建 worker → `resume_super_agent`）；否则本 tick 不动作、直接结束。"
            )
            return "\n\n".join(parts)

        _m = await db.get(_Mission, ctx.mission_id)
        _goal = (_m.workflow_config or {}).get("goal_hint") if _m and _m.workflow_config else None
        # chat 路径无 goal_hint → 从主线首条用户消息取需求（builder 的首条用户消息即设计请求）。
        # 这样整条 M2 构建管道每个 tick 都重新锚定真实领域，不会在 config-gen/assemble 阶段
        # 因上下文稀释而漂到 HR/小红书等示例。
        if (not _goal or not str(_goal).strip()) and ctx.mission_id:
            from app.models.message import Message as _Msg
            row = (await db.execute(
                select(_Msg.content)
                .where(_Msg.mission_id == ctx.mission_id, _Msg.thread_key == "main",
                       _Msg.role == "user")
                .order_by(_Msg.created_at.asc()).limit(1)
            )).scalar()
            if row:
                _goal = row
        if _goal and str(_goal).strip():
            parts.append(
                "## 🧭 本次设计会话（opened_by=user · 进入 DESIGN_SUPER 模式）\n"
                "用户开启这个 Builder 会话来设计一个 super，需求如下：\n\n"
                f"> {str(_goal).strip()}\n\n"
                "**这是本次构建唯一的领域来源。从规划到 config-gen 到 assemble 的『每一步 / 每个构建步骤』"
                "都必须严格围绕上面这个需求所属领域设计 super（含对应 worker + 调度 + 敏感动作审批）。"
                "🚫 严禁在任何步骤套用或漂移到示例领域（如小红书/内容运营/HR 简历筛选等）；"
                "🚫 严禁当成空项目空等需求——需求就在上面。每次 tick 都以此为准。**"
            )

    # ── Supervisor 专属：动态注入能力花名册（ADR-027 D2：required_capabilities，替代节点表）──
    if ctx.agent_node_name == "supervisor" and ctx.mission_id:
        cap_roster = await _build_capability_roster_snapshot(db, ctx.mission_id)
        if cap_roster:
            parts.append(cap_roster)

    return "\n\n".join(parts)


async def _build_capability_roster_snapshot(db: AsyncSession, mission_id) -> str:
    """为 Supervisor 动态渲染能力花名册（ADR-027 D2）。

    花名册来源：super 自己 `Agent.extra_config.required_capabilities`（声明在协议/spec）。
    运行时按能力解析（`invoke_worker capability:x`）+ `list_workers` 发现；缺则
    `request_new_capability`。不再读 mission_nodes 节点表。
    """
    from app.services import mission_service

    project = await mission_service.get_mission(db, mission_id)
    if not project or not project.supervisor_agent_id:
        return ""
    sup = await get_agent(db, project.supervisor_agent_id)
    if not sup:
        return ""
    caps = ((sup.extra_config or {}).get("required_capabilities")) or []

    lines = [
        "## 能力花名册（ADR-027：按 capability 调度，不再有节点表）",
        "",
    ]
    if caps:
        lines.append(f"本 super 声明需要以下 {len(caps)} 个能力（required_capabilities）：")
        for c in caps:
            if isinstance(c, dict):
                slug = c.get("capability") or c.get("slug") or c.get("name") or "?"
                hint = c.get("description") or c.get("hint") or ""
                lines.append(f"- `{slug}`" + (f" — {hint}" if hint else ""))
            else:
                lines.append(f"- `{c}`")
        lines.append("")
    else:
        lines.append("本 super 暂未在 spec 声明 required_capabilities（运行时按需发现即可）。")
        lines.append("")

    lines += [
        "**通用调度规则（capability dispatch）**：",
        "- 用 `invoke_worker('capability:<slug>', action, params)` 按能力调度一个平台 worker；"
        "多个并发用 `invoke_workers_parallel`。",
        "- 用 `list_workers`（可按 capability 过滤）发现平台现有 worker；不确定有没有先查它。",
        "- **缺能力**（list_workers 查不到合适 worker）→ 调 `request_new_capability` 升级 Builder，"
        "本 super 会自动 `paused_waiting_capability`，Builder 建/升级好 worker 后唤醒续跑。",
        "- 交付物：worker 调 `workspace_write` 上传 S3 后**在对话中内联展示**（用户可预览 + 下载）；"
        "没有单独的『Workspace 面板』，**绝不要**叫用户去『右侧面板』找产物。",
        "- 进度看 live worker 调用流（SSE）；要复用某 worker 的上次产出，直接再 invoke 它"
        "（worker 持久 thread 会带上历史上下文）。",
    ]
    return "\n".join(lines)


# ──────────────────── Tools 装配 ────────────────────
def load_builtin_tools(agent: Agent, ctx: BuiltinToolContext) -> list[BaseTool]:
    tools: list[BaseTool] = []
    # 事先检测该 Agent 绑定的 tool_builtin 里有没有被禁用的，打出 warning
    # 帮助运维在运行时发现"管理员刚停用了某 Skill，但还有 Agent 绑着它"
    for binding in agent.skills:
        s = binding.skill
        if s.skill_type == "tool_builtin" and not s.is_enabled:
            logger.warning(
                "⚠️ Agent %s 绑定了被禁用的 Skill %s (%s) —— 本次执行将缺少该工具；"
                "如下游 prompt 依赖它会出错。请管理员检查 Skills 管理",
                agent.name,
                s.slug,
                s.builtin_ref,
            )
    for binding in agent.skills:
        s = binding.skill
        if s.skill_type != "tool_builtin" or not s.is_enabled or not s.builtin_ref:
            continue
        factory = BUILTIN_TOOL_REGISTRY.get(s.builtin_ref)
        if not factory:
            logger.warning("未知 builtin_ref=%s (skill=%s)", s.builtin_ref, s.slug)
            continue
        tool = factory(ctx)
        tools.append(tool)
    return tools


async def load_mcp_tools(agent: Agent, ctx: BuiltinToolContext | None = None) -> list[BaseTool]:
    """N3.2：把绑定到 agent 的 MCPServer 转成 LangChain BaseTool 列表。

    实现：用 `langchain_mcp_adapters.MultiServerMCPClient` 统一管理多 MCP 服务器
    连接。每个工具调用都会建立新 session（HTTP streamable / stdio），
    保证 daemon 长跑场景不会持有过期连接。

    跳过 disabled / 缺连接配置的 MCPServer，记 warning 不抛错。失败的 MCP 不应
    阻塞整个 executor 装配（worker 仍有其他工具可用）。
    """
    bindings = list(agent.mcp_servers or [])
    if not bindings:
        return []

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain-mcp-adapters 未安装；MCP tools 跳过")
        return []

    connections: dict[str, dict] = {}
    for b in bindings:
        m = b.mcp_server
        if m is None or not m.is_enabled:
            continue
        if m.server_type == "http":
            if not m.url:
                logger.warning("[mcp] server %s 缺 url，跳过", m.name)
                continue
            connections[m.name] = {
                "url": m.url,
                "transport": "streamable_http",
                "headers": m.headers or {},
            }
        elif m.server_type == "stdio":
            if not m.command:
                logger.warning("[mcp] server %s 缺 command，跳过", m.name)
                continue
            connections[m.name] = {
                "command": m.command[0] if isinstance(m.command, list) else m.command,
                "args": m.command[1:] if isinstance(m.command, list) and len(m.command) > 1 else [],
                "transport": "stdio",
                "env": m.env_vars or {},
            }
        else:
            logger.warning("[mcp] server %s 未知 transport=%s，跳过", m.name, m.server_type)

    if not connections:
        return []

    async def _get_tools() -> list:
        client = MultiServerMCPClient(connections)
        return list(await client.get_tools())

    try:
        tools = await _get_tools()
    except Exception:  # noqa: BLE001
        # Tier 1 (load-time) · unreachable server → autostart respawn + one retry.
        logger.warning("[mcp] get_tools 首次失败 agent=%s，尝试 autostart 重连", agent.name)
        try:
            from app.db import session as _db_session
            from app.services import mcp_autostart
            async with _db_session.AsyncSessionLocal() as _db:
                await mcp_autostart.autostart_local_mcp_servers(_db)
            tools = await _get_tools()
        except Exception:  # noqa: BLE001
            logger.exception("[mcp] get_tools 重连仍失败 agent=%s", agent.name)
            # Tier 2/3 · record a degradation signal to Worker-Opt (it can escalate to Builder).
            try:
                if ctx is not None and getattr(ctx, "db_factory", None) is not None:
                    from app.services import worker_health_service
                    async with ctx.db_factory() as _db2:
                        await worker_health_service.record_worker_issue(
                            _db2, capability="mcp:" + ",".join(connections.keys()),
                            evidence="MCP 服务器装载时不可达，autostart 重连失败，可能需 Builder 修复绑定/配置。",
                            severity="critical", source="mcp_load",
                        )
            except Exception:
                logger.exception("[mcp] load-time worker-opt report failed (不阻塞)")
            return []

    from app.services.mcp_self_repair import wrap_mcp_tools
    wrap_mcp_tools(tools, ctx=ctx)  # tier-1 call-time retry + tier-2 report wrapper
    logger.info(
        "[mcp] agent=%s 装载 %d 个 MCP tool（来自 %d 个 server，已挂自修复）",
        agent.name, len(tools), len(connections),
    )
    return tools


# ──────────────────── Executor 构建 ────────────────────
async def build_agent_executor(
    db: AsyncSession,
    agent: Agent,
    *,
    ctx: BuiltinToolContext,
    checkpointer: Any = None,
    llm_override: BaseChatModel | None = None,
) -> Any:
    """构建 `langchain.agents.create_agent` 实例（底层 LangGraph CompiledStateGraph）。

    - 优先使用 `llm_override`（供测试注入 FakeChatModel）
    - 无 override 时从 DB 取出 provider + LiteLLM 懒加载
    - System Prompt = soul + protocol + instruction skills + 当前分支该 Agent 的 memory.md
      （domain_memory_md 仅作为新分支首次运行时的初始化模板；运行时真正的 domain 记忆
       存于 `BranchAgentMemory`）
    """
    from langchain.agents import create_agent

    if llm_override is None:
        model = await _resolve_agent_model(db, agent)
        llm = await _build_llm(db, model, agent)
    else:
        llm = llm_override

    if ctx.extra is None:
        ctx.extra = {}
    ctx.extra["agent_id"] = agent.id

    # 把当前 Agent 的交付物属性注入 ctx，供 workspace_write 等工具按分支行为落地
    ctx.produces_deliverable = bool(agent.produces_deliverable)
    ctx.agent_id = agent.id
    # 安全护栏：Supervisor 节点（agent_node_name='supervisor'）绝不允许作为交付物 Agent
    # 即使管理员误勾，运行时也强制关闭，避免产物写到不存在的节点
    if ctx.agent_node_name == "supervisor":
        ctx.produces_deliverable = False

    system_prompt = await assemble_system_prompt_async(db, agent, ctx)
    tools = load_builtin_tools(agent, ctx)
    mcp = await load_mcp_tools(agent, ctx)
    tools.extend(mcp)

    kwargs: dict[str, Any] = {"system_prompt": system_prompt} if system_prompt else {}
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer

    return create_agent(llm, tools=tools, **kwargs)


async def _build_llm(db: AsyncSession, model: LLMModel, agent: Agent) -> BaseChatModel:
    """从 LLMProvider 配置构造 ChatLiteLLM。

    按 provider_type 选择 LiteLLM 路由；api_base **全部走 per-call kwargs**，
    避免多 provider 并发时污染 `os.environ['GEMINI_API_BASE']` 这种进程级全局。
    """
    from app.core.encryption import decrypt
    from app.models.provider import LLMProvider
    from app.services.resilient_llm import ResilientChatLiteLLM as ChatLiteLLM

    provider = await db.get(LLMProvider, model.provider_id)
    if not provider:
        raise ValueError(f"Provider {model.provider_id} 不存在")
    api_key = decrypt(provider.api_key)

    # LiteLLM 路由构造（决定走哪个原生 SDK / HTTP 路径）：
    # - **非 custom provider**（anthropic / gemini / openai / …）：用 `<type>/<model_id>`
    #   让 LiteLLM 走对应家族的原生 SDK 路径
    # - **custom provider**（Nebula 等 OpenAI-compat 代理）：按 model_id 前缀再决策
    #   * `claude-*`：保留裸 model_id，走 LiteLLM 的 **Anthropic** 路径 —— 它支持 api_base 覆盖，
    #     所以 Nebula 的 /v1/messages 能命中（配合 model_kwargs["extra_body"]={"stream":True}
    #     强制请求体带 stream=true 才会返回 SSE）
    #   * `gemini-*`：**必须**强制 `openai/` 前缀，否则 LiteLLM 会识别成 Vertex AI 走 Google
    #     Cloud SDK（需要 `google-auth` 包 + ADC 凭据），忽略 api_base 直连 Google → 失败
    #     `ModuleNotFoundError: No module named 'google'`
    #   * 其它（gpt-* / deepseek-* / 不识别的新模型）：也走 openai/ 前缀更稳
    # OpenAI-compat 代理类 provider（custom / dashscope / volcengine / aliyun）统一走 openai/ 前缀，
    # 让 LiteLLM 走 OpenAI-compat HTTP 路径打到 provider.base_url。
    # custom + claude-* 仍保留 Anthropic 路径以支持 nebula 代理 claude 走 /v1/messages。
    # R3-3 · 路由 + 流式开关走纯函数 provider_router（矩阵可独立测）
    from app.domain.llm.provider_router import resolve_route, should_stream
    route = resolve_route(provider.provider_type, model.model_id)

    # ── streaming 开关 ──
    # 默认开，让 astream_events 能收到 on_chat_model_stream 逐 token 事件。
    # 但 Nebula + Gemini 的 streaming 适配**带 tools 时会丢全部 content/tool_calls**
    # （实测 curl：首 chunk finish_reason=stop 空 delta，usage 里 completion_tokens > 0 但
    #  delta 里啥都没有；非流式同场景 tool_calls 正确返回）。
    # Claude on Nebula 靠 extra_body={"stream":True} 额外强制才能拿到 SSE，也属于同一 family 问题。
    # 所以对"custom provider + gemini-*"场景强制降级为非流式：ChatLiteLLM 走 _agenerate，
    # 一次性拿完整响应（包括 tool_calls），LangGraph 的 astream_events 只少 on_chat_model_stream
    # 逐 token 事件，不影响功能（流程正确性 > 打字机效果）。
    _use_streaming = should_stream(provider.provider_type, model.model_id)

    kwargs: dict[str, object] = {
        "model": route,
        "api_key": api_key,
        "temperature": agent.temperature,
        # 单次调用输出 token 上限：防止"整份 Markdown 一口气输出 14k token × 54 tok/s = 260s"
        # 击穿 Worker 预算。命中 length 上限时由 ResilientChatLiteLLM 自动续写（纯文本场景）
        "max_tokens": int(getattr(agent, "max_output_tokens", 30000) or 30000),
        "streaming": _use_streaming,
    }
    # base_url 透传到 LiteLLM `api_base`。
    # 注意：不同 SDK 路径对 base_url 的尾部路径期望不同：
    # - Anthropic SDK 路径（Claude 场景）：会自己 append `/v1/messages`，需要 base_url
    #   **不带** /v1（如 `https://llm.ai-nebula.com`）
    # - OpenAI SDK 路径（Gemini/GPT 等走 openai/ 前缀的场景）：会 append `/chat/completions`，
    #   需要 base_url **带** /v1（如 `https://llm.ai-nebula.com/v1`）
    # 为了让用户只在 Provider 表里填一个 base_url（不带 /v1）就能兼容两种路径，这里在
    # openai/ 路由时自动补 /v1，对 Claude 等其它路径保持原样。
    if provider.base_url:
        if route.startswith("openai/"):
            import re as _re
            base_no_slash = provider.base_url.rstrip("/")
            # 已含 `/vN` 或 `/vN/...` 版本段（如火山 `/api/v3` / 阿里 `/compatible-mode/v1`）
            # 直接用，不补 /v1，否则 URL 变 `.../api/v3/v1/chat/completions` 拿 404
            already_versioned = bool(_re.search(r"/v\d+(/|$)", base_no_slash))
            kwargs["api_base"] = (
                base_no_slash if already_versioned else base_no_slash + "/v1"
            )
        else:
            kwargs["api_base"] = provider.base_url

    # ── 思考预算控制（按 Agent 粒度）──
    # Agent.enable_thinking 决定是否开启模型内置 reasoning / thinking。默认 False。
    # 关闭原因：Agent 自身 ReAct Loop 已具备规划能力；thinking tokens 不会流出
    # astream_events 的 on_chat_model_stream（前端白屏等待）；首 token 延迟显著降低。
    #
    # True 时不注入任何 thinking 字段，走 provider 默认值或 Agent.extra_config 覆盖。
    #
    # 按主模型真实家族（而不是 provider_type）下发最稳的关闭思考参数：
    #   Claude 家族（anthropic 直连 / openai-compat 代理比如 Nebula → model_id 以 claude 开头）：
    #     注入 thinking={"type":"disabled"}，**不**注入 reasoning_effort
    #     —— 实测 Nebula + Claude Sonnet 在 reasoning_effort=minimal 单独存在时会触发
    #     上游 Anthropic 502 BadGateway（LiteLLM 把它映射成 thinking.budget_tokens 触发兼容性问题）
    #   Gemini: extra_body.generationConfig.thinkingConfig.thinkingLevel="off" + reasoning_effort=minimal 兜底
    #   DeepSeek V4（deepseek-v4-pro/flash）: extra_body={"thinking":{"type":"disabled"}}
    #     —— thinking 默认开，官方 thinking_mode 文档唯一关法；reasoning_effort 对 DeepSeek 无效
    #   其它（openai o-series / azure / ollama / 其它 custom）: reasoning_effort="low"
    #
    # ⚠️ 关键：ChatLiteLLM 的 pydantic schema 不识别 `thinking` / `reasoning_effort` / `extra_body`
    # 这类"非标准字段"，直接 kwargs 传入会被**静默丢弃**。必须通过 `model_kwargs={...}`
    # 作为字典传入，运行时才会透传到 litellm.completion(**model_kwargs)。
    # R4-2 · per-family 思考档位映射已抽到纯函数 thinking_policy（矩阵可独立测）
    # thinking_level（off/low/medium/high）是权威控制；旧 enable_thinking 仅作回退：
    # thinking_level 缺省（旧数据/未传）时 True→medium、False→off。
    from app.domain.llm.thinking_policy import compute_thinking_model_kwargs
    _level = (getattr(agent, "thinking_level", None) or "").lower()
    if _level not in ("off", "low", "medium", "high"):
        _level = "medium" if bool(getattr(agent, "enable_thinking", False)) else "off"
    model_kwargs: dict[str, Any] = compute_thinking_model_kwargs(
        level=_level,
        provider_type=provider.provider_type,
        model_id=model.model_id,
        route=route,
    )

    # Agent.extra_config 里的自定义透传参数：
    # - ChatLiteLLM 已知字段（如 top_p / max_tokens / request_timeout）→ 顶层 kwargs
    # - 其它一律进 model_kwargs（如用户手工 thinking={"type":"enabled","budget_tokens":...}
    #   覆盖默认禁用；或 reasoning_effort="high" 覆盖默认 minimal）
    # 覆盖逻辑：用户的 extra_config 以最高优先级写入 model_kwargs
    _CHATLITELLM_TOP_LEVEL_FIELDS = {
        "top_p",
        "top_k",
        "n",
        "max_tokens",
        "num_ctx",
        "request_timeout",
        "extra_headers",
        "stream_options",
    }
    extra_cfg = agent.extra_config or {}
    if isinstance(extra_cfg, dict):
        for k, v in extra_cfg.items():
            if v is None:
                continue
            if k in _CHATLITELLM_TOP_LEVEL_FIELDS:
                kwargs[k] = v
            else:
                model_kwargs[k] = v

    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    return ChatLiteLLM(**kwargs)
