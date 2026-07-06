"""ADR-013 · Builder 构建确定性收尾（不依赖 LLM 记得调）。

病根：工厂靠超长 LLM 协议让模型记得 mcp_ensure_ready + activate_super_first_run，模型一停就
留个半成品壳。解法：Builder tick 结束后，**代码**自动收尾它本会话建的 super 项目。

信号：mission_create 会把 Builder 会话的 target_project_id 指向新建项目 → tick 后据此 finalize。
幂等：已 finalize（存在 super_activated 消息）则跳过。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def maybe_finalize_after_builder_tick(
    db: AsyncSession, builder_project_id
) -> dict | None:
    """仅当 builder_project_id 是 Builder 项目时：finalize 它本 mission 建出的 super 项目。

    ADR-018 mission-only：通知写到 builder mission 的主 thread (builder_project_id, 'main')。"""
    from app.models.agent import Agent
    from app.models.mission import Mission

    proj = await db.get(Mission, builder_project_id)
    if proj is None:
        return None
    # 任何「Builder super 监管的 mission」都要收尾——不只主 builder mission(slug='builder')，
    # 也包括用户 +新建 的每场景设计会话（slug != 'builder'）。否则 +新建 建出的 super 永远
    # 没 schedule / 不自启。判定：supervisor 是 category='builder' 的 super。
    sup = await db.get(Agent, proj.supervisor_agent_id) if proj.supervisor_agent_id else None
    if sup is None or sup.category != "builder":
        return None
    # provenance 找本 builder mission 建出的 super 项目（取代 session.target_project_id）
    from app.services import mission_service
    built = await mission_service.get_mission_built_by_mission(db, builder_project_id)
    if built is None:
        return None
    return await finalize_super_build(db, built.id, builder_project_id, "main")


async def _bound_or_managed_mcp_ids(db: AsyncSession, mission_id) -> list:
    """该项目相关的本地 http MCP：先取绑到本 mission super 的（ADR-027 D5：worker MCP 由
    capability dispatch 动态解析，不再 mission_nodes 预绑）；没有则回退到系统里唯一一个
    「有 startup_command/manifest 的本地 http MCP」（单 colony 启发式）。"""
    pid = str(mission_id)
    bound = (await db.execute(text(
        "SELECT DISTINCT m.id FROM mcp_servers m "
        "JOIN agent_mcp_servers ams ON ams.mcp_server_id=m.id "
        "WHERE m.server_type='http' AND m.is_enabled IS TRUE AND "
        "  ams.agent_id IN (SELECT supervisor_agent_id FROM missions WHERE id=:p)"
    ), {"p": pid})).scalars().all()
    if bound:
        return list(bound)
    # 回退：系统中受管的本地 http MCP（有 startup_command 或 manifest）
    managed = (await db.execute(text(
        "SELECT id FROM mcp_servers WHERE server_type='http' AND is_enabled IS TRUE "
        "AND (startup_command IS NOT NULL OR readiness_manifest IS NOT NULL)"
    ))).scalars().all()
    return list(managed)


async def check_build_completeness(db: AsyncSession, super_agent) -> dict:
    """校验 super 的构建完整性：roster ⟺ worker 双向一致（2026-07-03 install-first gate）。

    可靠的结构不变量（不依赖 LLM 自觉）：
    - missing_workers：super.required_capabilities 里声明了、但平台没有对应 enabled worker 的 cap。
    - orphan_workers：建了非系统 worker、但**没有任何 super** 的 required_capabilities 声明它 →
      super 不会 invoke_worker 它 → 残废（真出的 bug：6 个 worker 全孤儿、roster 空）。
    complete = 两者皆空。
    """
    from app.models.agent import Agent

    roster = list(((super_agent.extra_config or {}).get("required_capabilities") or []))
    workers = (await db.execute(
        select(Agent).where(
            Agent.kind == "worker", Agent.is_system.is_(False), Agent.capability.is_not(None)
        )
    )).scalars().all()
    worker_caps = {w.capability for w in workers}

    # 所有 super 声明的 capability 全集（判孤儿：worker 没被任何 super 声明）
    supers = (await db.execute(select(Agent).where(Agent.kind == "super"))).scalars().all()
    all_declared: set = set()
    for s in supers:
        all_declared.update((s.extra_config or {}).get("required_capabilities") or [])

    missing_workers = sorted(c for c in roster if c and c not in worker_caps)
    orphan_workers = sorted(c for c in worker_caps if c not in all_declared)
    # 空构建洞（2026-07-06 e2e 实证）：业务 super 花名册为空 → 什么都 invoke_worker 不了 →
    # 残废。missing/orphan 都空时旧逻辑"真空完整"会放行（installer 超时→Builder 回退→建了
    # super 装了 MCP 但 0 worker、roster 空的真实形态）。系统 super（Builder/Worker-Opt）本就
    # 无 roster，不受此规则约束。
    empty_roster = (not roster) and (not bool(getattr(super_agent, "is_system", False)))

    # 未绑 MCP 洞（2026-07-06 e2e 实证残留）：installer 装好+注册了受管本地 MCP，但 Builder
    # 建完 worker 忘了 agent_mcp_bind（LLM 超时/漏步）→ worker 用不上 MCP 方法（用户原始投诉）。
    # 结构化强制"必绑"：受管本地 MCP（有 startup_command、启用）却没绑给**任何业务 worker**
    # → 基础设施白装、super 残废 → 不完整，退回 Builder 绑。不 auto-bind，只把"必绑"变 gate 兜底。
    unbound_mcp = await _unbound_managed_mcps(db)

    complete = (
        not missing_workers and not orphan_workers
        and not empty_roster and not unbound_mcp
    )
    return {
        "complete": complete,
        "missing_workers": missing_workers,
        "orphan_workers": orphan_workers,
        "empty_roster": empty_roster,
        "unbound_mcp": unbound_mcp,
    }


async def _unbound_managed_mcps(db: AsyncSession) -> list[str]:
    """受管本地 MCP（server_type='http'、启用、有 startup_command）但没绑给任何**非系统**
    worker 的名字列表。用于 gate 强制 install-first 后的"必绑"（2026-07-06）。"""
    rows = await db.execute(text(
        "SELECT m.name FROM mcp_servers m "
        "WHERE m.server_type='http' AND m.is_enabled IS TRUE AND m.startup_command IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM agent_mcp_servers ams JOIN agents a ON a.id = ams.agent_id "
        "  WHERE ams.mcp_server_id = m.id AND a.is_system IS NOT TRUE AND a.kind='worker'"
        ")"
    ))
    return sorted(r[0] for r in rows.all())


async def auto_adopt_orphan_workers(
    db: AsyncSession, super_agent, builder_mission_id
) -> list[str]:
    """确定性自修复（2026-07-05）：把「本 builder mission 建的孤儿 worker」自动并入
    super.required_capabilities，返回收编的 capability 列表。

    背景：花名册声明曾靠 Builder LLM 自觉调 agent_update——e2e 实证它三连无视精确
    指令（gate 拦了 3 次都回「无需动作」）。孤儿收编是纯确定性问题：worker 存在、
    有 capability、provenance（built_by_mission_id）指向本次构建、没被任何 super
    声明 → 它就该进本 super 的花名册。missing_workers（声明了没建）仍是硬缺口，
    只能真建，不在本函数职责内。"""
    from app.models.agent import Agent

    if builder_mission_id is None:
        return []
    workers = (await db.execute(
        select(Agent).where(
            Agent.kind == "worker", Agent.is_system.is_(False),
            Agent.capability.is_not(None),
            Agent.built_by_mission_id == builder_mission_id,
        )
    )).scalars().all()
    if not workers:
        return []
    supers = (await db.execute(select(Agent).where(Agent.kind == "super"))).scalars().all()
    declared: set = set()
    for s in supers:
        declared.update((s.extra_config or {}).get("required_capabilities") or [])
    adopted = sorted({w.capability for w in workers} - declared)
    if not adopted:
        return []
    cfg = dict(super_agent.extra_config or {})
    roster = list(cfg.get("required_capabilities") or [])
    cfg["required_capabilities"] = roster + [c for c in adopted if c not in roster]
    super_agent.extra_config = cfg
    await db.commit()
    logger.info(
        "[finalize] 自修复：收编本次构建的孤儿 worker 进花名册 super=%s adopted=%s",
        super_agent.slug or super_agent.name, adopted,
    )
    return adopted


async def _count_unconsumed_incomplete(db: AsyncSession, mission_id) -> int:
    """builder pending 队列里未消费的 build_incomplete 条数（gate 入队去重用）。"""
    row = await db.execute(text(
        "SELECT COUNT(*) FROM super_pending_messages "
        "WHERE super_mission_id = :p AND status = 'pending' "
        "AND meta::text LIKE '%build_incomplete%'"
    ), {"p": str(mission_id)})
    return int(row.scalar() or 0)


# gate 入队上限：enqueue→drain→tick→gate 再评的链条，若 LLM 永远修不好会无限烧 token。
_INCOMPLETE_ENQUEUE_CAP = 3


async def _count_incomplete_reports(db: AsyncSession, project_slug: str) -> int:
    """该 project 历史上已报过几次 build_incomplete（messages 表，跨 tick 累计）。"""
    row = await db.execute(text(
        "SELECT COUNT(*) FROM messages WHERE meta->>'type'='build_incomplete' "
        "AND meta->>'project_slug'=:s"
    ), {"s": project_slug})
    return int(row.scalar() or 0)


async def finalize_super_build(
    db: AsyncSession, mission_id, notify_mission_id, notify_thread_key
) -> dict:
    """确定性收尾：ensure_ready 相关本地 MCP（卡落本项目）+ 激活 super 首跑 + Builder 会话给进入按钮。

    幂等：已存在本项目的 super_activated 消息则跳过。
    """
    from app.models.mission import Mission
    from app.services import mission_daemon, readiness as rd, messaging_service

    proj = await db.get(Mission, mission_id)
    if proj is None or proj.supervisor_agent_id is None:
        return {"skipped": "no_supervisor"}

    pid = mission_id if isinstance(mission_id, uuid.UUID) else uuid.UUID(str(mission_id))
    actions: list[str] = []

    # 0. origin_session_id 确定性写入 —— **在 already-finalized 短路之前**做，
    # 这样即便 Builder LLM 自己调了 activate_super_first_run（已写 super_activated），
    # 也能确保 origin 被写上（否则 L3 escalation 只能靠 dispatcher 回退猜）。
    # ADR-018 mission-only：origin = 产出该 super 的 Builder mission（notify_mission_id）。
    # 历史 bug：此处曾引用已删的 notify_session_id（sessions 退役遗留），NameError 让收尾整段崩，
    # super 永远拿不到默认 schedule + 首跑激活。
    if notify_mission_id:
        try:
            wf = dict(proj.workflow_config or {})
            if not wf.get("origin_session_id"):
                wf["origin_session_id"] = str(notify_mission_id)
                proj.workflow_config = wf
                await db.commit()
                actions.append("origin_session_id")
        except Exception:  # noqa: BLE001
            logger.exception("[finalize] 写 origin_session_id 失败（不阻塞）project=%s", mission_id)

    already = (await db.execute(text(
        "SELECT 1 FROM messages WHERE meta->>'type'='super_activated' "
        "AND meta->>'project_slug'=:s LIMIT 1"
    ), {"s": proj.slug})).first()
    if already:
        return {"skipped": "already_finalized", "project_slug": proj.slug,
                "actions": actions}

    # 0.5 完整性 gate（2026-07-03 install-first）：roster ⟺ worker 不一致 → 不激活残废 super，
    # 回 builder 会话报缺口，等 Builder 下一轮补齐。把"齐不齐"从 LLM 自觉变 FSM 兜底。
    from app.models.agent import Agent as _Agent
    _sup = await db.get(_Agent, proj.supervisor_agent_id)
    if _sup is not None:
        # 0.4 确定性自修复：先把本 builder mission 建的孤儿 worker 收编进花名册
        # （LLM 屡次无视声明指令，孤儿收编是纯确定性问题，代劳）。
        try:
            await auto_adopt_orphan_workers(db, _sup, notify_mission_id)
        except Exception:  # noqa: BLE001
            logger.exception("[finalize] 孤儿 worker 自修复失败（不阻塞，走 gate 报缺口）")
        comp = await check_build_completeness(db, _sup)
        if not comp["complete"]:
            gaps = []
            if comp.get("empty_roster"):
                gaps.append(
                    "super 花名册（required_capabilities）为空且没有任何业务 worker——"
                    "还没真正开始建 worker。请按方案为每个能力 agent_create(kind='worker') "
                    "建 worker（依赖的 MCP/skill 先装好绑好），再 agent_update 声明完整花名册。"
                )
            if comp["missing_workers"]:
                gaps.append(f"声明了但没建 worker 的能力：{', '.join(comp['missing_workers'])}")
            if comp["orphan_workers"]:
                gaps.append(
                    f"建了 worker 但未纳入 super 花名册（required_capabilities）："
                    f"{', '.join(comp['orphan_workers'])}"
                )
            if comp.get("unbound_mcp"):
                gaps.append(
                    f"已安装注册的 MCP 还没绑给任何 worker（worker 会用不上它的方法）："
                    f"{', '.join(comp['unbound_mcp'])}——请对依赖它的 worker 调 "
                    f"agent_mcp_bind(agent_id=<worker>, mcp_server_id=<该 MCP 的 id>)。"
                )
            msg = (
                f"⚠️ 「{proj.name or proj.slug}」构建未完成，暂不激活。缺口：\n- "
                + "\n- ".join(gaps)
                + "\n请补齐：为每个能力建 worker + 用 agent_update("
                "extra_config={'required_capabilities':[...]}) 声明完整花名册（含依赖的 MCP/skill 装好绑好），再收尾。"
            )
            if notify_mission_id:
                try:
                    await messaging_service.append_message(
                        db, notify_mission_id, notify_thread_key or "main", role="agent_log",
                        content=msg,
                        meta={"type": "build_incomplete", "project_slug": proj.slug,
                              "missing_workers": comp["missing_workers"],
                              "orphan_workers": comp["orphan_workers"]},
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[finalize] 写 build_incomplete 消息失败")
                # 2026-07-03 e2e 实证停摆点：只写 agent_log 的话，builder tick 结束转
                # paused_idle 后**没有任何触发器**让它补 roster → 永远停摆。把缺口报告
                # enqueue 进 builder pending 队列：tick 边界 auto-drain（paused_idle 可
                # 消费）/ 重启 reconcile 都会据此接力开 tick。防失控：已有未消费的
                # build_incomplete pending 时不重复入队（每轮 tick 末 gate 都会重评）。
                try:
                    if (
                        await _count_unconsumed_incomplete(db, notify_mission_id) == 0
                        and await _count_incomplete_reports(db, proj.slug) < _INCOMPLETE_ENQUEUE_CAP
                    ):
                        from app.models.mission import Mission as _Mission
                        from app.services import pending_queue as _pq
                        nproj = await db.get(_Mission, notify_mission_id)
                        if nproj is not None and nproj.supervisor_agent_id is not None:
                            await _pq.enqueue_user_message(
                                db, notify_mission_id, nproj.supervisor_agent_id, msg,
                                meta={"type": "build_incomplete", "project_slug": proj.slug},
                            )
                except Exception:  # noqa: BLE001
                    logger.exception("[finalize] enqueue build_incomplete 失败（不阻塞）")
            logger.warning("[finalize] 构建不完整，不激活 project=%s 缺口=%s", proj.slug, comp)
            return {"skipped": "incomplete", "project_slug": proj.slug, **comp}

    # 1. ensure_ready 相关本地 MCP（QR/密钥卡落到本 super 项目会话）
    try:
        for mid in await _bound_or_managed_mcp_ids(db, mission_id):
            await rd.ensure_ready_for_server(db, mid, mission_id=mission_id)
            actions.append(f"ensure_ready:{str(mid)[:8]}")
    except Exception:  # noqa: BLE001
        logger.exception("[finalize] ensure_ready 失败（不阻塞）project=%s", mission_id)

    # 1.6 调度由 Builder 结合场景决定，**不再强制补默认 cron**：
    #   - 周期性场景（如 SRE 巡检、日报）→ Builder 在 BUILD 阶段显式建 cron/interval schedule；
    #   - 事件/按需场景（如法律合同审查：上传合同才触发）→ 无 schedule，靠事件/用户消息驱动。
    # 旧逻辑无条件补 `*/3 * * * *` 默认 tick，会让事件驱动 super 每 3 分钟空跑烧 LLM（无意义）。
    # 首跑由下面 kickoff 保证；之后是否持续自动跑取决于 Builder 是否按场景建了 schedule。

    # 2. 激活 super 首跑（kickoff）
    try:
        await mission_daemon.start(db, pid, kickoff=True)
        actions.append("kickoff")
    except Exception:  # noqa: BLE001
        logger.exception("[finalize] kickoff 失败 project=%s", mission_id)

    # 3. Builder mission 主 thread 写 super_activated 消息 → 前端渲「进入 super →」按钮
    if notify_mission_id:
        try:
            await messaging_service.append_message(
                db, notify_mission_id, notify_thread_key or "main", role="agent_log",
                content=f"✅ {proj.name or proj.slug} 已建好并激活。点「进入 super →」进它的工作台，它会给你一份运营方案，确认或微调即可。",
                meta={"type": "super_activated", "project_slug": proj.slug,
                      "project_name": proj.name or proj.slug},
            )
            actions.append("button")
        except Exception:  # noqa: BLE001
            logger.exception("[finalize] 写 super_activated 消息失败")

    logger.info("[finalize] 确定性收尾 project=%s actions=%s", proj.slug, actions)
    return {"ok": True, "finalized": proj.slug, "actions": actions}
