"""Mission 业务服务 + LangGraph 图编译校验。

Colony 共享工作台：所有登录用户都能看到所有项目；无 ACL 过滤。
原 toystory-agents 的 access_mode / project_user_access / 白名单已删除。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.schemas.mission import (
    MissionCreate,
    MissionUpdate,
)

logger = logging.getLogger(__name__)


# ──────────────────── CRUD ────────────────────
async def list_missions(db: AsyncSession) -> Sequence[Mission]:
    result = await db.execute(select(Mission).order_by(Mission.created_at.desc()))
    return result.scalars().all()


async def list_accessible_missions(db: AsyncSession, user: User) -> Sequence[Mission]:
    """列出当前用户可访问的 active 项目（共享工作台：所有登录用户看到全部）。"""
    del user  # 共享模型不再按用户过滤；保留参数兼容调用方
    stmt = select(Mission).where(Mission.status == "active").order_by(Mission.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


async def list_accessible_mission_ids(
    db: AsyncSession,
    user: User,
    project_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    del user  # 共享模型不再按用户过滤
    if not project_ids:
        return set()
    stmt = select(Mission.id).where(
        Mission.id.in_(project_ids),
        Mission.status == "active",
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def get_mission(db: AsyncSession, mission_id: uuid.UUID) -> Mission | None:
    result = await db.execute(select(Mission).where(Mission.id == mission_id))
    return result.scalar_one_or_none()


async def get_mission_by_slug(db: AsyncSession, slug: str) -> Mission | None:
    result = await db.execute(select(Mission).where(Mission.slug == slug))
    return result.scalar_one_or_none()


async def get_mission_built_by_mission(
    db: AsyncSession, mission_id: uuid.UUID
) -> Mission | None:
    """ADR-018 step5/S · 取某 builder mission 建造出的 super 项目（取代 Session.target_project_id）。

    provenance 走 `Agent.built_by_mission_id`（super 创建时写）：找 supervisor agent 的
    built_by_mission_id == 该 builder mission 的项目。单-super 不变量保证至多一个，取最新。"""
    from app.models.agent import Agent

    return (
        await db.execute(
            select(Mission)
            .join(Agent, Agent.id == Mission.supervisor_agent_id)
            .where(Agent.built_by_mission_id == mission_id)
            .order_by(Mission.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def resolve_mission_id(db: AsyncSession, project_id_or_slug: str) -> uuid.UUID | None:
    """LLM 工具调用容错：接受 UUID 字符串或 slug，返回 UUID。

    LLM 在 Builder 协议里见过 slug 后倾向于继续用 slug 调 mission_get / mission_run_test 等
    工具，这层兜底防止 `uuid.UUID(...)` 直接 ValueError「badly formed hexadecimal UUID string」。
    """
    s = (project_id_or_slug or "").strip()
    if not s:
        return None
    try:
        return uuid.UUID(s)
    except (ValueError, TypeError):
        pass
    proj = await get_mission_by_slug(db, s)
    return proj.id if proj else None


async def check_user_can_access_mission(db: AsyncSession, project: Mission, user: User) -> bool:
    """共享工作台：所有登录用户都能访问 active 项目；admin 还能看 draft / archived。"""
    if user.role == "admin":
        return True
    return project.status == "active"


async def _require_agent(db: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} 不存在")
    if not agent.is_enabled:
        raise ValueError(f"Agent {agent.name} 已停用")
    return agent


async def create_mission(
    db: AsyncSession, payload: MissionCreate, created_by: uuid.UUID
) -> Mission:
    await _require_agent(db, payload.supervisor_agent_id)
    project = Mission(
        name=payload.name,
        description=payload.description,
        slug=payload.slug,
        supervisor_agent_id=payload.supervisor_agent_id,
        auto_approve=payload.auto_approve,
        context_compression_threshold=payload.context_compression_threshold,
        status="draft",
        created_by=created_by,
    )
    db.add(project)
    await db.commit()

    # 自动为新项目建一个专属 KB（slug 命名）。失败不阻塞 project 创建
    # —— KB 缺失只导致 knowledge_search 走兜底路径，不影响项目运转。
    try:
        await _ensure_mission_kb(db, project, created_by)
    except Exception:  # noqa: BLE001
        logger.exception("[create_mission] 为新项目 %s 创建 KB 失败（不阻塞）", project.slug)

    return await get_mission(db, project.id)  # type: ignore[return-value]


async def _ensure_mission_kb(
    db: AsyncSession, project: Mission, created_by: uuid.UUID
) -> None:
    """为 project 创建专属 KB（幂等）。
    embedding 模型用 settings.DEFAULT_EMBEDDING_MODEL_ID；没配置就找第一个 enabled 的。"""
    from app.core.config import settings
    from app.models.knowledge import KnowledgeBase
    from app.models.provider import LLMModel
    from app.services import knowledge_service

    # ADR-023 S7 · per-super：同一 super 已有 KB 就复用（该 super 所有 mission 共用一份）
    sup_id = project.supervisor_agent_id
    existing = await knowledge_service.get_kb_by_super(db, sup_id)
    if existing is not None:
        return

    model_id: uuid.UUID | None = None
    # ADR-023 S7 · 优先 onboarding 设的默认 embedding 模型（system_settings），再回退 env。
    # 用 system_settings.get（portable，跨 pg/sqlite）而非 pg 专有 JSONB 取值，避免测试 DB 报错。
    from app.core import system_settings as _ss
    ss_emb = await _ss.get(db, "default_embedding_model_id", None)
    default_id = (ss_emb or getattr(settings, "DEFAULT_EMBEDDING_MODEL_ID", "") or "")
    if default_id:
        # 支持 uuid / provider_name/model_id / model_id（与 supervisor/agent 默认模型一致）
        from app.domain.onboarding.default_model import _resolve_spec
        rm = await _resolve_spec(db, default_id)
        if rm is not None and rm.model_type == "embedding":
            model_id = rm.id
    if model_id is None:
        r = await db.execute(
            select(LLMModel).where(
                LLMModel.is_enabled.is_(True), LLMModel.model_type == "embedding"
            ).limit(1)
        )
        m = r.scalar_one_or_none()
        if m is None:
            # ADR-023 S7 · embedding gate：不再静默跳过，明确告警引导配置（否则 KB 永远空）
            logger.error(
                "[_ensure_super_kb] ⚠️ 系统无 enabled embedding 模型 → 无法为 super=%s 建知识库；"
                "请在『LLM 提供商』里配置并启用一个 embedding 模型，否则 knowledge_search 永远查不到经验",
                sup_id,
            )
            return
        model_id = m.id

    # per-super：name/collection 按 super，不绑单 mission（mission_id=None）；name/collection 唯一
    kb_name = f"kb-super-{str(sup_id).replace('-', '')[:12]}"
    collection = f"super_{str(sup_id).replace('-', '')[:24]}"
    await knowledge_service.create_kb(
        db,
        name=kb_name,
        description="super 共享知识库（自动创建；同 super 所有 mission 共用，沉淀经验）。",
        collection_name=collection,
        embedding_model_id=model_id,
        created_by=created_by,
        super_agent_id=sup_id,
        mission_id=None,
        tags=["auto", "per-super"],
        purpose="super-scoped",
    )
    logger.info(
        "[_ensure_super_kb] 为 super=%s 自动创建共享 KB %s", sup_id, kb_name
    )


async def update_mission(db: AsyncSession, project: Mission, payload: MissionUpdate) -> Mission:
    data = payload.model_dump(exclude_unset=True)
    if "supervisor_agent_id" in data:
        await _require_agent(db, data["supervisor_agent_id"])
    for field, value in data.items():
        setattr(project, field, value)
    await db.commit()
    return await get_mission(db, project.id)  # type: ignore[return-value]


async def delete_mission(db: AsyncSession, project: Mission) -> None:
    """删除项目（仅项目本体；不级联删 agent）。

    级联表（mission_nodes / mission_schedule / wechat_outbox / pending_approvals / sessions...）
    依赖 DB FK 的 ondelete=CASCADE 自动清理。

    如需同时删独占 agent，用 `delete_mission_with_optional_cascade_agents(cascade_agents=True)`。
    """
    await db.delete(project)
    await db.commit()


async def delete_mission_with_optional_cascade_agents(
    db: AsyncSession,
    project: Mission,
    *,
    cascade_agents: bool,
) -> dict:
    """删项目 + 可选级联删项目独占的 supervisor agent（ADR-027）。

    返回结构（前端 confirm-toast 用）：
      {
        deleted_project: "<slug or name>",
        cascade_agents: bool,
        deleted_agents: [<"name(id=...)" ...>],   # 实际被删的
        skipped_shared_or_failed: [...],          # 被其他 project 共享 → 保留；删失败 → 报错
      }

    ADR-027 节点版退役：worker 是平台级共享资源（按 capability 全局发现，不再按 mission
    预绑 mission_nodes），因此**不再级联删 worker**——没有「本 mission 独占 worker」的所属关系。
    级联只删本 mission 独占的 supervisor agent（若不被其它 mission 监管且非系统对象）。
    """
    from sqlalchemy import select
    from app.models.agent import Agent

    project_name = project.name or project.slug

    candidate_agent_ids: set = set()
    if cascade_agents and project.supervisor_agent_id:
        candidate_agent_ids.add(project.supervisor_agent_id)

    await delete_mission(db, project)

    deleted_agents: list[str] = []
    skipped: list[str] = []
    if cascade_agents and candidate_agent_ids:
        for aid in candidate_agent_ids:
            a = await db.get(Agent, aid)
            if a is None:
                continue
            # 系统对象（Builder Supervisor / Worker Optimization 等 is_system）永不级联删——
            # 否则删掉某个 Builder 设计会话/系统 mission 会把系统 super 一起带走（曾真出过）。
            if a.is_system:
                skipped.append(f"{a.name}(id={a.id})[is_system]")
                continue
            # 仍被其它 mission 监管 → 共享 → 保留
            other_sup = (await db.execute(
                select(Mission.id).where(Mission.supervisor_agent_id == aid).limit(1)
            )).first()
            if other_sup:
                skipped.append(f"{a.name}(id={a.id})")
                continue
            try:
                await db.delete(a)
                await db.commit()
                deleted_agents.append(f"{a.name}(id={a.id})")
            except Exception as exc:
                skipped.append(f"{a.name}(id={a.id}, 删除失败: {exc})")

    return {
        "deleted_project": project_name,
        "cascade_agents": cascade_agents,
        "deleted_agents": deleted_agents,
        "skipped_shared_or_failed": skipped,
    }


def _capability_haystack(agent) -> str:
    """super 引用 worker 的全部文本面：protocol_md + extra_config（花名册可能只在
    extra_config.required_capabilities，protocol 里不含 slug——真出过 4 个 worker 全漏扫）。"""
    import json

    parts = [getattr(agent, "protocol_md", None) or ""]
    extra = getattr(agent, "extra_config", None)
    if extra:
        try:
            parts.append(json.dumps(extra, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return "\n".join(parts)


def _references_capability(haystack: str, capability: str) -> bool:
    """引用面里是否出现某 capability（词边界，防 'xhs' 误配 'xhs_writer'）。"""
    import re

    if not haystack or not capability:
        return False
    return re.search(rf"\b{re.escape(capability)}\b", haystack) is not None


async def _scan_super_workers(db: AsyncSession, super_agent) -> tuple[list, list[dict]]:
    """找出 super 的成员 worker，分成 (独占可删, 保留[{name,reason}])。

    成员关系的活链接 = super 的 protocol_md / extra_config 里的 capability 引用
    （invoke_worker 的派发依据 + required_capabilities 花名册）。
    不用 built_by_mission_id：它对 worker 恒 NULL，且 builder mission 被删后 SET NULL 断链。
    独占 = 非 is_system 且没有其他 super 引用同一 capability。
    """
    from app.models.agent import Agent

    haystack = _capability_haystack(super_agent)
    workers = (await db.execute(
        select(Agent).where(Agent.kind == "worker", Agent.capability.is_not(None))
    )).scalars().all()
    members = [w for w in workers if _references_capability(haystack, w.capability)]
    if not members:
        return [], []

    other_supers = (await db.execute(
        select(Agent).where(Agent.kind == "super", Agent.id != super_agent.id)
    )).scalars().all()
    other_haystacks = [_capability_haystack(s) for s in other_supers]

    to_delete, to_keep = [], []
    for w in members:
        if w.is_system:
            to_keep.append({"name": w.name, "reason": "system"})
        elif any(_references_capability(h, w.capability) for h in other_haystacks):
            to_keep.append({"name": w.name, "reason": "shared"})
        else:
            to_delete.append(w)
    return to_delete, to_keep


async def preview_super_cascade(db: AsyncSession, super_agent) -> dict:
    """预览级联删 super 的影响（不删任何东西）。

    返回 {super_name, mission_count, missions[], workers_to_delete[], workers_to_keep[{name,reason}]}。

    worker 归属按 protocol 的 capability 引用判定（见 _scan_super_workers）——
    ADR-027 曾把 workers_to_delete 一刀切恒空，导致删 super 留下整队孤儿 worker。
    """
    super_id = super_agent.id
    missions = (await db.execute(
        select(Mission).where(Mission.supervisor_agent_id == super_id)
    )).scalars().all()
    to_delete, to_keep = await _scan_super_workers(db, super_agent)

    return {
        "super_name": super_agent.display_name or super_agent.name,
        "mission_count": len(missions),
        "missions": [m.name for m in missions],
        "workers_to_delete": [w.name for w in to_delete],
        "workers_to_keep": to_keep,
    }


async def delete_super_with_cascade(db: AsyncSession, super_agent) -> dict:
    """删一个 super agent + 它名下所有 Mission + 各 Mission 独占的 worker + super 本体。

    用户在 Agent 列表删一个仍有运营实例的 super 时走这条（前端强 confirm 后传 ?cascade=true）。

    级联策略（复用 delete_mission_with_optional_cascade_agents 的安全语义）：
    - 逐个删 super 名下的 Mission（cascade_agents=True）：每个 Mission 的 schedule /
      pending_approvals / mission_agent_memory 由 DB FK ondelete=CASCADE 自动清。
    - 删该 super 独占的 worker（protocol 引用其 capability 且无其他 super 引用，
      见 _scan_super_workers；is_system / shared 保留并记入 skipped）。
    - 删到最后一个 Mission 时 super 已无引用 → 由同一级联逻辑顺带删掉（除非 is_system）。
    - 兜底：若 super 仍残留（如 0 Mission 直接级联删），且非 is_system，显式删之
      （agent_skills / agent_mcp_servers / agent_aux_models / protocol 历史均 FK CASCADE）。

    返回 {deleted_super, deleted_missions, deleted_agents, skipped} 供前端 toast。
    """
    from sqlalchemy import select
    from app.models.agent import Agent

    super_name = super_agent.display_name or super_agent.name
    super_id = super_agent.id
    # 先扫描 worker 归属（依赖 super.protocol_md，须在 super 被级联删掉之前算）
    workers_to_delete, workers_kept = await _scan_super_workers(db, super_agent)

    missions = (await db.execute(
        select(Mission).where(Mission.supervisor_agent_id == super_id)
    )).scalars().all()

    deleted_missions: list[str] = []
    deleted_agents: list[str] = []
    skipped: list[str] = []
    for m in missions:
        res = await delete_mission_with_optional_cascade_agents(db, m, cascade_agents=True)
        deleted_missions.append(res["deleted_project"])
        deleted_agents.extend(res["deleted_agents"])
        skipped.extend(res["skipped_shared_or_failed"])

    # 删独占 worker（扫描结果在 super 删除前已定格）
    for w in workers_to_delete:
        try:
            await db.delete(w)
            await db.commit()
            deleted_agents.append(f"{w.name}(id={w.id})")
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{w.name}(id={w.id}, 删除失败: {exc})")
    skipped.extend(f"{k['name']}[{k['reason']}]" for k in workers_kept)

    # 兜底删 super 本体（级联可能已删；0 mission 时必走这里）
    still = await db.get(Agent, super_id)
    if still is not None and not still.is_system:
        await db.delete(still)
        await db.commit()
        if f"{still.name}(id={still.id})" not in deleted_agents:
            deleted_agents.append(f"{still.name}(id={still.id})")

    return {
        "deleted_super": super_name,
        "deleted_missions": deleted_missions,
        "deleted_agents": deleted_agents,
        "skipped": skipped,
    }


async def bulk_update_mission_models(
    db: AsyncSession,
    project: Mission,
    supervisor_model_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """覆盖 mission supervisor 的主模型（ADR-027 · worker 不再按 mission 预绑）。

    - `supervisor_model_id`：如非 None，更新 `project.supervisor_agent_id` 指向 Agent 的 model_id

    返回：实际被改了的 supervisor agent id 或 None。
    """
    from app.models.provider import LLMModel  # 局部 import 避免循环

    async def _require_model(mid: uuid.UUID) -> None:
        m = await db.get(LLMModel, mid)
        if not m:
            raise ValueError(f"模型 {mid} 不存在")
        if not m.is_enabled:
            raise ValueError(f"模型 {mid} 已禁用")

    updated_supervisor: uuid.UUID | None = None

    if supervisor_model_id is not None:
        await _require_model(supervisor_model_id)
        sup = await db.get(Agent, project.supervisor_agent_id)
        if sup is None:
            raise ValueError("Supervisor Agent 不存在")
        if sup.model_id != supervisor_model_id:
            sup.model_id = supervisor_model_id
            updated_supervisor = sup.id

    await db.commit()
    return updated_supervisor


# ──────────────────── 激活校验 ────────────────────
def validate_workflow(project: Mission) -> list[str]:
    """Mission 激活前的合法性校验，返回 issue 列表（ADR-027 · 仅校验 supervisor）。"""
    issues: list[str] = []

    if not project.supervisor_agent_id:
        issues.append("未指定 Supervisor Agent")
    # ADR-027：worker 按 capability 运行时发现，不再有节点声明层，无需校验节点。

    return issues


async def activate_mission(db: AsyncSession, project: Mission) -> tuple[bool, list[str]]:
    issues = validate_workflow(project)
    if issues:
        return False, issues
    project.status = "active"
    await db.commit()
    return True, []


async def deactivate_mission(db: AsyncSession, project: Mission) -> None:
    project.status = "draft"
    await db.commit()


async def compile_mission_graph(db: AsyncSession, project: Mission, checkpointer=None):
    """占位：Phase 6 会话引擎实际执行时调用。

    此处仅验证结构，不预编译（因为 supervisor node 的实现依赖 session 运行时数据）。
    """
    issues = validate_workflow(project)
    if issues:
        raise ValueError("工作流校验失败：" + "; ".join(issues))
    # TODO(Phase 6): 使用 langgraph.graph.StateGraph 组装
    return {"mission_id": str(project.id)}
