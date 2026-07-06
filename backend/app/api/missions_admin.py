"""Mission CRUD + 激活（ADR-027 · 节点版退役，无节点 CRUD）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import AdminUser, CurrentUser, DBSession
from app.models.mission import Mission
from app.schemas.mission import (
    MissionActivationResponse,
    MissionBulkModelUpdate,
    MissionBulkModelUpdateResponse,
    MissionCreate,
    MissionDetail,
    MissionLifecycleAction,
    MissionPublic,
    MissionRuntimePublic,
    MissionUpdate,
)
from app.services import mission_daemon, mission_service

router = APIRouter(prefix="/api/missions", tags=["missions-admin"])


def _to_detail(project: Mission) -> MissionDetail:
    return MissionDetail.model_validate(
        {c.name: getattr(project, c.name) for c in project.__table__.columns}
    )


@router.get("/all", response_model=list[MissionPublic])
async def list_missions(_: AdminUser, db: DBSession) -> list[MissionPublic]:
    items = await mission_service.list_missions(db)
    return [MissionPublic.model_validate(p) for p in items]


@router.post("/full", response_model=MissionDetail, status_code=status.HTTP_201_CREATED)
async def create_mission(payload: MissionCreate, admin: AdminUser, db: DBSession) -> MissionDetail:
    exists = await db.execute(select(Mission).where(Mission.slug == payload.slug))
    if exists.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="slug 已被使用")
    try:
        project = await mission_service.create_mission(db, payload, created_by=admin.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_detail(project)


# ── 普通用户可见：列出所有 active 项目（放在 /{mission_id} 前避免路径冲突）──
@router.get("/active", response_model=list[MissionPublic])
async def list_active_projects(
    user: CurrentUser, db: DBSession
) -> list[MissionPublic]:
    """任何登录用户可访问；仅返回当前用户可访问的 active 项目。"""
    items = await mission_service.list_accessible_missions(db, user)
    return [MissionPublic.model_validate(p) for p in items]


@router.get("/detail/{mission_id}", response_model=MissionDetail)
async def get_mission(mission_id: uuid.UUID, _: AdminUser, db: DBSession) -> MissionDetail:
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    return _to_detail(project)


@router.put("/{mission_id}", response_model=MissionDetail)
async def update_mission(
    mission_id: uuid.UUID, payload: MissionUpdate, _: AdminUser, db: DBSession
) -> MissionDetail:
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    try:
        updated = await mission_service.update_mission(db, project, payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_detail(updated)


class MissionDeleteResponse(BaseModel):
    """删除项目响应。仅当 cascade_agents=True 时 deleted_agents / skipped_shared_or_failed 非空。"""

    deleted_project: str
    cascade_agents: bool
    deleted_agents: list[str] = Field(default_factory=list)
    skipped_shared_or_failed: list[str] = Field(default_factory=list)


@router.delete("/{mission_id}", response_model=MissionDeleteResponse)
async def delete_mission(
    mission_id: uuid.UUID,
    _: AdminUser,
    db: DBSession,
    cascade_agents: bool = False,
) -> MissionDeleteResponse:
    """删除项目。

    Query 参数 `cascade_agents`（默认 False）：是否同时删项目独占的 supervisor + worker Agent。
    True 时只删不再被其他 Mission 共享的 agent；被共享或删失败的进 skipped_shared_or_failed。
    """
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    # ADR-015 · 平台系统对象（Builder Mission）不可删除
    if getattr(project, "is_system", False):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="平台系统对象（Builder Mission），不可删除",
        )
    result = await mission_service.delete_mission_with_optional_cascade_agents(
        db, project, cascade_agents=cascade_agents
    )
    return MissionDeleteResponse(**result)


# ── 批量覆盖 Agent 主模型 ──
@router.post(
    "/{mission_id}/bulk-update-models",
    response_model=MissionBulkModelUpdateResponse,
)
async def bulk_update_mission_models(
    mission_id: uuid.UUID,
    payload: MissionBulkModelUpdate,
    _: AdminUser,
    db: DBSession,
) -> MissionBulkModelUpdateResponse:
    """覆盖 mission Supervisor Agent 的主模型（ADR-027 · worker 不再按 mission 预绑）。

    - `supervisor_model_id`：覆盖 `mission.supervisor_agent_id` 指向的 Agent

    Agent 是共享资源（可能被其它 Mission 引用）——**这是明确的"便利性接口"，语义上就是改 Agent.model_id**。
    """
    if payload.supervisor_model_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="supervisor_model_id 不能为空",
        )
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    try:
        sup_id = await mission_service.bulk_update_mission_models(
            db,
            project,
            supervisor_model_id=payload.supervisor_model_id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return MissionBulkModelUpdateResponse(updated_supervisor_agent_id=sup_id)


# ── 激活 ──
@router.post("/{mission_id}/activate", response_model=MissionActivationResponse)
async def activate_mission(
    mission_id: uuid.UUID, _: AdminUser, db: DBSession
) -> MissionActivationResponse:
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    ok, issues = await mission_service.activate_mission(db, project)
    return MissionActivationResponse(ok=ok, status=project.status, issues=issues)


@router.post("/{mission_id}/deactivate", response_model=MissionActivationResponse)
async def deactivate_mission(
    mission_id: uuid.UUID, _: AdminUser, db: DBSession
) -> MissionActivationResponse:
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    await mission_service.deactivate_mission(db, project)
    return MissionActivationResponse(ok=True, status=project.status)


# ── 公开信息（供 /p/[slug] 使用；需要登录，不再匿名） ──
@router.get("/public/{slug}", response_model=MissionDetail)
async def get_project_public(
    slug: str, user: CurrentUser, db: DBSession
) -> MissionDetail:
    """登录用户可访问。仅 active 项目可见；返回 MissionDetail。"""
    project = await mission_service.get_mission_by_slug(db, slug)
    if not project or project.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="项目不存在或未激活")
    if not await mission_service.check_user_can_access_mission(db, project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="无权访问该项目")
    return _to_detail(project)


# ─────────────────────────── M1/M3: Lifecycle ───────────────────────────
@router.post("/{mission_id}/lifecycle/{action}", response_model=MissionRuntimePublic)
async def project_lifecycle(
    mission_id: uuid.UUID,
    action: MissionLifecycleAction,
    _: AdminUser,
    db: DBSession,
) -> MissionRuntimePublic:
    """启停 project daemon。

    action：
    - start / stop / restart：状态机切换（幂等）
    - clear_memory（M3）：清空 mission_agent_memory 全部行；返回当前 runtime
    """
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    try:
        if action == "start":
            # 管理台 start = 恢复调度（不强制立即 tick；要立刻跑用 Run Once）。
            # 首次激活 kick off 仅由 Builder 创建后的 start 路径触发（见 builder_skills）。
            await mission_daemon.start(db, mission_id)
        elif action == "stop":
            await mission_daemon.stop(db, mission_id)
        elif action == "restart":
            await mission_daemon.restart(db, mission_id)
        elif action == "clear_memory":
            await mission_daemon.clear_memory(db, mission_id)
        elif action == "run_once":
            await mission_daemon.run_once(db, mission_id, payload=None)
            # 单发 tick 收尾补消费闭环：tick 末可能有新 pending（如完整性 gate 的缺口
            # 报告入队），没人 drain 会停摆（2026-07-03 实证）。
            await mission_daemon.maybe_autodrain(mission_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    rs = await mission_daemon.get_runtime(db, mission_id)
    return MissionRuntimePublic.model_validate(rs)


@router.get("/{mission_id}/runtime", response_model=MissionRuntimePublic)
async def project_runtime(
    mission_id: uuid.UUID,
    _: AdminUser,
    db: DBSession,
) -> MissionRuntimePublic:
    project = await mission_service.get_mission(db, mission_id)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mission 不存在")
    rs = await mission_daemon.get_runtime(db, mission_id)
    return MissionRuntimePublic.model_validate(rs)


