"""平台级 system_settings 进程内缓存 + 类型化读 helper。

设计：
- 60s 进程缓存（与 compression cache 共用 TTL 也存在 system_settings 里 → cache_ttl_seconds）
- admin PATCH 后 invalidate；compression_service.invalidate_compression_platform_cache 已联动
- 统一 get(key, default, cast) 接口；任意模块都能用：
    val = await system_settings.get_int(db, 'worker.max_clarification_rounds', 3)

为什么单独建文件：
  避免 super_dispatch_skills / escalation_dispatcher / mission_daemon / scheduler 各自
  重复 SELECT system_settings；也避免和 compression_service 的 compression cache 互相绕。
"""
from __future__ import annotations

import logging
import time
from typing import Any, TypeVar

from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL_DEFAULT = 60.0

# 新建 Mission 未填「它要做什么」(goal_hint) 时，主动发给用户的固定问候语。
# 这是 admin 可在「系统设置」编辑的 system_setting (key=mission.empty_goal_prompt)，
# 非 LLM 生成；此常量是 DB 行缺失时的兜底默认值（create_mission 与 startup seed 共用）。
# ADR-030 · shell 安全门提示词（后台「系统设置」可编辑；缺省回落 shell_judge.JUDGE_DEFAULT_SYSTEM_PROMPT）
SHELL_JUDGE_PROMPT_KEY = "shell_judge.system_prompt"

MISSION_EMPTY_GOAL_PROMPT_KEY = "mission.empty_goal_prompt"
MISSION_EMPTY_GOAL_PROMPT_DEFAULT = (
    "你好 👋 这个助手已就绪。告诉我你想让我做什么，我就开始干。\n\n"
    "比如：\n"
    "• 每天早上 9 点把昨天的行业新闻整理成一条摘要发我\n"
    "• 收到合同就帮我审一遍关键风险条款\n"
    "• 给我的小红书账号自动写图文并定时发布\n\n"
    "直接一句话描述需求即可，我会帮你拆解和规划。"
)

T = TypeVar("T")


def invalidate() -> None:
    """admin 改 system_settings 后调；下次 get 重读 DB。"""
    global _CACHE_TS
    _CACHE.clear()
    _CACHE_TS = 0.0


async def _load_all(db: AsyncSession) -> dict[str, Any]:
    global _CACHE_TS
    now = time.time()
    # 用 cache_ttl_seconds 自身（递归只读 cache；若没有就 60s）
    ttl = float(_CACHE.get("compression.cache_ttl_seconds", _CACHE_TTL_DEFAULT)) if _CACHE else _CACHE_TTL_DEFAULT
    if _CACHE and now - _CACHE_TS < ttl:
        return _CACHE
    try:
        rows = (await db.execute(_sql_text("SELECT key, value FROM system_settings"))).all()
        new_cache: dict[str, Any] = {}
        for k, v in rows:
            new_cache[k] = v
        _CACHE.clear()
        _CACHE.update(new_cache)
        _CACHE_TS = now
    except Exception:
        logger.exception("[system_settings] load failed; using stale / empty cache")
    return _CACHE


async def get(db: AsyncSession, key: str, default: Any = None) -> Any:
    cache = await _load_all(db)
    return cache.get(key, default)


async def get_int(db: AsyncSession, key: str, default: int) -> int:
    v = await get(db, key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


async def get_float(db: AsyncSession, key: str, default: float) -> float:
    v = await get(db, key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def get_bool(db: AsyncSession, key: str, default: bool) -> bool:
    v = await get(db, key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return default


async def get_list(db: AsyncSession, key: str, default: list) -> list:
    v = await get(db, key, default)
    return v if isinstance(v, list) else default
