"""v4 · 用户跟 super 实时对话 API。

3 个端点：
- POST /api/super/{slug}/chat     — 写主 thread + 入队 + cancel + 立即触发新 tick
- POST /api/super/{slug}/interrupt — 强 cancel 当前 tick（不发消息）
- GET  /api/super/{slug}/stream    — SSE 推 super 实时事件（tick 状态 / 新消息）

与 v3 `/api/super/{slug}/threads /stats /artifacts` 并存；不影响。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.core import system_settings as _ss
from app.core.deps import CurrentUser, DBSession
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.mission import Mission
from app.models.message import Message
from app.services import super_inbox

logger = logging.getLogger(__name__)
router = APIRouter(tags=["super-conversation"])

# run_once no-op（已有 tick 在跑）时，退避多久再轮询 respawn 续跑。防零延迟 busy-loop。
_SKIP_RESPAWN_BACKOFF_SEC = 1.0


class ChatAttachment(BaseModel):
    kind: str  # 'image' | 'file'
    name: str
    url: str
    mediaType: str | None = None
    size: int | None = None


class ChatBody(BaseModel):
    content: str
    meta: dict | None = None
    attachments: list[ChatAttachment] | None = None
    auto_start: bool = True  # v4 · super 是 stopped 时自动 start（默认开）


class ChatResp(BaseModel):
    ok: bool
    message_id: str | None = None  # v6 fix · main thread Message.id（用于前端 dedup）
    pending_queue_id: str | None = None  # super_pending_messages 队列行 id
    queue_size_after: int | None = None
    cancel_result: dict | None = None
    triggered_tick: bool = False
    v38_offloaded: bool = False
    auto_started: bool = False  # v4 · 是否本次 chat 顺手 start 了 super
    lifecycle_after: str | None = None
    error: str | None = None
    warning: str | None = None
    # v6 · 若本次 chat 顺带把 pending approval 用 free-form 决议了，回传给前端 toast
    auto_decided_approval: dict | None = None


async def post_user_message_and_trigger(
    db,
    proj: Mission,
    content: str,
    *,
    user_id,
    sup: Agent | None = None,
    meta: dict | None = None,
) -> bool:
    """Append a user message to the mission's main thread + auto-start daemon + trigger run_once.

    Reusable core extracted from `super_chat`：让任何路径（用户实时聊天 / 新建 Mission 带
    goal_hint）都能「把一句话当作用户输入丢给 super 并立刻处理」。

    流程（与 super_chat Step 2/5/6 一致）：
    1. 写主 thread message（role='user'）+ enqueue 到 super_pending_messages
    2. 若 daemon 非 running → mission_daemon.start（含 freshly-created mission：start 内部把
       draft→active 并同步 lifecycle）
    3. super idle 时立即 trigger 一个新 tick；正跑 tick 则排队，tick 完 auto-drain 接手

    返回是否触发了新 tick（triggered_tick）。
    """
    if sup is None:
        sup = await db.get(Agent, proj.supervisor_agent_id)

    max_pending = await _ss.get_int(db, "super.max_pending_msgs_per_super", 20)
    max_content_kb = await _ss.get_int(db, "super.pending_msg_max_kb_per_msg", 50)
    auto_trigger = await _ss.get_bool(db, "super.auto_trigger_on_user_msg", True)

    # Step 1 · 写主 thread message（ADR-018 mission-only）
    from app.services import messaging_service as _sess_svc
    main_msg = await _sess_svc.append_message(
        db, proj.id, "main",
        role="user",
        content=content,
        meta={
            "source": "user_chat",
            "actor_user_id": str(user_id),
            **(meta or {}),
        },
    )

    # enqueue 到 pending 队列
    await super_inbox.enqueue_user_message(
        db, proj.id, sup.id if sup else None, content,
        meta={
            "actor_user_id": str(user_id),
            "main_msg_id": str(main_msg.id),
            **(meta or {}),
        },
        max_pending=max_pending,
        max_content_kb=max_content_kb,
    )

    # Step 2 + 3 · 自动 start + idle 立即触发（与 super_chat 共用同一缝 _autostart_and_trigger）
    _started, triggered, _warn, _lifecycle = await _autostart_and_trigger(
        db, proj.id, user_id, auto_trigger=auto_trigger, auto_start=True,
    )
    return triggered


async def _autostart_and_trigger(
    db,
    mission_id,
    user_id,
    *,
    auto_trigger: bool,
    auto_start: bool,
) -> tuple[bool, bool, str | None, str]:
    """super_chat / post_user_message_and_trigger 共用的「自动 start daemon + idle 立即触发」缝。

    返回 (auto_started, triggered_tick, warning, lifecycle_after)。freshly-created mission
    （run_count==0、status='draft'）也走这条：mission_daemon.start 内部把 draft→active 并同步
    lifecycle，run_once 只看 runtime_status='running' 即可被立即触发的新 tick 消费。
    """
    auto_started = False
    warning: str | None = None
    proj_refreshed = await db.get(Mission, mission_id)
    current_lifecycle = proj_refreshed.lifecycle_status if proj_refreshed else "unknown"
    current_runtime = proj_refreshed.runtime_status if proj_refreshed else "unknown"

    # ADR-028 D4 · H3 · 用户消息对 paused_* 先 RESUME→running 再触发。
    # 任何 pause（人工门 paused_for_human / 阶段完成 paused_idle）收到用户消息都意味着
    # 用户要继续 → 统一 RESUME，让随后的 should_trigger_now / tick 守卫放行（否则消息被吞）。
    _PAUSED_RESUMABLE = (
        "paused_clarification", "paused_waiting_capability", "paused_idle",
    )
    if proj_refreshed is not None and current_lifecycle in _PAUSED_RESUMABLE:
        try:
            from app.domain.lifecycle_service import LifecycleService
            from app.domain.lifecycle import LifecycleAction
            await LifecycleService(db).transition(
                mission_id, LifecycleAction.RESUME, force=True,
            )
            await db.refresh(proj_refreshed)
            current_lifecycle = proj_refreshed.lifecycle_status
            current_runtime = proj_refreshed.runtime_status
        except Exception:
            logger.exception("[autostart] paused→running resume failed (不阻塞)")

    needs_start = (
        auto_start
        and proj_refreshed is not None
        and current_runtime != "running"
        and current_lifecycle != "paused_waiting_capability"
        and current_lifecycle != "error"
    )
    if needs_start:
        try:
            from app.services import mission_daemon
            await mission_daemon.start(db, mission_id)
            await db.refresh(proj_refreshed)
            if proj_refreshed.lifecycle_status != "running":
                try:
                    from app.domain.lifecycle_service import LifecycleService
                    from app.domain.lifecycle import LifecycleAction
                    await LifecycleService(db).transition(
                        mission_id, LifecycleAction.START, force=True,
                    )
                except Exception:
                    logger.exception("[autostart] LifecycleService.start failed")
                await db.refresh(proj_refreshed)
            auto_started = True
            current_lifecycle = proj_refreshed.lifecycle_status
            current_runtime = proj_refreshed.runtime_status
        except Exception as exc:
            logger.exception("[autostart] auto_start failed")
            warning = f"自动 start 失败: {exc}"
    elif current_lifecycle == "paused_waiting_capability":
        warning = (
            "super 当前 paused_waiting_capability（缺 capability）；"
            "消息已入队列，但需等 Builder 处理 capability 后恢复才会被读。"
        )
    elif current_lifecycle == "error":
        warning = "super 当前为 error 状态；请先到工作台上看 last_error 并 restart"

    from app.domain.tick_policy import should_trigger_now
    _is_running = super_inbox.is_running(mission_id)
    triggered = False
    if auto_trigger and should_trigger_now(is_running=_is_running, runtime_status=current_runtime):
        try:
            from app.services.mission_daemon import _spawn_bg
            _spawn_bg(_trigger_tick_async(mission_id, user_id), name=f"tick-{mission_id}")
            triggered = True
        except Exception:
            logger.exception("[autostart] trigger tick failed (不阻塞)")
    elif auto_trigger and _is_running:
        warning = (warning + " · " if warning else "") + (
            "super 正在处理上一轮，消息已排队；当前 tick 一结束会第一时间接手"
        )
    elif auto_trigger and current_runtime != "running":
        warning = (warning + " · " if warning else "") + (
            f"未触发新 tick (runtime_status={current_runtime})；消息已入队，下次 scheduler 触发时被消费"
        )
    return auto_started, triggered, warning, current_lifecycle


async def _resolve_super(db, slug: str) -> tuple[Mission, Agent]:
    proj = (await db.execute(select(Mission).where(Mission.slug == slug))).scalar_one_or_none()
    if proj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"slug={slug} project 不存在")
    if not proj.supervisor_agent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project 无 supervisor_agent")
    sup = await db.get(Agent, proj.supervisor_agent_id)
    if sup is None or sup.kind != "super":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"supervisor agent kind={sup.kind if sup else None}（要求 super）",
        )
    return proj, sup


@router.post("/api/super/{slug}/chat", response_model=ChatResp)
async def super_chat(slug: str, body: ChatBody, db: DBSession, user: CurrentUser) -> ChatResp:
    """V4 主入口：用户给 super 发实时消息。

    流程：
    1. 写 super 主 thread（角色 user）—— 让 super 历史里能看到
    2. enqueue 到 super_pending_messages 队列（兜底持久化，重启不丢）
    3. cancel 当前 tick（cooperative，超时强 cancel）
    4. 如果 auto_trigger_on_user_msg=true → 立即 trigger run_once
    """
    proj, sup = await _resolve_super(db, slug)
    if not (body.content or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content 不能为空")

    # 读 admin 可调配置
    max_pending = await _ss.get_int(db, "super.max_pending_msgs_per_super", 20)
    max_content_kb = await _ss.get_int(db, "super.pending_msg_max_kb_per_msg", 50)
    cancel_timeout = await _ss.get_float(db, "super.user_chat_cancel_timeout_seconds", 10.0)
    auto_trigger = await _ss.get_bool(db, "super.auto_trigger_on_user_msg", True)

    # Step 1 · 拼 content（R4-4 · 纯函数 build_user_message_content）
    from app.domain.super_chat.intake import build_user_message_content
    content_with_atts = build_user_message_content(body.content, body.attachments)

    # ADR-018 mission-only · 删 ADR-011 首跑中继（relay_service / builder-chat-as-session 退役）。

    # v6 · 未审批时用户输入的 chat 自动当作审批意见（free-form 决定）
    # 用户讲话「调整 schedule 时间」/「我要先配好 MCP」等自然语言 → 直接 decide
    # 最旧那条 pending approval，option = 用户原文。daemon supervisor 下次 tick 读
    # [approval_response] 消息，自然按用户意见继续。
    auto_decided_approval: dict | None = None
    try:
        from app.models.approvals import PendingApproval as _PA
        from sqlalchemy import select as _sel
        # 绑**最新**那张 pending 卡：用户的自由文本回应的是刚弹出来的卡（如 QR），
        # 不是很久前遗留的卡（旧代码取 asc 最旧 → 误杀遗留的运营计划卡，v4 真出过）。
        # 单卡不变量下 mission 至多一张，desc 只是稳妥兜底。
        oldest_pa = (await db.execute(
            _sel(_PA)
            .where(_PA.mission_id == proj.id, _PA.status == "pending")
            .order_by(_PA.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if oldest_pa is not None:
            from app.services import pending_approval_service as _pa_svc
            from app.domain.approval.resolution import build_auto_decide_option
            decided = await _pa_svc.decide(
                db,
                request_id=oldest_pa.request_id,
                option=build_auto_decide_option(body.content),
                decided_by=f"user:{user.id} (chat-as-comment)",
            )
            if decided is not None:
                auto_decided_approval = {
                    "request_id": decided.request_id,
                    "title": decided.title,
                    "option": decided.decided_option,
                }
                # 推 SSE 让前端卡片立刻 flip 成 Resolved
                try:
                    from app.services.event_bus import bus as _bus
                    if decided.mission_id:
                        await _bus.publish(decided.mission_id, {
                            "type": "approval_resolved",
                            "request_id": decided.request_id,
                            "option": decided.decided_option,
                            "decided_by": "user (free-form)",
                            "via": "chat",
                        })
                except Exception:
                    pass
                logger.info(
                    "[super_chat] auto-decided pending approval req=%s with chat text",
                    decided.request_id,
                )
    except Exception:
        logger.exception("[super_chat] auto-decide pending approval failed (不阻塞)")

    # Step 2 · 写主 thread message（ADR-018 mission-only：直接 (mission_id=proj.id, thread_key='main')）
    from app.services import messaging_service as _sess_svc
    main_msg = await _sess_svc.append_message(
        db, proj.id, "main",
        role="user",
        content=content_with_atts,
        meta={
            "source": "user_chat",
            "actor_user_id": str(user.id),
            "attachments": [a.model_dump() for a in (body.attachments or [])],
            **(body.meta or {}),
        },
    )

    # Step 3 · enqueue 到 pending 队列
    enq = await super_inbox.enqueue_user_message(
        db, proj.id, sup.id, content_with_atts,
        meta={
            "actor_user_id": str(user.id),
            "main_msg_id": str(main_msg.id),
            "attachments": [a.model_dump() for a in (body.attachments or [])],
            **(body.meta or {}),
        },
        max_pending=max_pending,
        max_content_kb=max_content_kb,
    )
    if not enq.get("ok"):
        return ChatResp(ok=False, error=enq.get("error"))

    # Step 4 · V7.2 · 不再 cancel（ADR-007 tick 边界插入）。
    # super 正跑 tick → 消息进 pending_queue 等当前 tick 完即被 auto-drain 接手；
    # super idle → 下面 Step 6 立即触发新 tick。前端用户消息显示 loading 直到被处理。
    cancel_res = None

    # Step 5+6 · v5 · 自动 start（修 v4 bug：run_once 看 runtime_status 非 lifecycle_status）
    # + idle 时立即触发新 tick。抽到 _autostart_and_trigger 缝，与新建 Mission(goal_hint) 路径共用。
    auto_started, triggered, warning, current_lifecycle = await _autostart_and_trigger(
        db, proj.id, user.id, auto_trigger=auto_trigger, auto_start=body.auto_start,
    )

    return ChatResp(
        ok=True,
        # v6 fix · 返回 main thread message.id（SSE 后续推送也用同 id），让前端
        # 乐观更新与 SSE 推送 dedup 一致；老逻辑返回 pending_queue id 导致 UI 重复。
        message_id=str(main_msg.id),
        pending_queue_id=enq.get("message_id"),
        queue_size_after=enq.get("queue_size_after"),
        cancel_result=cancel_res,
        triggered_tick=triggered,
        v38_offloaded=enq.get("v38_offloaded", False),
        auto_started=auto_started,
        lifecycle_after=current_lifecycle,
        warning=warning,
        auto_decided_approval=auto_decided_approval,
    )


async def _trigger_tick_async(mission_id: uuid.UUID, actor_user_id: uuid.UUID | None = None) -> None:
    """异步触发 super tick；register_task 让后续 cancel 能找到。

    actor_user_id 仅用于 auto-drain 递归透传（run_once 本身不消费），审批等
    非用户触发路径可传 None。
    """
    from app.services import mission_daemon

    async def _run() -> None:
        _skipped = False
        try:
            async with AsyncSessionLocal() as db:
                _res = await mission_daemon.run_once(
                    db, mission_id,
                    payload={"trigger": "user_chat", "user_message": ""},  # message 走 pending 队列读
                )
                _skipped = bool(isinstance(_res, dict) and _res.get("skipped"))
        except asyncio.CancelledError:
            logger.info("[trigger_tick] project=%s cancelled (cooperative)", mission_id)
        except Exception:
            logger.exception("[trigger_tick] failed project=%s", mission_id)
        finally:
            # V7.2 · tick 边界 auto-drain：本 tick 跑完后若还有 pending，立即开下一 tick。
            # ⚠️ run_once no-op（已有 tick 在跑 → skipped）时**退避**再 respawn，而不是零延迟自触发：
            # daemon 循环跑的 kickoff/scheduled tick 结束时不走本 auto-drain，故必须由本路径轮询
            # 到锁释放才能接力续跑（不能直接 return，否则审批后续跑丢失）。但零延迟 respawn 会
            # busy-loop（真出过 83s 空转 10 万次 + 每轮开 2 个 DB session 耗尽连接池，用户审批后
            # 干等一分多钟）。退避到 ~1/s：既保证续跑不丢，又不烧 CPU / 不撑爆池。
            if _skipped:
                await asyncio.sleep(_SKIP_RESPAWN_BACKOFF_SEC)
            try:
                from app.domain.tick_policy import should_drain_after_tick
                async with AsyncSessionLocal() as _db:
                    _pending = await super_inbox.count_pending(_db, mission_id)
                    # ADR-028 D4 · auto-drain 查 lifecycle：人工门/停止/错误态不续。
                    _fresh = await _db.get(Mission, mission_id)
                    _ls = (_fresh.lifecycle_status if _fresh else "") or ""
                if should_drain_after_tick(pending_count=_pending, lifecycle_status=_ls):
                    if not _skipped:
                        logger.info("[trigger_tick] auto-drain: %d pending → 立即开下一 tick", _pending)
                    from app.services.mission_daemon import _spawn_bg
                    _spawn_bg(
                        _trigger_tick_async(mission_id, actor_user_id),
                        name=f"drain-tick-{mission_id}",
                    )
            except Exception:
                logger.exception("[trigger_tick] auto-drain check failed (不阻塞)")

    # ⚠️ 这里**不注册** task：注册归 run_once 里抢到并发锁的真 tick（_run_once_body 自注册）。
    # 老代码在此 register_task → 1s 退避轮询的每个 skip task 都覆盖注册表、并清掉
    # cancel_event（人工门协作取消信号每秒被抹一次）；其 finally 的无条件 unregister 又会
    # 弹掉真 tick 的注册 → 外部 cancel 找错对象（2026-07-03 事故帮凶）。
    from app.services.mission_daemon import _spawn_bg
    _spawn_bg(_run(), name=f"tick-run-{mission_id}")


class InterruptResp(BaseModel):
    ok: bool
    cancel_result: dict | None = None


@router.post("/api/super/{slug}/interrupt", response_model=InterruptResp)
async def super_interrupt(slug: str, db: DBSession, _user: CurrentUser) -> InterruptResp:
    """V4 强 cancel super 当前 tick（不发消息）。"""
    proj, _sup = await _resolve_super(db, slug)
    cancel_timeout = await _ss.get_float(db, "super.user_chat_cancel_timeout_seconds", 10.0)
    res = await super_inbox.cancel_current_tick(proj.id, timeout_seconds=cancel_timeout)
    return InterruptResp(ok=True, cancel_result=res)


# ─────────────────────────── v4.3 · 消息管理（delete + rewind）────────────────

class MessageOpResp(BaseModel):
    ok: bool
    deleted_messages: int = 0
    dropped_pending: int = 0
    cancelled_current_tick: bool = False
    error: str | None = None


async def _find_main_message(db, slug: str, message_id: uuid.UUID):
    """找到主 thread 上的指定 message；返回 (proj, sup, message)（ADR-018 mission-only）。"""
    from sqlalchemy import select as _sel

    proj, sup = await _resolve_super(db, slug)
    # super 主 thread = (mission_id=proj.id, thread_key='main')
    msg = (await db.execute(
        _sel(Message).where(
            Message.id == message_id, Message.mission_id == proj.id, Message.thread_key == "main"
        )
    )).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"message {message_id} 不存在或不在该 super 的主 thread")
    return proj, sup, msg


@router.delete("/api/super/{slug}/messages/{message_id}", response_model=MessageOpResp)
async def super_delete_message(
    slug: str,
    message_id: uuid.UUID,
    db: DBSession,
    _user: CurrentUser,
) -> MessageOpResp:
    """v4.3 · 删除主 thread 上单条消息（hard delete）。

    同步动作：
    - 如果该 message.meta.main_msg_id 关联 super_pending_messages 中 pending 行，
      把那行标 status='dropped'（让下次 tick 不再读它）
    - 如果当前正在跑 tick → 不主动 cancel（用户若要中断改用 /interrupt）
    """
    from sqlalchemy import text as _sql_text
    proj, _sup, msg = await _find_main_message(db, slug, message_id)
    dropped = 0
    try:
        # 找 pending 队列对应行（按 meta.main_msg_id）
        res = await db.execute(_sql_text("""
            UPDATE super_pending_messages
               SET status='dropped', consumed_at=now()
             WHERE super_mission_id=:pid
               AND status='pending'
               AND meta->>'main_msg_id'=:mid
        """), {"pid": str(proj.id), "mid": str(message_id)})
        dropped = res.rowcount or 0
    except Exception:
        logger.exception("[delete_message] drop pending failed (不阻塞)")
    await db.delete(msg)
    await db.commit()
    return MessageOpResp(ok=True, deleted_messages=1, dropped_pending=dropped)


class RewindBody(BaseModel):
    # cancel_running: 是否 cancel 正在跑的 tick（默认 true）；
    # 因为 rewind 改了 super 看到的世界，正在跑的 tick 已基于旧上下文
    cancel_running: bool = True


@router.post("/api/super/{slug}/messages/{message_id}/rewind", response_model=MessageOpResp)
async def super_rewind(
    slug: str,
    message_id: uuid.UUID,
    body: RewindBody,
    db: DBSession,
    _user: CurrentUser,
) -> MessageOpResp:
    """v4.3 · rewind 到指定 message：删除该 message 之后（created_at 严格大于）的全部 main thread 消息。

    清理链：
    1. 删主 thread 上 created_at > target 的所有 messages
    2. drop super_pending_messages 中创建时间晚于 target 的 pending 行
    3. （可选）cancel 当前正在跑的 tick（默认 true；它跑的就是被 rewind 的世界）

    保留：
    - target message 本身（rewind 到此意为「回到这条之后的状态」之前）
    - MissionAgentMemory（项目级长期记忆；用户若想清需另调 clear_memory）
    - BranchAgentMemory.memory_md（压缩历史；删消息不重写摘要 — super 下次 tick 看到的
      历史是 [memory_md（含已删消息的摘要）+ keep_recent 后剩余]，可能略有 stale 但不破坏）
    """
    from sqlalchemy import text as _sql_text
    proj, _sup, target_msg = await _find_main_message(db, slug, message_id)

    cancelled = False
    if body.cancel_running:
        cancel_timeout = await _ss.get_float(db, "super.user_chat_cancel_timeout_seconds", 10.0)
        cres = await super_inbox.cancel_current_tick(proj.id, timeout_seconds=cancel_timeout)
        cancelled = (cres.get("ok") and cres.get("skipped") != "no_running_tick")

    # 删主 thread 后续 messages（ADR-018 mission-only：按 mission_id + thread_key='main'）
    del_res = await db.execute(_sql_text("""
        DELETE FROM messages
         WHERE mission_id=:pid AND thread_key='main'
           AND created_at > :ts
    """), {"pid": str(proj.id), "ts": target_msg.created_at})
    deleted = del_res.rowcount or 0

    # drop 未消费的 pending 队列（created_at > target）
    drop_res = await db.execute(_sql_text("""
        UPDATE super_pending_messages
           SET status='dropped', consumed_at=now()
         WHERE super_mission_id=:pid
           AND status='pending'
           AND created_at > :ts
    """), {"pid": str(proj.id), "ts": target_msg.created_at})
    dropped = drop_res.rowcount or 0

    await db.commit()
    return MessageOpResp(
        ok=True,
        deleted_messages=deleted,
        dropped_pending=dropped,
        cancelled_current_tick=cancelled,
    )


@router.get("/api/super/{slug}/stream")
async def super_stream(slug: str, db: DBSession):
    # 注意：SSE 不强认证（前端 EventSource 不能塞自定义 header）。
    # 只读 super 状态 + thread 消息；写入接口（/chat /interrupt）仍要 JWT。
    """V5 SSE 推流：super 当前 tick 状态 / 新消息 / 调用 worker / artifact / approval。

    v5：默认订阅 event_bus（in-process pub/sub），实时收到 worker_resolve/start/llm_invoke/done
    + approval_request/approval_resolved + memory events，**零 polling 延迟**。

    可关回老 2s poll 模式：system_settings.live_events_enabled=false 时退化。
    """
    proj, sup = await _resolve_super(db, slug)
    live_enabled = await _ss.get_bool(db, "live_events_enabled", True)

    async def gen() -> AsyncIterator[bytes]:
        # 一进来先推一条 initial state（两种模式都先推）
        async with AsyncSessionLocal() as fdb:
            # v6 · 把未决审批也带出来，前端 chat 流头部渲染 ApprovalCard
            from app.models.approvals import PendingApproval
            from app.services.pending_approval_service import serialize_approval
            from sqlalchemy import select as _sel
            # ADR-024 #1 · 读时合并：pending 全带 + 最近 30 条 decided（带 resolution），
            # 刷新后已决卡保持「已决定/禁用」不复活；每条带 thread_key 供前端按线程过滤(#3)。
            _pending = (await fdb.execute(
                _sel(PendingApproval).where(
                    PendingApproval.mission_id == proj.id,
                    PendingApproval.status == "pending",
                ).order_by(PendingApproval.created_at.asc())
            )).scalars().all()
            _decided = (await fdb.execute(
                _sel(PendingApproval).where(
                    PendingApproval.mission_id == proj.id,
                    PendingApproval.status == "decided",
                ).order_by(PendingApproval.created_at.desc()).limit(30)
            )).scalars().all()
            pa_rows = sorted([*_pending, *_decided], key=lambda p: p.created_at)
            init = {
                "type": "init",
                "mission_id": str(proj.id),
                "slug": proj.slug,
                "lifecycle_status": proj.lifecycle_status,
                "is_running": super_inbox.is_running(proj.id),
                "pending_count": await super_inbox.count_pending(fdb, proj.id),
                "live_events": live_enabled,
                "pending_approvals": [serialize_approval(pa) for pa in pa_rows],
            }
        yield f"data: {json.dumps(init, ensure_ascii=False)}\n\n".encode("utf-8")

        if live_enabled:
            # v5 · 实时模式：订阅 event_bus，同时定时（30s）heartbeat 给前端
            import time as _time

            from app.domain.stream.sse_heartbeat import HEARTBEAT, iter_with_heartbeat
            from app.services.event_bus import bus as _bus
            # ADR-018 mission-only · 直接订阅 Mission channel (mission_id)，无需 session
            channel_id = proj.id
            sub_gen = _bus.subscribe(channel_id)
            # 心跳期间**不取消**订阅 __anext__（旧实现的 cancel 会触发订阅 finally 注销 →
            # 用户盯审批 >30s 再点确认时 decide 触发的 tick 事件 publish 到已死订阅而丢失）。
            try:
                deadline = _time.monotonic() + 1800  # 30 min hard cap
                async for item in iter_with_heartbeat(
                    sub_gen, heartbeat_interval=30.0, deadline=deadline, time_fn=_time.monotonic
                ):
                    if item is HEARTBEAT:
                        yield b"data: {\"type\":\"heartbeat\"}\n\n"
                    else:
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8")
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[super_stream live] failed")
                yield b"data: {\"type\":\"error\",\"msg\":\"live stream failure\"}\n\n"
            yield b"data: {\"type\":\"done\"}\n\n"
            return

        # v4 兼容路径：2s poll
        last_msg_at = None
        last_lifecycle = proj.lifecycle_status
        last_running = super_inbox.is_running(proj.id)
        max_iters = 600  # 20 min max stream
        for _ in range(max_iters):
            await asyncio.sleep(2)
            try:
                async with AsyncSessionLocal() as fdb:
                    fresh = await fdb.get(Mission, proj.id)
                    if fresh is None:
                        yield b"data: {\"type\":\"project_deleted\"}\n\n"
                        break
                    cur_lifecycle = fresh.lifecycle_status
                    cur_running = super_inbox.is_running(proj.id)
                    pending = await super_inbox.count_pending(fdb, proj.id)
                    # lifecycle / running 变化
                    if cur_lifecycle != last_lifecycle or cur_running != last_running:
                        ev = {
                            "type": "state",
                            "lifecycle_status": cur_lifecycle,
                            "is_running": cur_running,
                            "pending_count": pending,
                        }
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                        last_lifecycle = cur_lifecycle
                        last_running = cur_running
                    # 主 thread 新消息 — ADR-018 step 3 · 读 keyed by (mission_id, thread_key='main')
                    q = select(Message).where(
                        Message.mission_id == proj.id, Message.thread_key == "main"
                    )
                    if last_msg_at:
                        q = q.where(Message.created_at > last_msg_at)
                    q = q.order_by(Message.created_at.asc()).limit(20)
                    rows = (await fdb.execute(q)).scalars().all()
                    for m in rows:
                        ev = {
                            "type": "message",
                            "id": str(m.id),
                            "role": m.role,
                            "content": (m.content or "")[:4000],
                            "meta": m.meta,
                            "created_at": m.created_at.isoformat() if m.created_at else None,
                        }
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                        last_msg_at = m.created_at
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[super_stream] poll failed")
                yield b"data: {\"type\":\"error\"}\n\n"
        yield b"data: {\"type\":\"done\"}\n\n"

    # Part 3 · SSE 防缓冲头：daemon 逐 piece publish + relay 逐事件 yield，但 tool call 仍
    # 「一大块蹦出来」的根因之一是 HTTP 层缓冲。显式禁缓存/禁 transform 压缩/禁反代缓冲，
    # 让每个 event 即产即达，前端「执行一个显示一个」。
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─────────────────────────── v6 · Activity tree API ────────────────

class InterveneBody(BaseModel):
    verb: str  # approve / reject / interrupt / inject_hint / rewind_to / force_retry / skip / mark_stuck
    payload: dict | None = None


# V7.4 · activity_intervene + /super/{slug}/activities 端点已删（ADR-007 ActivityTree 退役）


# ─────────────────────────── v5 · Memory viewer / clear / editor ────────────────

class MemoryView(BaseModel):
    mission_id: str
    super_agent_id: str
    project_memory: dict | None = None  # MissionAgentMemory row
    branch_memories: list[dict] = []


class MemoryClearBody(BaseModel):
    reason: str = ""


class MemoryPatchBody(BaseModel):
    memory_md: str
    reason: str


@router.get("/api/super/{slug}/memory", response_model=MemoryView)
async def super_memory_view(slug: str, db: DBSession, _user: CurrentUser) -> MemoryView:
    """v5 · 查看 super 长期记忆 + 各 branch 压缩记忆"""
    from app.models.mission import MissionAgentMemory
    from sqlalchemy import text as _sql_text
    proj, sup = await _resolve_super(db, slug)
    proj_mem = (await db.execute(
        select(MissionAgentMemory).where(MissionAgentMemory.mission_id == proj.id)
    )).scalars().all()
    proj_mem_dict = None
    if proj_mem:
        m = proj_mem[0]
        proj_mem_dict = {
            "id": str(m.id),
            "agent_node_name": m.agent_node_name,
            "memory_md": m.memory_md,
            "fingerprint_count": getattr(m, "fingerprint_count", 0),
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }
    rows = (await db.execute(_sql_text("""
        SELECT tam.id, tam.thread_key, tam.agent_node_name, tam.memory_md,
               tam.compressed_message_count, tam.last_compressed_at
          FROM thread_agent_memories tam
         WHERE tam.mission_id = :pid
         ORDER BY tam.last_compressed_at DESC NULLS LAST
         LIMIT 50
    """), {"pid": str(proj.id)})).mappings().all()
    # ADR-018 step5/M · 记忆按 (mission_id, thread_key) 收口到 thread_agent_memories
    branch_mems = [
        {
            "id": str(r["id"]),
            "thread_key": r["thread_key"],
            "agent_node_name": r["agent_node_name"],
            "memory_md": r["memory_md"],
            "compressed_message_count": r["compressed_message_count"],
            "last_compressed_at": r["last_compressed_at"].isoformat() if r["last_compressed_at"] else None,
        }
        for r in rows
    ]
    return MemoryView(
        mission_id=str(proj.id),
        super_agent_id=str(sup.id),
        project_memory=proj_mem_dict,
        branch_memories=branch_mems,
    )


@router.post("/api/super/{slug}/memory/clear")
async def super_memory_clear(
    slug: str,
    body: MemoryClearBody,
    db: DBSession,
    user: CurrentUser,
) -> dict:
    """v5 · 清空 super 长期记忆（先快照到 revisions 表）。"""
    from app.models.mission import MissionAgentMemory
    from sqlalchemy import text as _sql_text
    proj, _sup = await _resolve_super(db, slug)
    rows = (await db.execute(
        select(MissionAgentMemory).where(MissionAgentMemory.mission_id == proj.id)
    )).scalars().all()
    cleared = 0
    for m in rows:
        # 快照
        if m.memory_md:
            await db.execute(_sql_text("""
                INSERT INTO mission_agent_memory_revisions
                  (memory_id, memory_md, edited_by, reason, is_clear_op)
                VALUES (:mid, :md, :by, :reason, true)
            """), {"mid": str(m.id), "md": m.memory_md,
                   "by": str(user.id), "reason": body.reason or "manual clear"})
        m.memory_md = ""
        m.fingerprint_count = 0
        cleared += 1
    await db.commit()
    return {"ok": True, "cleared_count": cleared}


@router.patch("/api/super/{slug}/memory")
async def super_memory_edit(
    slug: str,
    body: MemoryPatchBody,
    db: DBSession,
    user: CurrentUser,
) -> dict:
    """v5 · 直接编辑 super 长期记忆 memory_md（feature flag 默认关；先快照旧版到 revisions）。"""
    enabled = await _ss.get_bool(db, "memory_edit_enabled", False)
    if not enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "memory_edit_enabled=false（admin 默认关；改 system_settings 后再试）"
        )
    if not body.reason or not body.reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "reason 必填（用于 revisions 审计）")
    from app.models.mission import MissionAgentMemory
    from sqlalchemy import text as _sql_text
    proj, _sup = await _resolve_super(db, slug)
    rows = (await db.execute(
        select(MissionAgentMemory).where(MissionAgentMemory.mission_id == proj.id)
    )).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "无 MissionAgentMemory 行")
    m = rows[0]
    # 快照旧版
    await db.execute(_sql_text("""
        INSERT INTO mission_agent_memory_revisions
          (memory_id, memory_md, edited_by, reason, is_clear_op)
        VALUES (:mid, :md, :by, :reason, false)
    """), {"mid": str(m.id), "md": m.memory_md or "",
           "by": str(user.id), "reason": body.reason.strip()})
    m.memory_md = body.memory_md
    await db.commit()
    return {"ok": True, "new_size_bytes": len(body.memory_md.encode("utf-8"))}


@router.get("/api/super/{slug}/memory/revisions")
async def super_memory_revisions(slug: str, db: DBSession, _user: CurrentUser) -> list[dict]:
    """v5 · 列出该 super 长期记忆的历史版本（最近 30 条）。"""
    from sqlalchemy import text as _sql_text
    proj, _sup = await _resolve_super(db, slug)
    rows = (await db.execute(_sql_text("""
        SELECT r.id, r.memory_id, r.memory_md, r.edited_by, r.edited_at, r.reason, r.is_clear_op
          FROM mission_agent_memory_revisions r
          JOIN mission_agent_memory p ON p.id = r.memory_id
         WHERE p.mission_id = :pid
         ORDER BY r.edited_at DESC
         LIMIT 30
    """), {"pid": str(proj.id)})).mappings().all()
    return [
        {
            "id": str(r["id"]),
            "memory_id": str(r["memory_id"]),
            "memory_md": r["memory_md"],
            "memory_md_preview": (r["memory_md"] or "")[:300],
            "memory_md_size": len((r["memory_md"] or "").encode("utf-8")),
            "edited_by": r["edited_by"],
            "edited_at": r["edited_at"].isoformat() if r["edited_at"] else None,
            "reason": r["reason"],
            "is_clear_op": r["is_clear_op"],
        }
        for r in rows
    ]


@router.post("/api/super/{slug}/memory/revisions/{rev_id}/revert")
async def super_memory_revert(
    slug: str,
    rev_id: uuid.UUID,
    db: DBSession,
    user: CurrentUser,
) -> dict:
    """v5 · 一键 revert 到指定历史版本（同样写一行新 revision 标 reason=revert）。"""
    enabled = await _ss.get_bool(db, "memory_edit_enabled", False)
    if not enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "memory_edit_enabled=false")
    from app.models.mission import MissionAgentMemory
    from sqlalchemy import text as _sql_text
    proj, _sup = await _resolve_super(db, slug)
    rev_row = (await db.execute(_sql_text("""
        SELECT r.id, r.memory_id, r.memory_md
          FROM mission_agent_memory_revisions r
          JOIN mission_agent_memory p ON p.id = r.memory_id
         WHERE r.id = :rid AND p.mission_id = :pid
    """), {"rid": str(rev_id), "pid": str(proj.id)})).mappings().one_or_none()
    if rev_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "revision 不存在或不属此 super")
    m = await db.get(MissionAgentMemory, rev_row["memory_id"])
    # 快照当前
    await db.execute(_sql_text("""
        INSERT INTO mission_agent_memory_revisions
          (memory_id, memory_md, edited_by, reason, is_clear_op)
        VALUES (:mid, :md, :by, :reason, false)
    """), {"mid": str(m.id), "md": m.memory_md or "",
           "by": str(user.id), "reason": f"revert to {rev_id}"})
    m.memory_md = rev_row["memory_md"]
    await db.commit()
    return {"ok": True, "reverted_to": str(rev_id)}


@router.get("/api/super/{slug}/work-log")
async def super_work_log(
    slug: str,
    db: DBSession,
    user: CurrentUser,
    limit: int = 100,
) -> dict:
    """ADR-009 G5 · Builder（或任意 super）每 mission 的结构化工作记录。

    返回 build_super/build_worker/install_skill/resume 等 mutation 的审计行：
    建/升了什么、影响了哪些 super、结果。
    """
    from app.models.builder_governance import BuilderWorkLog
    proj, _sup = await _resolve_super(db, slug)
    stmt = (
        select(BuilderWorkLog)
        .where(BuilderWorkLog.mission_id == proj.id)
        .order_by(BuilderWorkLog.created_at.desc())
        .limit(min(limit, 500))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "ok": True,
        "mission_id": str(proj.id),
        "items": [
            {
                "id": str(r.id),
                "session_id": str(r.session_id),
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "affected_supers": r.affected_supers or [],
                "result": r.result,
                "summary": r.summary,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
