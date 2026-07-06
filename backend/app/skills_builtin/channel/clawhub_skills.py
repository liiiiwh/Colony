"""M6: Builder / Installer Agent 用的 ClawHub 工具集 + remote_skill_invoke stub。"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from langchain_core.tools import StructuredTool

from app.skills_builtin.context import BuiltinToolContext


def _fmt_exc(exc: BaseException) -> str:
    """绝不返回空 error：httpx 传输层异常 str() 常为空，至少带上类型名（如 ConnectError）。"""
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


async def _resolve_target_project_id(
    ctx: BuiltinToolContext, target_project_id: str
) -> tuple[uuid.UUID | None, dict | None]:
    """target_project_id → (mission UUID, None) 或 (None, 结构化错误)。

    2026-07-03 e2e 实证：installer 的 LLM 传了 mission slug，`uuid.UUID(...)` 裸抛
    ValueError 崩掉整个 invoke_worker 图。工具**永不裸抛**——坏参数回结构化错误让
    LLM 自纠；slug 是 LLM 天然爱传的形态，顺手按 missions.slug 解析。"""
    if not target_project_id:
        return None, None
    try:
        return uuid.UUID(target_project_id), None
    except ValueError:
        pass
    if ctx.db_factory is not None:
        from sqlalchemy import select as _select

        from app.models.mission import Mission as _Mission

        async with ctx.db_factory() as db:
            row = (await db.execute(
                _select(_Mission.id).where(_Mission.slug == target_project_id)
            )).scalar_one_or_none()
        if row is not None:
            return row, None
    return None, {
        "ok": False,
        "error": f"target_project_id 既不是 UUID 也不是已知 mission slug: {target_project_id!r}",
        "instruction": "传 mission 的 UUID 或有效 slug；不确定就留空（不绑定到具体 mission）。",
    }


# SKILL.md 中描述外部 setup / prerequisites / login / server 启动等步骤的 section 头
# 容忍数字编号前缀（如 `## 1. Local Server Setup`）
_SETUP_HEADING_PAT = re.compile(
    r"^\s*#{1,4}\s+"           # heading 标记
    r"(?:\d+\.?\s+)?"          # 可选数字编号 `1.` / `1 `
    r"(?:"
    r"local\s+server\s+setup|"
    r"server\s+setup|"
    r"prerequisites|"
    r"setup|"
    r"installation|"
    r"local\s+setup|"
    r"getting\s+started|"
    r"environment\s+setup|"
    r"requirements|"
    r"login|"
    r"配置环境|"
    r"前置条件|"
    r"安装步骤|"
    r"启动方式|"
    r"准备工作|"
    r"使用前"
    r")\b",
    re.IGNORECASE,
)


def _extract_setup_instructions(install_dir: str) -> str:
    """从 install_dir/SKILL.md 提取「外部 setup 说明」section。

    扫所有 .md 文件，找匹配 _SETUP_HEADING_PAT 的 section，截取到下一个 ≤同级 heading
    或文末。返回拼接后的内容（最多 4000 字符）。

    没找到时返回 ""。Supervisor 据此决定是否弹 approval 让用户做手动配置。
    """
    if not install_dir:
        return ""
    p = Path(install_dir)
    if not p.exists() or not p.is_dir():
        return ""
    sections: list[str] = []
    for md_file in list(p.glob("*.md")) + list(p.glob("**/SKILL.md")):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            m = _SETUP_HEADING_PAT.match(lines[i])
            if m:
                # 找出当前 heading level（# 数量）
                head = re.match(r"^\s*(#+)\s", lines[i])
                cur_level = len(head.group(1)) if head else 2
                # 截取直到下一个 ≤ cur_level 的 heading
                buf = [lines[i]]
                j = i + 1
                while j < len(lines):
                    next_head = re.match(r"^\s*(#+)\s", lines[j])
                    if next_head and len(next_head.group(1)) <= cur_level:
                        break
                    buf.append(lines[j])
                    j += 1
                sections.append("\n".join(buf).strip())
                i = j
            else:
                i += 1
    if not sections:
        return ""
    out = "\n\n---\n\n".join(sections)
    # 截断到 4000 字符（含尾部省略）
    if len(out) > 4000:
        out = out[:3900] + "\n\n…(已截断，完整内容见 SKILL.md)"
    return out

logger = logging.getLogger(__name__)


# ─────────────────────────── clawhub_search ───────────────────────────
# ClawHub 搜索是**全 token AND 匹配**，且只索引领域词；下面这些"形态/通用"词参与匹配只会把结果打成 0
# （实测："zhihu MCP"→0、"zhihu publish post comment"→0，而 "zhihu"→10、"zhihu publish"→6）。
# 自动降级时先剔除它们，避免 LLM 堆一串关键词导致搜不到既有 skill 而误判"平台没有"。
_CLAWHUB_NOISE_TOKENS = frozenset({
    "mcp", "api", "apis", "http", "https", "sdk", "plugin", "plugins", "skill", "skills",
    "server", "service", "tool", "tools", "bot", "agent", "integration", "relay", "call",
    "publish", "post", "posts", "comment", "comments", "reply", "fetch", "crawl", "scrape",
    "browser", "automation", "automate", "social", "media", "content", "data", "manage",
})


def _clawhub_query_fallbacks(query: str) -> list[str]:
    """生成由窄到宽的候选查询：原词 → 剔除形态噪声词 → 仅首个领域词。去重保序。"""
    toks = [t for t in (query or "").split() if t.strip()]
    candidates = [query]
    domain = [t for t in toks if t.lower() not in _CLAWHUB_NOISE_TOKENS]
    if domain and domain != toks:
        candidates.append(" ".join(domain))
    if len(domain) > 1:
        candidates.append(domain[0])  # 最宽：单个领域词
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def clawhub_search_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(query: str, limit: int = 10) -> dict:
        from app.services import clawhub_client

        # A2：limit 封顶，防 LLM 传 100 万对 ClawHub DoS
        try:
            limit_int = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit_int = 10
        # ClawHub = 全 token AND 匹配。LLM 常堆 "zhihu MCP publish post comment" 之类导致 0 结果，
        # 进而误判"平台没有该能力"去骚扰人类。这里自动降级：原查询 0 命中就剔噪声词/收窄到领域词重试。
        last_err: str | None = None
        fallbacks = _clawhub_query_fallbacks(query)
        for i, q in enumerate(fallbacks):
            try:
                data = await clawhub_client.search_skills(q, limit=limit_int)
            except Exception as exc:  # noqa: BLE001
                last_err = _fmt_exc(exc)
                continue
            # 提取结果列表：注意「key 存在但空列表」必须判为"无结果"（不能 `or` 链落到整个 dict）
            if isinstance(data, dict):
                results = data["results"] if "results" in data else data.get("items", data)
            else:
                results = data
            if isinstance(results, dict) and "results" in results:  # 解嵌套 {"results": [...]}
                results = results["results"]
            has = bool(results)  # 空 list / 空 dict 都判为无结果 → 继续放宽
            if has or i == len(fallbacks) - 1:
                out = {"ok": True, "query": q, "limit_applied": limit_int, "results": results}
                if q != query:
                    out["auto_broadened_from"] = query
                    out["note"] = (
                        f"原查询 {query!r} 0 命中，已自动放宽到 {q!r}（ClawHub 是全词 AND 匹配，"
                        "别堆 MCP/publish/post 等形态词，用 1-2 个领域关键词如 'zhihu' / '知乎'）。"
                    )
                return out
        return {"ok": False, "error": last_err or "search failed"}

    return StructuredTool.from_function(
        coroutine=_run,
        name="clawhub_search",
        description=(
            "（Builder/Installer）在 ClawHub 搜索 skill。返回 results 数组（slug / displayName / summary 等）。"
            "**查询用 1-2 个领域关键词**（如 'zhihu' / '知乎 发帖'）——ClawHub 是全词 AND 匹配，"
            "堆 'MCP'/'publish'/'post'/'api' 等形态词会命中 0。0 命中时本工具会自动放宽重试并在 note 里说明。"
        ),
    )


# ─────────────────────────── clawhub_inspect ───────────────────────────
def clawhub_inspect_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(slug: str, version: str = "") -> dict:
        from app.services import remote_skill_installer

        try:
            info = await remote_skill_installer.inspect(slug, version or None)
            return {"ok": True, **info}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _fmt_exc(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="clawhub_inspect",
        description=(
            "（Builder/Installer）查看 ClawHub skill 的详情 + 安全摘要。"
            "返回 blocked / high_risk_tags / security / skill 等字段。"
        ),
    )


# ─────────────────────────── clawhub_install ───────────────────────────
def clawhub_install_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(
        slug: str,
        version: str = "",
        target_project_id: str = "",
        force_high_risk: bool = False,
    ) -> dict:
        from app.services import remote_skill_installer

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        # A1：force_high_risk 仅 InstallerAgent 可用，防 Supervisor 被 prompt 注入绕开高危检查
        if force_high_risk and ctx.agent_node_name != "installer":
            return {
                "ok": False,
                "error": "FORCE_HIGH_RISK_NOT_ALLOWED",
                "instruction": (
                    f"agent={ctx.agent_node_name} 不允许传 force_high_risk=True；"
                    "仅 InstallerAgent 可在用户 request_approval 后绕开高危检查。"
                ),
            }
        pid, _pid_err = await _resolve_target_project_id(ctx, target_project_id)
        if _pid_err is not None:
            return _pid_err
        async with ctx.db_factory() as db:
            try:
                rec = await remote_skill_installer.install(
                    db,
                    slug=slug,
                    version=version or None,
                    mission_id=pid,
                    force_high_risk=force_high_risk,
                )
                # 装完后提取 SKILL.md 中的「外部 setup 说明」，让 Supervisor 主动暴露给用户
                # 否则 runtime_kind=static-instruction 类 skill（如 xiaohongshu-mcp）
                # 用户根本不知道还需要下载 binary / 扫 QR / 起 MCP server 等步骤
                setup_instructions = _extract_setup_instructions(rec.install_dir)
                needs_external_setup = bool(setup_instructions)
                return {
                    "ok": True,
                    "install_id": str(rec.id),
                    "local_skill_id": str(rec.local_skill_id) if rec.local_skill_id else None,
                    "runtime_kind": rec.runtime_kind,
                    "install_dir": rec.install_dir,
                    "entrypoint": rec.entrypoint,
                    "capability_tags": rec.capability_tags,
                    "needs_external_setup": needs_external_setup,
                    "setup_instructions": setup_instructions,
                    "user_action_required": (
                        # 给 Supervisor 的明示信号
                        f"⚠️ 该 skill 是 {rec.runtime_kind} 类型，可能需要用户在本机完成额外配置。"
                        "请立即调 `request_approval(title='需要您手动完成外部配置', "
                        f"message=setup_instructions, options=['已完成配置，继续', "
                        f"'我先去配置稍后回来', '换个不需要外部依赖的方案'])` 让用户知情。"
                    ) if needs_external_setup else None,
                }
            except remote_skill_installer.ClawhubInstallNeedsApproval as exc:
                return {"ok": False, "needs_approval": True, "error": str(exc)}
            except remote_skill_installer.ClawhubInstallBlocked as exc:
                return {"ok": False, "blocked": True, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="clawhub_install",
        description=(
            "（Installer）从 ClawHub 安装 skill。target_project_id 留空 = 全局安装。"
            "若 needs_approval=True 则需先 request_approval 拿到批准，再带 force_high_risk=True 重试。"
        ),
    )


# ─────────────────────────── clawhub_uninstall ───────────────────────────
def clawhub_uninstall_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(install_id: str) -> dict:
        from app.services import remote_skill_installer

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        async with ctx.db_factory() as db:
            try:
                ok = await remote_skill_installer.uninstall(db, uuid.UUID(install_id))
                return {"ok": ok}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}

    return StructuredTool.from_function(
        coroutine=_run,
        name="clawhub_uninstall",
        description="（Installer）按 install_id 卸载 ClawHub skill。",
    )


# ─────────────────────────── clawhub_list_installed ───────────────────────────
def clawhub_list_installed_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(target_project_id: str = "") -> dict:
        from app.services import mission_service, remote_skill_installer

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        pid, _pid_err = await _resolve_target_project_id(ctx, target_project_id)
        if _pid_err is not None:
            return _pid_err
        # A3：跨 project 边界保护
        # - 不传 target_project_id：返回全局（slug='builder' 项目允许；其他 project 只能列自己的）
        # - 传了 target_project_id：必须等于 ctx.mission_id，除非当前在 builder project
        if pid is not None and ctx.mission_id is not None and pid != ctx.mission_id:
            async with ctx.db_factory() as db:
                cur_proj = await mission_service.get_mission(db, ctx.mission_id)
                if cur_proj is None or cur_proj.slug != "builder":
                    return {
                        "ok": False,
                        "error": "PROJECT_BOUNDARY_VIOLATION",
                        "instruction": (
                            f"agent 所属 project={ctx.mission_id} 不能列其他 project={pid} 的 installed skill。"
                            "仅 Builder Mission 内的 agent 可跨 project 查询。"
                        ),
                    }
        async with ctx.db_factory() as db:
            rows = await remote_skill_installer.list_installed(db, mission_id=pid)
            return {
                "ok": True,
                "items": [
                    {
                        "install_id": str(r.id),
                        "slug": r.clawhub_slug,
                        "version": r.clawhub_version,
                        "runtime_kind": r.runtime_kind,
                        "install_dir": r.install_dir,
                        "capability_tags": r.capability_tags,
                        "local_skill_id": str(r.local_skill_id) if r.local_skill_id else None,
                    }
                    for r in rows
                ],
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="clawhub_list_installed",
        description="（Builder/Installer）列出已安装的 ClawHub skill。target_project_id 留空 = 全局。",
    )


# ─────────────────────────── remote_skill_invoke (stub) ───────────────────────────
def remote_skill_invoke_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """M6 stub：让 Agent 至少能 bind 远程 skill 镜像。

    当被调用时根据 RemoteSkillInstall.runtime_kind 决定行为：
    - python: 尝试 subprocess 跑 entrypoint（M7 起接入；M6 直接报"未启用"）
    - 其他 kind: 全部"未启用"
    """

    async def _run(remote_install_id: str, payload: dict | None = None) -> dict:
        from app.models.skill import RemoteSkillInstall

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        try:
            _rid = uuid.UUID(str(remote_install_id))
        except (ValueError, TypeError):
            return {"ok": False, "error": f"remote_install_id 不是合法 UUID: {remote_install_id!r}"}
        async with ctx.db_factory() as db:
            rec = await db.get(RemoteSkillInstall, _rid)
            if rec is None:
                return {"ok": False, "error": "RemoteSkillInstall 不存在"}
            return {
                "ok": False,
                "stub": True,
                "message": (
                    f"colony M6 仅安装 + 编排 ClawHub skill；"
                    f"runtime_kind={rec.runtime_kind} 的实际执行将在 M7+ 接入。"
                ),
                "install_dir": rec.install_dir,
                "entrypoint": rec.entrypoint,
                "payload_received": payload or {},
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="remote_skill_invoke",
        description=(
            "（系统）调用一个已安装的 ClawHub skill。Agent 不会主动调它；"
            "这是远程 skill 镜像的工具入口，M6 阶段是 stub。"
        ),
    )
