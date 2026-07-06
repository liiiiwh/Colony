"""Pending Approval 业务逻辑：

- request_approval skill 落库时调 `create_pending(...)`：
  - 写 pending_approvals 行
  - 如果该 project 配置了 wechat clawbot 渠道，把审批内容同步发到指定 wechat 审批人
- 用户在 observe 页 / 微信里回复后调 `decide(...)`：
  - 更新行状态为 decided
  - 同步写一条 `[approval_response request_id=... 用户选择=X]` 消息到 daemon session
    （daemon supervisor 下次 invoke 会读到，从而续推进流程）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.approvals import (
    PendingApproval,
    MissionApprovalChannel,
    WechatClawbotAccount,
)

logger = logging.getLogger(__name__)


def _short_request_id() -> str:
    """6 字符短 ID，给微信用户复用回复时也能 type 出来。"""
    return uuid.uuid4().hex[:8]


def serialize_approval(pa: PendingApproval) -> dict:
    """ADR-024 #1/#3 · 审批行 → 前端 ApprovalCardData（读时合并真相源 = pending_approvals）。

    始终带 `thread_key`（None→'main'）供前端按 thread 过滤；decided 时带 `resolution`
    让刷新后已决卡保持「已决定/禁用」，不再幽灵复活成可点。
    """
    d: dict = {
        "id": str(pa.id),
        "request_id": pa.request_id,
        "title": pa.title,
        "message": pa.message,
        "options": pa.options,
        "created_at": pa.created_at.isoformat() if pa.created_at else None,
        "thread_key": pa.thread_key or "main",
        "status": pa.status,
    }
    if pa.status == "decided" and pa.decided_option is not None:
        d["resolution"] = {
            "option": pa.decided_option,
            "decided_by": pa.decided_by,
            "via": "wechat" if pa.clawbot_account_id else "inline",
        }
    return d


async def get_channel(
    db: AsyncSession, mission_id: uuid.UUID
) -> tuple[WechatClawbotAccount | None, list[str]]:
    """返回 (clawbot_account, reviewer_wechat_ids) 二元组。

    没配 / disabled / 缺账号 → 返回 (None, [])。
    项目级 reviewer_wechat_ids 为空则继承 account.reviewers。
    """
    r = await db.execute(
        select(MissionApprovalChannel).where(
            MissionApprovalChannel.mission_id == mission_id
        )
    )
    cfg = r.scalar_one_or_none()
    if cfg is None or not cfg.enabled or cfg.clawbot_account_id is None:
        return None, []
    acc = await db.get(WechatClawbotAccount, cfg.clawbot_account_id)
    if acc is None or not acc.is_enabled:
        return None, []
    reviewers = list(cfg.reviewer_wechat_ids or []) or list(acc.reviewers or [])
    return acc, reviewers


async def _pause_for_pending(db: AsyncSession, mission_id: uuid.UUID) -> None:
    """ADR-025 D3 · 有 pending 卡时把 mission 暂停到 paused_clarification。

    仅当前为 running 才转换（PAUSE_FOR_CLARIFICATION 的唯一合法源态）；其它态（已暂停/
    停止/错误）跳过——保持幂等，绝不因暂停失败而阻断落卡。"""
    from app.models.mission import Mission

    proj = await db.get(Mission, mission_id)
    if proj is None or proj.lifecycle_status != "running":
        return
    try:
        from app.domain.lifecycle import LifecycleAction
        from app.domain.lifecycle_service import LifecycleService

        await LifecycleService(db).transition(
            mission_id, LifecycleAction.PAUSE_FOR_CLARIFICATION,
            reason="pending_approval",
        )
    except Exception:  # noqa: BLE001
        logger.exception("[pending_approval] 落卡暂停 mission 失败（不阻塞）mission=%s", mission_id)
        return

    # ADR-028 D4 · H1 · 人工门落卡 → **只发协作取消信号**（E2 在每个 tool 结果后检查）。
    # ⚠️ 不能 cancel_current_tick：本函数在 tick 的 LangGraph tool 子 task 里执行，
    # `task is current_task()` 自检失效 → 硬 cancel 路径 = tool 等 tick、tick 等 tool 的
    # 死锁，超时强 cancel 再撞千层 gather 链 RecursionError → tick 僵尸永久持锁
    # （2026-07-03 mission-cc129f 审批后挂死事故）。
    try:
        from app.services import super_inbox
        super_inbox.signal_cancel(mission_id)
    except Exception:  # noqa: BLE001
        logger.exception("[pending_approval] H1 signal_cancel 失败（不阻塞）mission=%s", mission_id)


async def _resume_after_clarification(db: AsyncSession, mission_id: uuid.UUID) -> None:
    """ADR-025 D3 + ADR-028 D4 H2 · 卡已了结时把 mission 从 paused_for_human 恢复 running。

    统一覆盖两种人工门态（paused_clarification / paused_waiting_capability）——决卡触发
    re-probe / resume，不再只认 paused_clarification（H2）。用 generic RESUME（force）兼容
    两态。仅当前为 paused_for_human 才转换（幂等）；其它态跳过，绝不阻断主流程。"""
    from app.models.mission import Mission

    proj = await db.get(Mission, mission_id)
    if proj is None or proj.lifecycle_status not in (
        "paused_clarification", "paused_waiting_capability",
    ):
        return
    try:
        from app.domain.lifecycle import LifecycleAction
        from app.domain.lifecycle_service import LifecycleService

        # generic RESUME（force=True）：paused_clarification / paused_waiting_capability 统一恢复。
        await LifecycleService(db).transition(
            mission_id, LifecycleAction.RESUME,
            reason="approval_resolved", force=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[pending_approval] 卡了结恢复 mission 失败（不阻塞）mission=%s", mission_id)


async def create_pending(
    db: AsyncSession,
    *,
    mission_id: uuid.UUID,
    title: str,
    message: str,
    options: list[str],
    thread_key: str | None = None,
    agent_node_name: str | None = None,
    dispatch_wechat: bool = True,
    request_id: str | None = None,
) -> PendingApproval:
    """落 pending_approvals + 可选同步发微信（ADR-018 mission-only：按 mission_id + thread_key）。

    ADR-025 D3 · 落卡即把 mission 暂停到 paused_clarification（有卡⟺暂停不变式）。
    request_id 可由调用方（如 request_approval skill）传入复用业务 ID；不传则自生。"""
    # ADR-024 #2 + 单卡不变量（2026-07-03）· mission 内已有任何未决审批 → 复用不新建。
    # **不看 thread_key**：super 提案卡（thread='main'）与 readiness QR 卡（thread=None）曾
    # 因 thread_key 不一致绕过去重 → 双卡并存 → chat-as-comment 误杀。收窄到 mission 级：
    # 每 mission 至多一张 pending 卡，杜绝连续多卡。
    existing = (await db.execute(
        select(PendingApproval)
        .where(
            PendingApproval.mission_id == mission_id,
            PendingApproval.status == "pending",
        )
        .order_by(PendingApproval.created_at.asc())
    )).scalars().first()
    if existing is not None:
        logger.info(
            "[pending_approval] 同 thread 已有未决审批 req=%s，复用不新建（mission=%s thread=%s）",
            existing.request_id, mission_id, thread_key,
        )
        await _pause_for_pending(db, mission_id)
        return existing

    req_id = request_id or _short_request_id()
    row = PendingApproval(
        mission_id=mission_id,
        request_id=req_id,
        thread_key=thread_key,
        agent_node_name=agent_node_name,
        title=title,
        message=message,
        options=list(options),
        status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    # ADR-029 · 落卡即在**规范点（create_pending）**向 event_bus 推 approval_request（所有调用方
    # 统一覆盖，不只 super skill）。bus 带重放缓冲 → 连接空窗/重连也能补齐；前端 handler 按
    # request_id 幂等，安全。
    # ⚠️ 必须在 _pause_for_pending **之前** publish：_pause_for_pending 里的 ADR-028 D4 H1
    # 会 cancel 当前 tick（cooperative cancel），若放其后，本协程常在 await 处被 cancel 掉 →
    # publish 永不执行 → 前端拿不到 approval_request → 卡片渲染成「已关闭」需手刷（实测根因）。
    try:
        from app.services.event_bus import bus as _bus
        await _bus.publish(mission_id, {
            "type": "approval_request",
            "request_id": row.request_id,
            "title": row.title,
            "message": row.message,
            "options": list(row.options or []),
            "thread_key": row.thread_key,
            "created_at": (row.created_at.isoformat() if row.created_at else None),
        })
    except Exception:  # noqa: BLE001
        logger.exception("[pending_approval] publish approval_request 失败（不阻塞）req=%s", req_id)

    # ADR-025 D3 · 审批暂停不变式：落卡即暂停 mission（调度器/cron 跳过暂停态），
    # 仅答卡或用户发消息能 resolve→running 续跑。（放在 publish 之后：本步可能 H1 cancel 本 tick）
    await _pause_for_pending(db, mission_id)

    if dispatch_wechat:
        try:
            await _dispatch_to_wechat(db, row)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[pending_approval] 微信分发失败 request_id=%s（不阻塞，仍可在 observe 页审）",
                req_id,
            )
    return row


async def _dispatch_to_wechat(db: AsyncSession, row: PendingApproval) -> None:
    """如果项目配了 clawbot 渠道，把审批信息发到所有 reviewer。"""
    from app.core.encryption import decrypt
    from app.services import wechat_clawbot

    acc, reviewers = await get_channel(db, row.mission_id)
    if acc is None or not reviewers:
        return  # 未配渠道，跳过

    # ADR-008 P3 · 审批消息带平台深链：{frontend_base}/mission/{slug}?session={sid}
    from app.core.config import settings
    from app.domain.approval.wechat_format import (
        build_approval_message, build_mission_deep_link,
    )
    from app.models.mission import Mission
    proj = await db.get(Mission, row.mission_id)
    mission_url = build_mission_deep_link(
        frontend_base=settings.frontend_base_url,
        slug=(proj.slug if proj else ""),
        session_id=None,  # ADR-018 mission-only · 深链按 slug，不带 session
    )
    body = build_approval_message(
        request_id=row.request_id,
        title=row.title,
        message=row.message,
        options=row.options or [],
        mission_url=mission_url,
    )
    token = decrypt(acc.bot_token)
    sent_ok: list[str] = []
    failed: list[dict] = []
    ctx_tokens = acc.context_tokens or {}
    for uid in reviewers:
        try:
            await wechat_clawbot.send_text(
                token=token,
                base_url=acc.base_url,
                to_user_id=uid,
                text=body,
                context_token=str(ctx_tokens.get(uid, "")),
            )
            sent_ok.append(uid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[pending_approval] 发到 wechat user=%s 失败（落 outbox 等用户首次对话）: %s",
                uid, exc,
            )
            failed.append({"user_id": uid, "error": str(exc)})

    if sent_ok or failed:
        row.clawbot_account_id = acc.id
        row.clawbot_user_ids = sent_ok or [f["user_id"] for f in failed]
        row.clawbot_sent_at = datetime.now(UTC) if sent_ok else None
        await db.commit()

    # 失败的塞 outbox，下次用户主动发消息时 poller 会 flush
    if failed:
        from app.services import wechat_outbox
        await wechat_outbox.queue(
            db, account_id=acc.id, mission_id=row.mission_id,
            failed_recipients=failed, content=body, kind="approval_resend",
        )


async def _readiness_reason_for(db: AsyncSession, mission_id: uuid.UUID) -> str | None:
    """mission 若因 readiness 人工门暂停，返回其 paused_reason，否则 None。"""
    from app.models.mission import Mission

    proj = await db.get(Mission, mission_id)
    if proj is None or proj.lifecycle_status != "paused_waiting_capability":
        return None
    reason = proj.paused_reason or ""
    return reason if reason.startswith("readiness:") else None


async def _rerun_readiness_if_applicable(
    db: AsyncSession, mission_id: uuid.UUID, reason: str
) -> None:
    """readiness 卡（QR/密钥/条款）决策后重跑 ensure_ready 复验。

    reason 形如 'readiness:<server>:<req> …'。重跑 ensure_ready_for_server：登录已完成 →
    无 pending，mission 由复验自然继续；未完成 → 重新拉一张新 QR 卡（QR 会过期，每次复验拉
    新的即可，无需单独刷新端点）。死按钮由此闭环。
    """
    from app.models.skill import MCPServer
    from sqlalchemy import select as _sel

    server_name = reason.split(":", 2)[1] if reason.count(":") >= 2 else None
    if not server_name:
        return
    server = (await db.execute(
        _sel(MCPServer).where(MCPServer.name == server_name)
    )).scalar_one_or_none()
    if server is None:
        return
    from app.services import readiness as rd
    await rd.ensure_ready_for_server(db, server.id, mission_id=mission_id)


async def decide(
    db: AsyncSession,
    *,
    request_id: str,
    option: str,
    decided_by: str,
) -> PendingApproval | None:
    """用户作出决定 → 更新行 + 把审批响应消息写回 daemon session，让 supervisor 下次读到。"""
    r = await db.execute(
        select(PendingApproval).where(PendingApproval.request_id == request_id)
    )
    row = r.scalar_one_or_none()
    if row is None:
        return None
    if row.status != "pending":
        # 已决定过，幂等返回
        return row
    row.status = "decided"
    row.decided_option = option
    row.decided_by = decided_by
    row.decided_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)

    # 在 resume 翻态之前先抓 readiness 上下文（_resume_after_clarification 会把 mission 转 running）。
    _readiness_reason = await _readiness_reason_for(db, row.mission_id)

    # ADR-025 D3 · 答卡即恢复：至多一张卡，故无需查其它 pending。触发 tick 前先 resolve→running，
    # 否则唤醒的 tick 会被 paused 守卫挡掉。
    await _resume_after_clarification(db, row.mission_id)

    # QR/密钥等 readiness 卡的「我已完成/刷新」→ 重跑 ensure_ready 复验（复验恢复钩子）：
    # 登录成功则不再 pending、mission 复位继续；否则重新拉一张新 QR。死按钮由此闭环。
    if _readiness_reason:
        try:
            await _rerun_readiness_if_applicable(db, row.mission_id, _readiness_reason)
        except Exception:  # noqa: BLE001
            logger.exception("[pending_approval] readiness 复验失败（不阻塞）req=%s", request_id)

    # ADR-018 mission-only · 审批结果作为 [approval_response] 写入 (mission_id, thread_key)，
    # 并 enqueue 进 super_inbox + 触发 idle-tick（daemon super 下一 tick pick up）。
    # 原 orchestrator advance（stream_chat_reply）路径随 orchestrator 退役一并删除。
    try:
        await _write_response_message(db, row, option, decided_by)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[pending_approval] 写 approval_response 消息失败 req=%s", request_id
        )
    try:
        from app.services import super_inbox
        from app.models.mission import Mission
        proj = await db.get(Mission, row.mission_id)
        runtime = (proj.runtime_status if proj else "")
        # daemon tick 的 LLM 上下文不加载 thread 历史 → 把审批回复 enqueue 进 super_inbox，
        # 下一 tick 的 prompt 就带上它（否则仅落 messages 表的 [approval_response] super 看不到）。
        if proj is not None and proj.supervisor_agent_id is not None:
            try:
                from app.services.pending_queue import enqueue_user_message
                await enqueue_user_message(
                    db, row.mission_id, proj.supervisor_agent_id,
                    content=(
                        f"[approval_response request_id={row.request_id}] "
                        f"审批标题：{row.title} | 用户选择：{option}"
                    ),
                    meta={"approval_response": {"request_id": row.request_id, "option": option}},
                )
            except Exception:  # noqa: BLE001
                logger.exception("[pending_approval] enqueue approval_response 到 super_inbox 失败 req=%s", row.request_id)
        # ADR-028 fix · 决卡后**无条件**调度续跑（只要 runtime 可恢复）。旧逻辑用
        # should_trigger_now 在 is_running=True 时丢弃触发 → 与「正被 cancel 的 propose-tick
        # 尚未 unregister」竞态 → 确认建造后构建永不继续（Chrome e2e 真实复现的卡死）。
        # run_once 的 _TICKING 守卫兜底防重跑。
        if runtime in ("running", "paused_idle", "starting"):
            from app.api.super_conversation import _trigger_tick_async
            from app.core.bg_tasks import spawn
            spawn(
                _trigger_tick_async(row.mission_id),
                name=f"approval-tick-{row.request_id}",
            )
            logger.info(
                "[pending_approval] 审批后已调度续跑 tick project=%s req=%s (is_running=%s, runtime=%s)",
                row.mission_id, row.request_id,
                super_inbox.is_running(row.mission_id), runtime,
            )
    except Exception:  # noqa: BLE001
        logger.exception("[pending_approval] 派发后续任务失败（不阻塞）")
    return row


async def _write_response_message(
    db: AsyncSession, row: PendingApproval, option: str, decided_by: str
) -> None:
    """把 [approval_response] 消息落到 (mission_id=mission_id, thread_key)（ADR-018 mission-only）。"""
    # 回执必须自包含被确认的方案正文——原卡片可能已滑出 LLM 上下文（隔了很久 / 被压缩），
    # 只给标题会让确认后的 tick 靠标题脑补方案（真出过：幻觉出方案里不存在的 worker）。
    body = (row.message or "").strip()
    if len(body) > 4000:
        body = body[:4000] + "\n…（正文过长已截断）"
    payload_text = "\n".join(
        [
            f"[approval_response request_id={row.request_id}]",
            f"审批标题：{row.title}",
            "审批说明（用户确认的就是下面这份内容，执行时以此为准）：",
            body or "（无正文）",
            f"用户选择：{option}",
            f"决策人：{decided_by}",
        ]
    )
    from app.services import messaging_service as _sess_svc
    await _sess_svc.append_message(
        db, row.mission_id, row.thread_key or "main",
        role="user",
        content=payload_text,
        meta={
            "approval_response": {
                "request_id": row.request_id,
                "option": option,
                "title": row.title,
                "decided_by": decided_by,
            }
        },
    )


async def list_pending_for_project(
    db: AsyncSession, mission_id: uuid.UUID, status: str | None = "pending"
) -> list[PendingApproval]:
    stmt = select(PendingApproval).where(PendingApproval.mission_id == mission_id)
    if status:
        stmt = stmt.where(PendingApproval.status == status)
    stmt = stmt.order_by(PendingApproval.created_at.desc()).limit(50)
    return list((await db.execute(stmt)).scalars().all())


async def get_by_request_id(
    db: AsyncSession, request_id: str
) -> PendingApproval | None:
    r = await db.execute(
        select(PendingApproval).where(PendingApproval.request_id == request_id)
    )
    return r.scalar_one_or_none()


__all__ = (
    "create_pending",
    "decide",
    "list_pending_for_project",
    "get_by_request_id",
    "get_channel",
)
