"""Skill / MCP Server 业务服务。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import MCPServer, Skill
from app.schemas.skill import (
    MCPServerCreate,
    MCPServerUpdate,
    MCPToolInfo,
    SkillCreate,
    SkillUpdate,
)

logger = logging.getLogger(__name__)


# ── Skill ──
async def list_skills(db: AsyncSession) -> Sequence[Skill]:
    result = await db.execute(select(Skill).order_by(Skill.is_builtin.desc(), Skill.name))
    return result.scalars().all()


async def get_skill(db: AsyncSession, skill_id: uuid.UUID) -> Skill | None:
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    return result.scalar_one_or_none()


async def get_skill_by_slug(db: AsyncSession, slug: str) -> Skill | None:
    result = await db.execute(select(Skill).where(Skill.slug == slug))
    return result.scalar_one_or_none()


async def create_skill(db: AsyncSession, payload: SkillCreate) -> Skill:
    skill = Skill(
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        version=payload.version,
        skill_type=payload.skill_type,
        content_md=payload.content_md,
        builtin_ref=payload.builtin_ref,
        config_schema=payload.config_schema,
        is_enabled=payload.is_enabled,
        is_builtin=False,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


async def update_skill(db: AsyncSession, skill: Skill, payload: SkillUpdate) -> Skill:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(skill, field, value)
    await db.commit()
    await db.refresh(skill)
    return skill


async def delete_skill(db: AsyncSession, skill: Skill) -> None:
    if skill.is_builtin:
        raise ValueError("内置 Skill 不可删除")
    await db.delete(skill)
    await db.commit()


# ── MCP Server ──
async def list_mcp_servers(db: AsyncSession) -> Sequence[MCPServer]:
    result = await db.execute(select(MCPServer).order_by(MCPServer.created_at))
    return result.scalars().all()


async def get_mcp_server(db: AsyncSession, server_id: uuid.UUID) -> MCPServer | None:
    result = await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    return result.scalar_one_or_none()


async def create_mcp_server(db: AsyncSession, payload: MCPServerCreate) -> MCPServer:
    server = MCPServer(
        name=payload.name,
        description=payload.description,
        server_type=payload.server_type,
        command=payload.command,
        env_vars=payload.env_vars,
        url=payload.url,
        headers=payload.headers,
        is_enabled=payload.is_enabled,
        # 修 mission-7a9afb：原漏传 → startup_command 恒 None → 本地 MCP 无法 auto-launch + readiness 假 ready
        startup_command=payload.startup_command,
        startup_cwd=payload.startup_cwd,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return server


async def update_mcp_server(
    db: AsyncSession, server: MCPServer, payload: MCPServerUpdate
) -> MCPServer:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(server, field, value)
    await db.commit()
    await db.refresh(server)
    return server


async def delete_mcp_server(db: AsyncSession, server: MCPServer) -> None:
    await db.delete(server)
    await db.commit()


async def test_mcp_server(server: MCPServer) -> tuple[bool, str | None, list[MCPToolInfo]]:
    """尝试连接 MCP Server 并返回工具清单。

    Phase 3：仅对 stdio / http 做基础参数校验，不真正启动进程（避免测试环境依赖）。
    Phase 8 集成验收时再接通 `langchain_mcp_adapters.client.MultiServerMCPClient`。
    """
    if server.server_type == "stdio":
        if not server.command:
            return False, "stdio MCP 缺少 command", []
        return (
            True,
            None,
            [
                MCPToolInfo(
                    name="(connection not verified)",
                    description="stdio 模式校验通过；实时工具清单需启动子进程才能列出",
                )
            ],
        )
    if server.server_type == "http":
        if not server.url:
            return False, "http MCP 缺少 url", []
        return (
            True,
            None,
            [
                MCPToolInfo(
                    name="(connection not verified)",
                    description=f"http endpoint 已配置：{server.url}",
                )
            ],
        )
    return False, f"未知的 server_type={server.server_type}", []
