"""v3 · Super-only dispatch skills。

R5 super 自由调度全平台 worker；R6 并行调用；R16 super-worker 持久 thread；R22 worker 反问;
R8 capability 缺失自动申请；V17 invoke_worker 嵌套 ≤2 层；V53 per-thread asyncio.Lock。

4 个工具：
- invoke_worker         同步调一个 worker（按 capability:slug 或 agent_id），返回结构化 envelope
- invoke_workers_parallel  asyncio.gather 多 worker 并发；同 worker 串行（V53）
- list_workers          目录查询；分页强制（V46）
- request_new_capability  缺能力时升级到 Builder（自动开 super-initiated session + 把 super 标 paused）

3-tier 上下文压缩：thread > super > 平台默认（system_settings）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from langchain_core.tools import StructuredTool
from sqlalchemy import select

from app.skills_builtin.context import BuiltinToolContext

logger = logging.getLogger(__name__)

# 兜底默认；运行时优先读 system_settings（admin 可调）
INVOKE_TIMEOUT_SEC_DEFAULT = 600
MAX_NESTING_DEPTH_DEFAULT = 2  # V17 (修复 R3-6: 原为 MAX_NESTING_DEPTH_DEFAULT_DEFAULT typo → line 242 NameError)
MAX_CLARIFICATION_ROUNDS_DEFAULT = 3  # V37
TOOL_MESSAGE_MAX_KB_DEFAULT = 50  # V38
CAPABILITY_QUOTA_PER_SUPER_DEFAULT = 3  # V16
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

# ── per-(super, worker) asyncio.Lock 注册表（V53）──
# R3-2 · registry 已搬到 app/domain/dispatch/invocation_context._THREAD_LOCKS；
# 这里 re-export 同一 dict 引用，保证 InvocationContext 与 legacy 路径共享同一把锁。
from app.domain.dispatch.invocation_context import (
    _THREAD_LOCKS,  # noqa: F401 — 同一引用，跨模块共享
    _get_thread_lock as _ic_get_thread_lock,
)
# v4.1 · per-(super, worker, action) Lock — 只在 action_spec.parallel_safe=false 时启用
_ACTION_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}


def _get_thread_lock(super_id: str, worker_id: str) -> asyncio.Lock:
    """legacy per-thread Lock；委托给 InvocationContext 的同一 registry。"""
    return _ic_get_thread_lock(super_id, worker_id)


def resolve_action_spec(cap_contract: dict, action: str) -> tuple[dict | None, str | None]:
    """action → (spec, None) 或 (None, 错误信息)。

    `execute`（goal 自然语言）是**不可废除的万能兜底**（2026-07-05 xhs_publisher 事故：
    write_contract 把合同收窄成单 publish 并 deprecate execute → super 对 worker 绑定
    MCP 的登录/取码/搜索/评论等方法永远没有派发通道）。worker LLM 拿到 goal 后自选
    工具（含全部绑定 MCP 方法）。审批语义防绕过：任一 advertised action 要审批 →
    execute 兜底同样要（approval_judge 自动裁决，安全 goal 秒批，不加人工负担）。
    """
    advertises = {
        a.get("action"): a
        for a in (cap_contract.get("advertises") or [])
        if isinstance(a, dict)
    }
    spec = advertises.get(action)
    if spec is not None:
        return spec, None
    if action == "execute":
        any_approval = any(bool(a.get("requires_approval")) for a in advertises.values())
        side_effects = sorted({
            s for a in advertises.values() for s in (a.get("side_effects") or [])
        })
        return {
            "action": "execute",
            "input_schema": {"goal": "string", "params": "object?"},
            "output_schema": {"result": "object"},
            "requires_approval": any_approval,
            "side_effects": side_effects,
            "synthetic_fallback": True,
        }, None
    return None, (
        f"不支持 action={action!r}。可用：{list(advertises.keys())[:10]}；"
        "或用万能兜底 action='execute'（goal 写自然语言，worker 自选工具，"
        "含其绑定 MCP 的全部方法）"
    )


def _get_action_lock(super_id: str, worker_id: str, action: str) -> asyncio.Lock:
    """v4.1 · per-(super, worker, action) Lock。仅当 capability_contract.advertises[*].parallel_safe=false 时启用。

    举例：
      - xhs_ops.publish_note: parallel_safe=false → 同账号串行（避免重复发帖）
      - xhs_ops.search_posts: parallel_safe=true  → 多关键词可并发
    """
    key = (str(super_id), str(worker_id), str(action))
    lock = _ACTION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ACTION_LOCKS[key] = lock
    return lock


async def _v38_guard(
    ctx: BuiltinToolContext,
    content: str,
    meta: dict,
    max_kb: int,
    prefix: str = "thread",
) -> tuple[str, dict]:
    """V38 · 单条 thread 消息内容 > max_kb 时自动转 S3 + 替换为 URL。

    不抛错：上传失败 fallback 到 truncate 字符截断（前 max_kb*1024 字符）。
    返回新的 (content, meta)。meta 上会加 'v38_offloaded': True 标记。
    """
    if not content:
        return content, meta
    size = len(content.encode("utf-8"))
    cap = max(5, int(max_kb)) * 1024
    if size <= cap:
        return content, meta
    try:
        from app.services.storage_service import get_storage
        import hashlib
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        key = f"colony/thread-offload/{prefix}/{digest}.txt"
        store = get_storage()
        await store.upload(key, content.encode("utf-8"), content_type="text/plain; charset=utf-8")
        url = store.public_url(key) if hasattr(store, "public_url") else key
        snippet = content[: min(2000, cap // 4)]
        new_content = (
            f"⚠️ V38: 内容 {size} bytes > {cap} bytes 上限，已转 S3。\n\n"
            f"URL: {url}\n\n"
            f"--- 前 {len(snippet)} 字符预览 ---\n{snippet}"
        )
        new_meta = dict(meta or {})
        new_meta["v38_offloaded"] = True
        new_meta["v38_url"] = url
        new_meta["v38_orig_bytes"] = size
        return new_content, new_meta
    except Exception:
        logger.exception("[V38] S3 offload 失败，fallback 截断")
        # fallback：硬截断 + 标记
        new_meta = dict(meta or {})
        new_meta["v38_truncated"] = True
        new_meta["v38_orig_bytes"] = size
        return content[:cap] + f"\n\n... [V38 截断；原始 {size} bytes]", new_meta


# ── helper: resolve worker by "capability:slug" 或 agent_id ──
async def _resolve_worker(db, ref: str):
    """返回 (agent_row, error_msg)。"""
    from app.models.agent import Agent

    ref = (ref or "").strip()
    if not ref:
        return None, "❌ worker 引用为空"
    if ref.startswith("capability:"):
        cap = ref.split(":", 1)[1].strip()
        row = (
            await db.execute(
                select(Agent).where(
                    Agent.kind == "worker",
                    Agent.capability == cap,
                    Agent.is_enabled.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None, f"capability:{cap} 平台无可用 worker；考虑调 request_new_capability"
        return row, None
    # else: 当作 agent_id UUID
    try:
        uid = uuid.UUID(ref)
    except (ValueError, TypeError):
        return None, f"❌ 无法解析 worker 引用：{ref}"
    row = await db.get(Agent, uid)
    if row is None or row.kind != "worker":
        return None, f"❌ agent {ref} 不存在或不是 worker"
    return row, None


# ADR-018 mission-only · 已删 _get_or_create_super_worker_thread / _ensure_super_session：
# super 主流 = (mission_id, 'main')，super-worker thread = (mission_id, InvocationContext.thread_id)，
# 都是纯 (mission_id, thread_key)，不再 find-or-create session/session_branch 行。


# ── invoke_worker 主路径 ──
async def _invoke_worker_inner(
    ctx: BuiltinToolContext,
    worker_ref: str,
    action: str,
    params: dict | None,
    approval_ticket: str | None = None,
) -> dict:
    """实际执行单次 invoke_worker；返回 envelope dict。"""
    from app.core import system_settings as _ss
    from app.services import agent_service, compression_service, messaging_service

    if ctx.db_factory is None or ctx.mission_id is None:
        return {"ok": False, "status": "failed", "error_msg": "缺 db_factory / mission_id"}
    # 读 admin 可调的运行配置
    async with ctx.db_factory() as _db_cfg:
        max_nesting = await _ss.get_int(_db_cfg, "invoke_worker.max_nesting_depth", MAX_NESTING_DEPTH_DEFAULT)
        invoke_timeout = await _ss.get_int(_db_cfg, "invoke_worker.timeout_seconds", INVOKE_TIMEOUT_SEC_DEFAULT)
        max_clarification_rounds = await _ss.get_int(_db_cfg, "worker.max_clarification_rounds", MAX_CLARIFICATION_ROUNDS_DEFAULT)
        tool_msg_max_kb = await _ss.get_int(_db_cfg, "worker.tool_message_max_kb", TOOL_MESSAGE_MAX_KB_DEFAULT)

    # v6.M R2-1 · 前置校验走纯函数 (V17/V37/super_id)
    from app.domain.dispatch.precheck import precheck_invocation
    call_stack = list((ctx.extra or {}).get("call_stack") or [])
    clarification_round = int((ctx.extra or {}).get("clarification_round") or 0)
    super_id = (ctx.extra or {}).get("agent_id")
    _pc = precheck_invocation(
        call_stack=call_stack,
        clarification_round=clarification_round,
        super_id=super_id,
        max_nesting=max_nesting,
        max_clarification_rounds=max_clarification_rounds,
    )
    if not _pc.ok:
        return _pc.to_envelope()

    log_row_id: uuid.UUID | None = None
    started_at = time.time()
    call_id = str(uuid.uuid4())  # v5 · 给每次 invoke 一个稳定 id，让 UI 关联 4 个事件
    # ADR-018 step 3b · event_bus channel = the Mission (mission_id); worker-dispatch events stream
    # into the same mission channel the user's SSE watches.
    bus_channel: uuid.UUID | None = ctx.mission_id if isinstance(ctx.mission_id, uuid.UUID) else None
    from app.services.event_bus import bus as _bus

    async with ctx.db_factory() as db:
        # 1. resolve worker
        worker, err = await _resolve_worker(db, worker_ref)
        if err:
            return {"ok": False, "status": "failed", "error_msg": err}
        worker_id = worker.id
        # v5 publish #1 · resolve
        if bus_channel:
            await _bus.publish(bus_channel, {
                "type": "worker_resolve", "call_id": call_id,
                "worker_id": str(worker_id), "capability": worker.capability,
                "action": action, "super_id": str(super_id),
            })

        # 2. capability_contract 校验 action + requires_approval（execute 万能兜底见
        #    resolve_action_spec —— 2026-07-05 xhs_publisher 事故：合同被收窄后 super
        #    永远没通道用 worker 绑定 MCP 的其余方法）
        cap_contract = (worker.extra_config or {}).get("capability_contract") or {}
        action_spec, _act_err = resolve_action_spec(cap_contract, action)
        if action_spec is None:
            return {
                "ok": False,
                "status": "failed",
                "error_msg": f"❌ worker {worker.capability or worker.name} {_act_err}",
            }
        # V27/V33 approval gate
        if action_spec.get("requires_approval") and not approval_ticket:
            return {
                "ok": False,
                "status": "needs_approval",
                "error_msg": (
                    f"⚠️ action {action!r} requires_approval；super 必须先 request_approval "
                    f"拿到 ticket 后用 approval_ticket=<ticket> 重 invoke"
                ),
                "action_spec": action_spec,
            }

        # 3-4. ADR-018 mission-only · super-worker thread = (mission_id, thread_key)，纯字符串无 session/branch 行
        from app.domain.dispatch.invocation_context import InvocationContext
        _ic = InvocationContext(
            db, super_session_id=ctx.mission_id, super_id=super_id, worker_id=worker_id
        )
        _worker_thread_key = _ic.thread_id  # f"super-{sid8}-worker-{wid8}"

        # 5. 写 worker_invocation_log start
        try:
            from app.models.agent import Agent  # noqa: F401
            ins_row = await db.execute(
                __import__("sqlalchemy").text("""
                    INSERT INTO worker_invocation_log
                      (worker_agent_id, super_agent_id, super_mission_id,
                       action, started_at, status)
                    VALUES (:wid, :sid, :pid, :act, now(), 'started')
                    RETURNING id
                """),
                {
                    "wid": str(worker_id), "sid": str(super_id),
                    "pid": str(ctx.mission_id),
                    "act": action,
                },
            )
            log_row_id = ins_row.scalar()
            await db.commit()
        except Exception:
            logger.exception("[invoke_worker] wil insert 失败（不阻塞）")

        # 6. 写 super 触发消息到 thread（user role 告诉 worker 要干嘛）
        params_blob = json.dumps(params or {}, ensure_ascii=False)
        instruction = (
            f"[super dispatch] action={action}\n"
            f"params:\n```json\n{params_blob[:8000]}\n```\n"
            + (f"approval_ticket: {approval_ticket}\n" if approval_ticket else "")
        )
        # V38 size guard
        instruction_meta: dict = {"type": "super_dispatch", "action": action, "worker_id": str(worker_id)}
        instruction, instruction_meta = await _v38_guard(
            ctx, instruction, instruction_meta, tool_msg_max_kb, prefix=f"super_dispatch/{action}"
        )
        await messaging_service.append_message(
            db, ctx.mission_id, _worker_thread_key,
            role="user", content=instruction,
            meta=instruction_meta,
        )

        # 7. build worker executor + ctx
        worker_ctx = BuiltinToolContext(
            thread_key=_worker_thread_key,
            agent_node_name="worker_conversation",
            mission_id=ctx.mission_id,
            event_queue=ctx.event_queue,
            db_factory=ctx.db_factory,
            memory_scope="branch",
            extra={
                "agent_id": str(worker_id),
                "acting_user_id": (ctx.extra or {}).get("acting_user_id"),
                "call_stack": call_stack + [str(super_id)],
                "invoked_by_super": str(super_id),
            },
            produces_deliverable=False,
        )
        # ADR-020 · super↔worker 线程超阈值时先压缩旧消息（node='worker_conversation' 与
        # InvocationContext.load_history 读回键一致）。best-effort。
        try:
            await compression_service.maybe_compress_context(
                db, ctx.mission_id, _worker_thread_key, "worker_conversation"
            )
        except Exception:
            logger.warning("[invoke_worker] maybe_compress_context 失败（不阻塞）", exc_info=True)
        try:
            worker_exec = await agent_service.build_agent_executor(db, worker, ctx=worker_ctx)
        except Exception as exc:
            logger.exception("[invoke_worker] build executor 失败")
            return {"ok": False, "status": "failed", "error_msg": f"❌ 构建 worker executor 失败：{exc}"}

    # v4 · cancel check：worker LLM ainvoke 前后都验 cancel_event
    cancel_ev = getattr(ctx, "cancel_event", None)
    if cancel_ev is not None and cancel_ev.is_set():
        envelope = {"ok": False, "status": "cancelled", "error_msg": "用户对话触发 cancel；放弃本次 invoke_worker"}
        await __finalize_log(ctx.db_factory, log_row_id, envelope, started_at)
        if bus_channel:
            await _bus.publish(bus_channel, {"type": "worker_done", "call_id": call_id,
                "worker_id": str(worker_id), "action": action, "status": "cancelled",
                "duration_ms": int((time.time() - started_at) * 1000)})
        return envelope

    # 8. 运行 worker
    #    v4.2 · 平台不再硬锁；并发 / 串行完全由 super LLM 基于任务 + capability_contract
    #          .advertises[*].concurrency_hint（描述性字符串）自己判断。
    #    底层 race（同账号发帖触发风控等）由 worker / MCP 自行处理（重试 / 限流 / 返回错误）。
    #    super 拿到错误后自己决定是 retry 还是改 sequential 调度。
    from contextlib import nullcontext
    try:
        async with nullcontext():
            # 加载 thread 历史 messages（让 worker 看见上次对话）
            async with ctx.db_factory() as db2:
                prior_msgs = await __load_thread_messages(db2, ctx.mission_id, _worker_thread_key, exclude_latest=True)
            # v5 publish #2 · start（lock 获取后；当前 nullcontext 实际无锁，此处即"准备 invoke"）
            if bus_channel:
                await _bus.publish(bus_channel, {
                    "type": "worker_start", "call_id": call_id,
                    "worker_id": str(worker_id), "capability": worker.capability, "action": action,
                    "prior_msg_count": len(prior_msgs),
                })
            # 构造 input messages：历史 + 本次 instruction
            from langchain_core.messages import HumanMessage
            input_msgs = prior_msgs + [HumanMessage(content=instruction)]
            try:
                # v5 publish #3 · LLM invoking
                if bus_channel:
                    await _bus.publish(bus_channel, {
                        "type": "worker_llm_invoke", "call_id": call_id,
                        "worker_id": str(worker_id), "action": action,
                    })
                # v4 · 监听 cancel_event 与 ainvoke 并发；任一触发即返回
                ainvoke_task = asyncio.create_task(worker_exec.ainvoke({"messages": input_msgs}))
                cancel_task = None
                if cancel_ev is not None:
                    cancel_task = asyncio.create_task(cancel_ev.wait())
                tasks_to_wait = [ainvoke_task] + ([cancel_task] if cancel_task else [])
                done, pending = await asyncio.wait(
                    tasks_to_wait, timeout=invoke_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task is not None and cancel_task in done and ainvoke_task not in done:
                    # cancel 先触发 → 取消 worker
                    ainvoke_task.cancel()
                    with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                        await ainvoke_task
                    envelope = {"ok": False, "status": "cancelled",
                                "error_msg": "用户对话触发 cancel；worker 中断"}
                    await __finalize_log(ctx.db_factory, log_row_id, envelope, started_at)
                    if bus_channel:
                        await _bus.publish(bus_channel, {"type": "worker_done", "call_id": call_id,
                            "worker_id": str(worker_id), "action": action, "status": "cancelled",
                            "duration_ms": int((time.time() - started_at) * 1000)})
                    return envelope
                if cancel_task is not None and not cancel_task.done():
                    cancel_task.cancel()
                if not done:
                    raise TimeoutError("worker timeout")
                result = ainvoke_task.result()
            except TimeoutError:
                envelope = {"ok": False, "status": "timeout", "error_msg": f"worker 执行超时 {invoke_timeout}s"}
                await __finalize_log(ctx.db_factory, log_row_id, envelope, started_at)
                if bus_channel:
                    await _bus.publish(bus_channel, {"type": "worker_done", "call_id": call_id,
                        "worker_id": str(worker_id), "action": action, "status": "timeout",
                        "duration_ms": int((time.time() - started_at) * 1000)})
                return envelope
            except Exception as exc:
                logger.exception("[invoke_worker] ainvoke 异常")
                envelope = {"ok": False, "status": "failed", "error_msg": str(exc)[:500]}
                await __finalize_log(ctx.db_factory, log_row_id, envelope, started_at)
                if bus_channel:
                    await _bus.publish(bus_channel, {"type": "worker_done", "call_id": call_id,
                        "worker_id": str(worker_id), "action": action, "status": "failed",
                        "error_msg": str(exc)[:300],
                        "duration_ms": int((time.time() - started_at) * 1000)})
                return envelope
    except Exception as exc:
        envelope = {"ok": False, "status": "failed", "error_msg": f"❌ thread lock / run 异常：{exc}"}
        await __finalize_log(ctx.db_factory, log_row_id, envelope, started_at)
        if bus_channel:
            await _bus.publish(bus_channel, {"type": "worker_done", "call_id": call_id,
                "worker_id": str(worker_id), "action": action, "status": "failed",
                "error_msg": str(exc)[:300],
                "duration_ms": int((time.time() - started_at) * 1000)})
        return envelope

    # 9. 解析 worker 最后一条 AI message + 它的 tool_call return_result
    msgs = result.get("messages") if isinstance(result, dict) else []
    # token 用量：本次 invoke 全部消息加总（修 worker_invocation_log.tokens 恒空 → 观察页 token 恒 0）
    from app.domain.dispatch.usage import sum_message_usage
    _tok_in, _tok_out = sum_message_usage(msgs)
    envelope = __extract_return_result_envelope(msgs)
    if envelope is None:
        # worker 没调 return_result；用最后 AI message text 当 fallback
        last_text = ""
        for m in reversed(msgs):
            role = getattr(m, "type", None) or getattr(m, "role", None)
            if role in ("ai", "assistant"):
                content = getattr(m, "content", "") or ""
                if isinstance(content, str) and content.strip():
                    last_text = content.strip()
                    break
        envelope = {
            "ok": True,
            "status": "completed",
            "text": last_text or "（worker 无文本输出）",
            "warning": "worker 未调 return_result；fallback 取最后 AIMessage",
        }

    envelope.setdefault("worker_id", str(worker_id))
    envelope.setdefault("worker_capability", worker.capability)
    envelope.setdefault("action", action)
    envelope.setdefault("ts", time.time())

    # 9.5 · 把 worker 本次的**中间过程**（thinking / tool-call / tool-result）落到 worker 线程，
    # 让用户点进 super↔worker 线程能看到「worker 到底想了什么、调了哪些工具、结果如何」，
    # 而不只是最终 envelope（否则调试/审计如本轮 run_shell 报错都看不见）。用与 daemon 流式
    # 一致的 meta 形状（event_type=tool-input-available/tool-output-available + raw），前端
    # missionTimeline 直接重建工具卡。非阻塞。（不改上面的 ainvoke 热路径，只事后落 msgs 轨迹。）
    try:
        await __persist_worker_trace(
            ctx.db_factory, ctx.mission_id, _worker_thread_key,
            msgs, len(input_msgs), str(worker_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[invoke_worker] persist worker trace failed（不阻塞）")

    # 10. 写 worker 输出回 thread（assistant role）便于下次 invoke 看到 (V38 size guard)
    try:
        async with ctx.db_factory() as db3:
            tail_content = json.dumps(envelope, ensure_ascii=False)
            tail_meta = {"type": "worker_return", "worker_id": str(worker_id), "action": action, "status": envelope.get("status")}
            tail_content, tail_meta = await _v38_guard(
                ctx, tail_content, tail_meta, tool_msg_max_kb, prefix=f"worker_return/{worker.capability or worker_id}"
            )
            await __import__("app.services.messaging_service", fromlist=["append_message"]).append_message(
                db3, ctx.mission_id, _worker_thread_key,
                role="assistant", content=tail_content,
                meta=tail_meta,
            )
    except Exception:
        logger.exception("[invoke_worker] write thread tail failed（不阻塞）")

    await __finalize_log(ctx.db_factory, log_row_id, envelope, started_at,
                         tokens_in=_tok_in, tokens_out=_tok_out)

    # v5 publish #4 · done
    if bus_channel:
        await _bus.publish(bus_channel, {
            "type": "worker_done", "call_id": call_id,
            "worker_id": str(worker_id), "capability": worker.capability, "action": action,
            "status": envelope.get("status"),
            "duration_ms": int((time.time() - started_at) * 1000),
            "artifact_url": envelope.get("artifact_url"),
            "error_msg": envelope.get("error_msg"),
        })

    return envelope


async def __persist_worker_trace(db_factory, mission_id, thread_key, msgs, input_len, worker_id):
    """把 worker 本次**新生成**的 thinking / tool-call / tool-result 落到 worker 线程。

    meta 形状对齐 daemon 流式（daemon_sink.persist_stream_piece 的 'trace' 形状：
    event_type + raw={type,toolCallId,toolName,input/output}），前端 missionTimeline 直接按
    toolCallId 配对重建工具卡 → 点进 super↔worker 线程能看到 worker 的完整过程，而非只有最终回复。"""
    if db_factory is None or not msgs:
        return
    from app.services.messaging_service import append_message
    new_msgs = msgs[input_len:] if 0 <= input_len < len(msgs) else msgs
    async with db_factory() as db:
        for m in new_msgs:
            mtype = getattr(m, "type", None) or getattr(m, "role", None)
            if mtype in ("ai", "assistant"):
                content = getattr(m, "content", "") or ""
                if isinstance(content, str) and content.strip():
                    await append_message(
                        db, mission_id, thread_key, role="agent_log",
                        content=content.strip()[:8000],
                        meta={"type": "worker_step", "event_type": "thinking-segment", "worker_id": worker_id},
                    )
                for tc in (getattr(m, "tool_calls", None) or []):
                    _n = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    _a = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
                    _id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    await append_message(
                        db, mission_id, thread_key, role="agent_log",
                        content=f"🔧 {_n}",
                        meta={"type": "worker_step", "event_type": "tool-input-available", "worker_id": worker_id,
                              "raw": {"type": "tool-input-available", "toolCallId": str(_id or ""),
                                      "toolName": _n, "input": _a}},
                    )
            elif mtype == "tool":
                out = getattr(m, "content", "") or ""
                _id = getattr(m, "tool_call_id", None)
                await append_message(
                    db, mission_id, thread_key, role="agent_log",
                    content=str(out)[:4000],
                    meta={"type": "worker_step", "event_type": "tool-output-available", "worker_id": worker_id,
                          "raw": {"type": "tool-output-available", "toolCallId": str(_id or ""),
                                  "output": str(out)[:8000]}},
                )


async def __load_thread_messages(db, mission_id, thread_key: str, exclude_latest: bool = False):
    """R3-2 · 委托给 InvocationContext.load_history（thin wrapper · ADR-018 step5/H thread 键）。"""
    from app.domain.dispatch.invocation_context import InvocationContext
    ic = InvocationContext(
        db, super_session_id=mission_id, super_id=mission_id, worker_id=mission_id
    )
    return await ic.load_history(mission_id, thread_key, exclude_latest=exclude_latest)


def __extract_return_result_envelope(msgs) -> dict | None:
    """R2-1 · 已抽到 app/domain/dispatch/envelope.py，本函数保留作 thin wrapper。"""
    from app.domain.dispatch.envelope import extract_return_result_envelope
    return extract_return_result_envelope(msgs)


async def __finalize_log(
    db_factory, log_id: uuid.UUID | None, envelope: dict, started_at: float,
    tokens_in: int = 0, tokens_out: int = 0,
) -> None:
    if log_id is None or db_factory is None:
        return
    try:
        async with db_factory() as db:
            await db.execute(
                __import__("sqlalchemy").text("""
                    UPDATE worker_invocation_log
                       SET finished_at=now(),
                           duration_ms=:dms,
                           status=:st,
                           error_msg=:err,
                           tokens_in = COALESCE(tokens_in,0) + :ti,
                           tokens_out = COALESCE(tokens_out,0) + :to,
                           artifact_count = artifact_count + CASE WHEN :has_art THEN 1 ELSE 0 END
                     WHERE id=:id
                """),
                {
                    "id": str(log_id),
                    "dms": int((time.time() - started_at) * 1000),
                    "st": envelope.get("status") or "completed",
                    "err": (envelope.get("error_msg") or "")[:500] or None,
                    "ti": int(tokens_in or 0),
                    "to": int(tokens_out or 0),
                    "has_art": bool(envelope.get("artifact_url")),
                },
            )
            await db.commit()
    except Exception:
        logger.exception("[invoke_worker] wil finalize 失败（不阻塞）")


# ── Tool factories ──
def invoke_worker_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _invoke(
        worker: str,
        action: str,
        params: dict | None = None,
        approval_ticket: str | None = None,
    ) -> str:
        envelope = await _invoke_worker_inner(ctx, worker, action, params, approval_ticket)
        return json.dumps(envelope, ensure_ascii=False)
    return StructuredTool.from_function(
        coroutine=_invoke,
        name="invoke_worker",
        description=(
            "（super-only）调度一个平台 worker。返回 JSON envelope。\n"
            "参数：\n"
            "- worker(str)：'capability:xhs_ops' 或具体 agent_id\n"
            "- action(str)：worker capability_contract.advertises 里的 action 名；"
            "或万能兜底 'execute'（goal 写自然语言，worker 自选工具，含其绑定 MCP 的全部方法）\n"
            "- params(dict)：传给 worker 的参数（按 action.input_schema）\n"
            "- approval_ticket(str, optional)：requires_approval=true 的 action 需要先 request_approval 拿到 ticket\n"
            "envelope.status：completed / needs_clarification / failed / timeout / needs_approval"
        ),
    )


def invoke_workers_parallel_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _parallel(calls: list[dict]) -> str:
        """v4.2 · 一次并发调多个 worker action（平台不加锁，全部 asyncio.gather）。

        calls: [{worker, action, params, approval_ticket?}]
        ★ 是否真的应该并发**完全由 super LLM 判断**：
          - 调 list_workers 看每个 action 的 concurrency_hint / side_effects / rate_limit
          - 结合当前任务的依赖图（前者输出 → 后者输入？同账号同时写？）
          - 自己决定是 1 次 invoke_workers_parallel 还是 N 次 sequential invoke_worker
        平台职责：
          - 不强制 Lock；不阻止"危险"并发（即使可能触发风控）
          - worker / MCP 自己处理 race / rate limit / 重试
          - super 看到错误后自己调整策略（改 sequential / 加间隔 / 拒绝）
        """
        if not isinstance(calls, list) or not calls:
            return json.dumps({"ok": False, "error": "calls 必须是非空 list"}, ensure_ascii=False)
        async def _one(idx: int, c: dict) -> tuple[int, dict]:
            env = await _invoke_worker_inner(
                ctx,
                c.get("worker", ""),
                c.get("action", ""),
                c.get("params") or {},
                c.get("approval_ticket"),
            )
            return idx, env
        results = await asyncio.gather(*[_one(i, c) for i, c in enumerate(calls)], return_exceptions=True)
        out = [None] * len(calls)
        for r in results:
            if isinstance(r, Exception):
                continue
            idx, env = r
            out[idx] = env
        return json.dumps({"ok": True, "results": out}, ensure_ascii=False)
    return StructuredTool.from_function(
        coroutine=_parallel,
        name="invoke_workers_parallel",
        description=(
            "（super-only）一次并发调多个 worker action（平台 asyncio.gather，不加锁）。"
            "calls: [{worker, action, params, approval_ticket?}]。"
            "**是否真该并发由 super 自己判断**：list_workers 看 actions 的 concurrency_hint / "
            "side_effects / rate_limit，结合任务依赖图决定。"
            "顺序依赖（output→input）→ 拆 sequential invoke_worker；"
            "无依赖批量 → 一次并发；"
            "高风险并发（同账号多写）→ 自己加间隔 / 改 sequential / request_approval 让 user 决定。"
        ),
    )


def list_workers_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _list(capability: str | None = None, page: int = 1, limit: int = DEFAULT_LIST_LIMIT) -> str:
        from app.models.agent import Agent
        limit = max(1, min(limit, MAX_LIST_LIMIT))
        page = max(1, page)
        offset = (page - 1) * limit
        if ctx.db_factory is None:
            return json.dumps({"ok": False, "error": "缺 db_factory"})
        async with ctx.db_factory() as db:
            # 系统 worker（installer / approval_judge 等）可见性按调用方分级：
            # - 业务 super：不可见（防误派系统件）；
            # - 系统调用方（Builder / 系统 super，is_system=True）：可见——Builder 委派
            #   mcp_installer 前会先 list_workers 验证，目录看不到会误判"installer 不存在"
            #   而放弃委派转弹人工卡（v3 e2e 真出过）。
            caller_is_system = False
            if ctx.agent_id is not None:
                caller = await db.get(Agent, ctx.agent_id)
                caller_is_system = bool(caller is not None and caller.is_system)
            stmt = select(Agent).where(
                Agent.kind == "worker", Agent.is_enabled.is_(True)
            )
            if not caller_is_system:
                stmt = stmt.where(Agent.is_system.is_(False))
            if capability:
                stmt = stmt.where(Agent.capability == capability)
            stmt = stmt.order_by(Agent.capability, Agent.name).limit(limit).offset(offset)
            rows = (await db.execute(stmt)).scalars().all()
        # MCP 能力清单（2026-07-05 xhs_publisher 双盲事故）：worker 绑定 MCP 的工具
        # 让 super 派发时可见（否则 super 只看合同 actions，断言 worker「不会」它
        # 运行时明明会的方法）。清单来自 mcp_inventory TTL 缓存，失败为空不阻塞。
        _mcp_by_agent: dict = {}
        if rows:
            try:
                from app.models.agent import AgentMCPServer as _AMS
                from app.models.skill import MCPServer as _MCPS
                from app.services import mcp_inventory as _inv
                async with ctx.db_factory() as db2:
                    _binds = (await db2.execute(
                        select(_AMS.agent_id, _MCPS)
                        .join(_MCPS, _MCPS.id == _AMS.mcp_server_id)
                        .where(_AMS.agent_id.in_([a.id for a in rows]),
                               _MCPS.is_enabled.is_(True))
                    )).all()
                for _aid, _srv in _binds:
                    _tools = await _inv.get_tools_cached(_srv)
                    if _tools:
                        _mcp_by_agent.setdefault(_aid, {})[_srv.name] = [
                            t["name"] for t in _tools
                        ]
            except Exception:  # noqa: BLE001
                logger.warning("[list_workers] MCP 清单装配失败（跳过）", exc_info=True)

        items = []
        for a in rows:
            cap = (a.extra_config or {}).get("capability_contract") or {}
            # v4.2 · 输出每个 action 的语义 hint（super LLM 用这些自己判断并发/串行/重试）
            #   · concurrency_hint: 字符串描述（如 "同账号高频发帖易触发风控；建议串行 + 间隔 ≥30s"）
            #   · side_effects:    标签数组（如 ["external_write","third_party_api"]）
            #   · requires_approval: 平台审批门（V27）
            #   · idempotent: 重复调同样参数是否安全
            #   · rate_limit: 已知 rate limit 提示
            actions_detail = []
            for x in (cap.get("advertises") or []):
                if not isinstance(x, dict):
                    continue
                d = {
                    "action": x.get("action"),
                    "requires_approval": x.get("requires_approval", False),
                }
                for opt in ("concurrency_hint", "side_effects", "idempotent", "rate_limit"):
                    if opt in x:
                        d[opt] = x[opt]
                actions_detail.append(d)
            item = {
                "agent_id": str(a.id),
                "name": a.name,
                "capability": a.capability,
                "version": cap.get("version"),
                "advertises": [x.get("action") for x in (cap.get("advertises") or [])],
                "actions": actions_detail,
                "description": a.description,
            }
            if a.id in _mcp_by_agent:
                item["mcp_tools"] = _mcp_by_agent[a.id]
            items.append(item)
        return json.dumps({
            "ok": True, "page": page, "limit": limit, "items": items,
            "note": (
                "action 除 advertises 外始终可用万能兜底 'execute'（goal 自然语言，"
                "worker 自选工具，含 mcp_tools 列的全部方法）"
            ),
        }, ensure_ascii=False)
    return StructuredTool.from_function(
        coroutine=_list,
        name="list_workers",
        description=(
            "（super-only）查平台 worker 目录。按需查不要全量缓存。"
            "参数：capability(str?) / page(int=1) / limit(int=50, max 200)"
        ),
    )


def invoke_super_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """v5 占位 · v6 才正式支持跨 super 调度。

    现在调用会抛 NotImplementedError；contract test 锁住"任何 super protocol_md 不可含此字样"，
    防止 LLM 提前自动写出这个用法导致整个 platform 行为不稳定。
    """
    async def _invoke_super_stub(
        super_ref: str,
        action: str = "",
        params: dict | None = None,
    ) -> str:
        raise NotImplementedError(
            "invoke_super 在 v5 不开放：跨 super 调度涉及循环检测 / 公平性 / "
            "memory 边界设计未定；v6 单独 ADR 后再正式启用。请改用 invoke_worker。"
        )
    return StructuredTool.from_function(
        coroutine=_invoke_super_stub,
        name="invoke_super",
        description=(
            "[v5 stub · v6 才会启用] 跨 super 调度占位。"
            "v5 调用必抛 NotImplementedError；请改用 invoke_worker 调度 worker。"
        ),
    )


def request_new_capability_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _request(
        capability: str,
        why: str,
        suggested_actions: list[str] | None = None,
        proposed_input_schema: dict | None = None,
    ) -> str:
        """缺 capability 时升级到 Builder。super 此后被标 paused_waiting_capability。
        实现 = 调既有 mission_escalate_to_builder（category='structural'）+ 设 lifecycle_status。
        V16：检查 pending capability 请求数量是否超 quota（默认 3）。
        """
        from app.core import system_settings as _ss
        from app.skills_builtin.builder.escalation_skills import mission_escalate_to_builder_tool
        from sqlalchemy import text as _sql_text

        if ctx.db_factory is None or ctx.mission_id is None:
            return json.dumps({"ok": False, "error": "缺上下文"})

        # V16 quota check
        async with ctx.db_factory() as db_q:
            quota = await _ss.get_int(db_q, "escalation.capability_quota_per_super", CAPABILITY_QUOTA_PER_SUPER_DEFAULT)
            pending_count = (await db_q.execute(_sql_text("""
                SELECT COUNT(*) FROM mission_escalations
                 WHERE mission_id = :pid
                   AND category = 'structural'
                   AND status IN ('pending', 'delivered')
            """), {"pid": str(ctx.mission_id)})).scalar() or 0
            if pending_count >= quota:
                return json.dumps({
                    "ok": False,
                    "error": (
                        f"❌ V16 quota: 同 super 已有 {pending_count} 个 pending capability 请求 ≥ {quota}; "
                        f"请等 Builder 处理或调整 escalation.capability_quota_per_super"
                    ),
                    "quota": quota,
                    "pending": pending_count,
                }, ensure_ascii=False)
        # 调既有 escalate_to_builder
        esc_tool = mission_escalate_to_builder_tool(ctx)
        evidence = {
            "capability": capability,
            "suggested_actions": suggested_actions or [],
            "proposed_input_schema": proposed_input_schema or {},
        }
        # esc_tool 是 StructuredTool；从内 coroutine 调
        # 它接受位置参数 category, severity, summary, evidence_json(str), proposed_change, worker_agent_id
        res = await esc_tool.coroutine(  # type: ignore[attr-defined]
            category="structural",
            severity="warn",
            summary=f"缺少 capability={capability}: {why[:200]}",
            evidence_json=json.dumps(evidence, ensure_ascii=False),
            proposed_change=f"add capability:{capability} 或升级现有相似 worker",
            worker_agent_id="",
        )
        # v6 · 走 LifecycleService 单一入口（不再裸 UPDATE lifecycle_status）
        try:
            from app.domain.lifecycle_service import LifecycleService
            from app.domain.lifecycle import LifecycleAction
            async with ctx.db_factory() as db:
                await LifecycleService(db).transition(
                    ctx.mission_id,
                    LifecycleAction.PAUSE_FOR_CAPABILITY,
                    reason=f"waiting capability={capability}: {why[:200]}",
                )
        except Exception:
            logger.exception("[request_new_capability] LifecycleService failed (不阻塞)")
        # ADR-028 D4 H1 · 缺能力即人工门 → 硬停当前 tick（cooperative cancel：
        # executor 在下个 tool 边界检查 cancel_event 即停，不靠 LLM 自觉 end turn）。
        _ce = getattr(ctx, "cancel_event", None)
        if _ce is not None:
            _ce.set()
        return json.dumps(
            {"ok": True, "escalation_result": json.loads(res), "super_paused": True},
            ensure_ascii=False,
        )
    return StructuredTool.from_function(
        coroutine=_request,
        name="request_new_capability",
        description=(
            "（super-only）缺 capability 时调用。自动升级到 Builder Chat 并把 super 标 paused_waiting_capability；"
            "scheduler 跳过；Builder 处理完调 resume_super_agent 唤醒。"
        ),
    )


def report_worker_issue_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _report(
        capability: str,
        evidence: str,
        severity: str = "warn",
        pause: bool = True,
    ) -> str:
        """ADR-018 D2 ·（super-only）现有 worker 反复失败/行为异常时上报 Worker Optimization super 求修。

        区别于 request_new_capability（缺能力）：这是「能力在、但 worker 坏了」。worker 跨 super 共享，
        所以优化集中归 **Colony Worker Optimization** super（不挂 Builder）：
        - 走 submit_worker_issue → 证据落到 Worker Optimization mission，由它按保守门控协议处理。
        - pause=True（默认）把 super 标 paused_waiting_capability（reason 前缀 worker_issue:），
          scheduler 跳过；worker-opt 成功修复该 capability 后**按 capability 自动唤醒**（ADR-025，
          不再依赖 Builder 手动 resume_super_agent）。
        参数：capability(出问题的 worker capability) / evidence(失败样本/错误摘要) /
              severity('info'|'warn'|'critical') / pause(是否停工等修)。
        """
        from app.services import worker_health_service

        if ctx.db_factory is None or ctx.mission_id is None:
            return json.dumps({"ok": False, "error": "缺上下文"})

        # 定位 worker_agent_id（用于 fingerprint 去重 + Worker-Opt 定位）
        worker_agent_id = ""
        try:
            from sqlalchemy import text as _sql_text
            async with ctx.db_factory() as db_q:
                row = (await db_q.execute(_sql_text(
                    "SELECT id FROM agents WHERE kind='worker' AND capability=:cap AND is_enabled LIMIT 1"
                ), {"cap": capability})).scalar()
                if row:
                    worker_agent_id = str(row)
        except Exception:  # noqa: BLE001
            pass

        try:
            async with ctx.db_factory() as db_sub:
                delivered = await worker_health_service.submit_worker_issue(
                    db_sub, capability=capability, evidence=evidence,
                    severity=severity if severity in ("info", "warn", "critical") else "warn",
                    worker_agent_id=worker_agent_id,
                )
        except Exception:
            logger.exception("[report_worker_issue] submit_worker_issue failed")
            delivered = False

        paused = False
        if pause:
            try:
                from app.domain.lifecycle_service import LifecycleService
                from app.domain.lifecycle import LifecycleAction
                async with ctx.db_factory() as db:
                    await LifecycleService(db).transition(
                        ctx.mission_id,
                        LifecycleAction.PAUSE_FOR_CAPABILITY,
                        reason=f"worker_issue:{capability}: {evidence[:160]}",
                    )
                paused = True
                # ADR-028 D4 H1 · worker 坏了停工等修 = 人工门 → 硬停当前 tick。
                _ce = getattr(ctx, "cancel_event", None)
                if _ce is not None:
                    _ce.set()
            except Exception:
                logger.exception("[report_worker_issue] LifecycleService pause failed (不阻塞)")

        return json.dumps(
            {"ok": True, "routed_to": "worker_optimization_super",
             "delivered": delivered, "super_paused": paused},
            ensure_ascii=False,
        )

    return StructuredTool.from_function(
        coroutine=_report,
        name="report_worker_issue",
        description=(
            "（super-only）现有 worker 反复失败/行为异常时上报 Colony Worker Optimization super 求修"
            "（区别于 request_new_capability 缺能力；worker 跨 super 共享，优化集中归 Worker-Opt 不挂 Builder）。"
            "默认把 super 标 paused_waiting_capability 停工等修；worker 修好调 resume_super_agent 唤醒。"
            "参数：capability(str) / evidence(str 失败摘要) / severity('info'|'warn'|'critical') / pause(bool=True)。"
        ),
    )


# ─────────────────────────── v6.B · Mismatch redirect (Q7) ───────────────────────────


def list_supers_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """v6.B · super-only · 查平台其它 super 候选（用于 mismatch 重定向 Q7）。"""
    async def _list_supers(keyword: str | None = None, limit: int = 10) -> str:
        from app.domain.builder.list_supers import list_supers
        if ctx.db_factory is None:
            return json.dumps({"ok": False, "error": "no db_factory"}, ensure_ascii=False)
        # exclude self
        self_id: uuid.UUID | None = None
        my_agent_id = (ctx.extra or {}).get("agent_id")
        if my_agent_id:
            try:
                self_id = uuid.UUID(my_agent_id)
            except (TypeError, ValueError):
                pass
        async with ctx.db_factory() as db:
            rows = await list_supers(db, keyword=keyword, exclude_super_id=self_id, limit=limit)
        return json.dumps({"ok": True, "items": rows, "total": len(rows)}, ensure_ascii=False)
    return StructuredTool.from_function(
        coroutine=_list_supers,
        name="list_supers",
        description=(
            "（super-only · Q7 mismatch 重定向用）查平台其它 super 候选。"
            "参数：keyword (可选 — 按 name/description/soul_md 模糊匹配；建议从用户 goal 提关键词)；"
            "limit (默认 10)。返回 [{super_id, name, description, fit_hint}]，自动排除自己。"
        ),
    )


def emit_redirect_suggestion_tool(ctx: BuiltinToolContext) -> StructuredTool:
    """v6.B · super-only · 写一张「mismatch 重定向卡」到 chat 流 + 触发 lifecycle 转 stopped。

    Q7 流程：super 判断 mission 不适合自己 → 调本 tool → 前端 chat 流出现卡片
    (4 个 action: 跳已有 super / 找 Builder / 在此继续 / 取消)；不立即停 mission，等用户选。
    """
    async def _emit(
        reason: str,
        candidates: list[dict],
        original_message: str = "",
    ) -> str:
        from app.services.event_bus import bus as _bus

        if ctx.mission_id is None or ctx.db_factory is None:
            return json.dumps({"ok": False, "error": "missing ctx.mission_id / db_factory"})

        # publish SSE event 让前端 chat 流立即渲染卡片 — ADR-018 step 3b · channel = Mission
        await _bus.publish(ctx.mission_id, {
            "type": "redirect_suggestion",
            "reason": reason,
            "candidates": candidates[:5],
            "original_message": original_message[:500],
        })
        return json.dumps({"ok": True, "candidates_count": len(candidates),
                           "awaiting_user": True}, ensure_ascii=False)
    return StructuredTool.from_function(
        coroutine=_emit,
        name="emit_redirect_suggestion",
        description=(
            "（super-only · Q7 mismatch 重定向用）当判定 mission 与自己能力不匹配时调。"
            "参数：reason (str, 解释为啥不匹配)；candidates (list[{super_id?, name, fit_hint}]，"
            "通常 list_supers + Builder 候选混合)；original_message (用户原话，跳转时带过去)。"
            "效果：chat 流出现 redirect 卡 + activity 标 waiting_user，等用户选择。"
            "**super 在调用本 tool 之后应立即结束本轮 tool 循环**，不要继续 invoke_worker / "
            "request_structured_input；让用户来决定。"
        ),
    )
