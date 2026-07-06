"""v4 · Super inbox · R4-5 后已拆为两个 cohesive 模块，本文件保留 re-export 兼容。

- tick_lifecycle.py · in-memory tick registry + cancel 信号（register/is_running/cancel_current_tick/...）
- pending_queue.py  · super_pending_messages 持久化队列（enqueue/pop/count）

老 caller 仍可 `from app.services import super_inbox` 后用 super_inbox.X，行为不变。
新代码建议直接 import 对应的 tick_lifecycle / pending_queue。
"""
from __future__ import annotations

# tick 生命周期（in-memory）
from app.services.tick_lifecycle import (  # noqa: F401
    _RUNNING_TICKS,
    _CANCEL_EVENTS,
    _CANCEL_HISTORY,
    get_cancel_event,
    register_task,
    unregister_task,
    is_running,
    cancel_current_tick,
    signal_cancel,
    cancel_burst_count,
)

# pending 消息队列（DB 持久化）
from app.services.pending_queue import (  # noqa: F401
    enqueue_user_message,
    pop_pending_messages,
    count_pending,
)
