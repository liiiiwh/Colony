"""R4-5 · tick_lifecycle · super tick 的 in-memory 生命周期（registry + cancel 信号）。

从 super_inbox 拆出：只管「哪个 super 正在跑 tick」「怎么 cooperative cancel」。
不碰 DB（持久化队列在 pending_queue.py）。

- _RUNNING_TICKS:  mission_id → 正在跑的 asyncio.Task
- _CANCEL_EVENTS:  mission_id → asyncio.Event（喂给 BuiltinToolContext.cancel_event）
- _CANCEL_HISTORY: mission_id → 最近 cancel 时间（F2 burst window 监控）
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

# 全进程 in-memory 注册表（uvicorn --reload 重启会清空，但 DB 队列 + tick 已有兜底）
_RUNNING_TICKS: dict[uuid.UUID, asyncio.Task] = {}
_CANCEL_EVENTS: dict[uuid.UUID, asyncio.Event] = {}
_CANCEL_HISTORY: dict[uuid.UUID, deque[float]] = defaultdict(lambda: deque(maxlen=20))


def get_cancel_event(mission_id: uuid.UUID) -> asyncio.Event:
    """返回 super 的 cancel_event（不存在则创建）。"""
    ev = _CANCEL_EVENTS.get(mission_id)
    if ev is None:
        ev = asyncio.Event()
        _CANCEL_EVENTS[mission_id] = ev
    return ev


def register_task(mission_id: uuid.UUID, task: asyncio.Task) -> None:
    """run_once 入口注册当前 tick task；finish 时调 unregister_task。"""
    _RUNNING_TICKS[mission_id] = task
    ev = get_cancel_event(mission_id)
    if ev.is_set():
        ev.clear()


def unregister_task(mission_id: uuid.UUID, task: asyncio.Task | None = None) -> None:
    """注销注册。传 task 时只在「注册的确实是它」才 pop——防 skip/respawn task 的
    finally 弹掉真 tick 的注册（2026-07-03 事故帮凶之一）。"""
    if task is not None and _RUNNING_TICKS.get(mission_id) is not task:
        return
    _RUNNING_TICKS.pop(mission_id, None)


def signal_cancel(mission_id: uuid.UUID) -> dict:
    """只 set 协作取消信号，不 await/不 cancel task —— **tick 内部人工门专用**。

    2026-07-03 事故：LangGraph 把 tool 跑在子 task 里，cancel_current_tick 的
    `task is current_task()` 自检失效 → tool wait_for(shield(tick)) 等 tick、tick 等
    tool 返回 = 死锁；10s 超时强 cancel 又沿 ~千层嵌套 gather 递归爆 RecursionError →
    tick 僵尸永久持锁。tick 内只能发信号，让 E2 checkpoint（on_tool_end）break 收尾。"""
    _CANCEL_HISTORY[mission_id].append(time.time())
    get_cancel_event(mission_id).set()
    return {"ok": True, "stage": "signal"}


def is_running(mission_id: uuid.UUID) -> bool:
    t = _RUNNING_TICKS.get(mission_id)
    return t is not None and not t.done()


async def cancel_current_tick(
    mission_id: uuid.UUID,
    *,
    timeout_seconds: float = 10.0,
) -> dict:
    """V4 · cancel super 当前 tick（cooperative → 超时强 cancel）。"""
    task = _RUNNING_TICKS.get(mission_id)
    if task is None or task.done():
        return {"ok": True, "skipped": "no_running_tick"}
    _CANCEL_HISTORY[mission_id].append(time.time())
    ev = get_cancel_event(mission_id)
    ev.set()
    # ADR-028 D4 · H1 · 自停场景：人工门 skill（request_approval/request_new_capability）在
    # **正跑的 tick 任务自身内**调本函数落卡。此时不能 await 自己完成（会死锁/被强 cancel）；
    # 只 set cancel_event 即可——E2 checkpoint 会在下一个 tool 结果后、LLM call 前协作式停下。
    if task is asyncio.current_task():
        return {"ok": True, "stage": "self_signal", "task_done": False}
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        return {"ok": True, "stage": "cooperative", "task_done": True}
    except TimeoutError:
        # ⚠️ LangGraph astream_events 内部是 ~千层嵌套 gather；task.cancel() 会沿链
        # 递归（_GatheringFuture.cancel → child.cancel → …）爆 RecursionError（2026-07-03
        # 真实事故）。兜住：结构化返回，锁交给 run_once 的 TTL 自愈。
        try:
            task.cancel()
        except RecursionError:
            logger.warning(
                "[tick_lifecycle] force cancel 撞深 gather 链 RecursionError mission=%s "
                "→ 放弃硬 cancel（tick 锁由 TTL 自愈回收）", mission_id,
            )
            return {"ok": False, "stage": "forced_failed_recursion", "task_done": task.done()}
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        return {"ok": True, "stage": "forced", "task_done": task.done()}
    except asyncio.CancelledError:
        return {"ok": True, "stage": "already_cancelled"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("[tick_lifecycle] cancel failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def cancel_burst_count(mission_id: uuid.UUID, window_seconds: float = 5.0) -> int:
    """F2/R-F2 · super 最近 window_seconds 内的 cancel 次数。"""
    now = time.time()
    history = _CANCEL_HISTORY[mission_id]
    return sum(1 for t in history if now - t <= window_seconds)
