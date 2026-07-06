"""Builder-only governance skills：work-claim 互斥锁 + 模板化 skill 创建。

（2026-07 绿地清理：build_super/build_worker 已删——M2 spec 工厂 skill 层无人绑定，
实走路径是 v3 手搓 agent_create + ADR-031 installer 委派。）
"""
from __future__ import annotations

import json
import logging
import uuid

from langchain_core.tools import StructuredTool

from app.skills_builtin.context import BuiltinToolContext

logger = logging.getLogger(__name__)


async def _acquire_or_reject(ctx: BuiltinToolContext, target_type: str, target_id: str) -> str | None:
    """ADR-009 G4 · 改 target 前抢锁。被其它 session 持有 → 返回 reject JSON（调用方应直接返回它）；
    可获取/复用 → 返回 None（继续）。ctx 无 session_id 时跳过（不阻塞非 session 场景）。"""
    if ctx.mission_id is None or ctx.db_factory is None:
        return None
    from app.services import builder_claim_service
    async with ctx.db_factory() as db:
        res = await builder_claim_service.acquire_claim(
            db, target_type=target_type, target_id=target_id,
            session_id=ctx.mission_id, mission_id=ctx.mission_id,
        )
    if res.get("outcome") == "reject":
        return json.dumps({"ok": False, "error": "claim_conflict", "message": res["message"]}, ensure_ascii=False)
    return None


async def _record_work(
    ctx: BuiltinToolContext, *, action: str, target_type: str, target_id: str,
    result: str = "ok", summary: str = "", affected_supers: list | None = None,
) -> None:
    """ADR-009 G5 · 写一行 Builder 工作记录（per session 审计）。不阻塞主流程。"""
    if ctx.mission_id is None or ctx.db_factory is None:
        return
    try:
        from app.models.builder_governance import BuilderWorkLog
        async with ctx.db_factory() as db:
            db.add(BuilderWorkLog(
                session_id=ctx.mission_id, mission_id=ctx.mission_id,
                action=action, target_type=target_type, target_id=target_id,
                affected_supers=affected_supers or [], result=result, summary=summary[:2000],
            ))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[builder_work_log] 写工作记录失败（不阻塞）")


def create_skill_from_template_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _create(template: str, slug: str, name: str, config: dict) -> str:
        """ADR-009 G6 ·（Builder-only）从白名单模板创建一个新 skill（不跑任意代码）。

        模板：http_api_call(config: method,url_template,headers?) /
              mcp_proxy(config: mcp_server_id,tool_name) /
              prompt_macro(config: prompt_template,role?)。
        创建后该 slug 即存在，可被 skill_bind 绑给 agent。
        """
        from sqlalchemy import select as _select
        from app.domain.builder.skill_template import render_skill_row, validate_template_request
        from app.models.skill import Skill

        if ctx.db_factory is None:
            return json.dumps({"ok": False, "error": "缺 db_factory"})
        err = validate_template_request(template=template, slug=slug, config=config or {})
        if err:
            return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        row = render_skill_row(template=template, slug=slug, name=name, config=config or {})
        async with ctx.db_factory() as db:
            exists = (await db.execute(_select(Skill).where(Skill.slug == slug))).scalar_one_or_none()
            if exists is not None:
                return json.dumps({"ok": True, "slug": slug, "already_exists": True}, ensure_ascii=False)
            db.add(Skill(
                slug=row["slug"], name=row["name"], description=f"模板生成({template})",
                skill_type=row["skill_type"], builtin_ref=row["builtin_ref"],
                config_schema=row["config"], is_enabled=True, is_builtin=False,
                scope="all", intent="io",
            ))
            await db.commit()
        await _record_work(ctx, action="create_skill", target_type="skill", target_id=slug,
                           result="ok", summary=f"模板 {template} 生成 skill {slug}")
        return json.dumps({"ok": True, "slug": slug, "template": template}, ensure_ascii=False)

    return StructuredTool.from_function(
        coroutine=_create,
        name="create_skill_from_template",
        description=(
            "（Builder-only ADR-009）从白名单模板创建新 skill（不跑任意代码）。"
            "template ∈ {http_api_call, mcp_proxy, prompt_macro}；slug/name/config。"
            "用于 build_* 报 missing_skills 时补齐缺失 skill。"
        ),
    )


def release_work_claim_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _release(target_type: str, target_id: str) -> str:
        """ADR-009 G4 ·（Builder-only）处理完某 worker/super/skill 后释放本 session 的处理锁，
        让其它 session 可以接手。target_type ∈ {worker, super, skill}；target_id = capability / slug。"""
        if ctx.mission_id is None or ctx.db_factory is None:
            return json.dumps({"ok": False, "error": "缺 session_id / db_factory"})
        from app.services import builder_claim_service
        async with ctx.db_factory() as db:
            res = await builder_claim_service.release_claim(
                db, target_type=target_type, target_id=target_id, session_id=ctx.mission_id,
            )
        return json.dumps(res, ensure_ascii=False)

    return StructuredTool.from_function(
        coroutine=_release,
        name="release_work_claim",
        description=(
            "（Builder-only）处理完某 worker/super/skill 后释放处理锁。"
            "target_type ∈ {worker, super, skill}, target_id = capability slug / project slug。"
        ),
    )
