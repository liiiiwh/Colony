"""审批续跑 tick 不因 no-op 陷入 busy-loop。

真出过（mission-987bb9，2026-07-03）：用户审批后续跑 tick 起不来——上一轮发审批卡的
tick 被 H1 cancel 但没释放并发锁；审批触发的新 tick 走 run_once 见"已有 tick 在跑"→
no-op skipped，但 _trigger_tick_async 的 finally auto-drain 仍 respawn（pending>0 +
lifecycle running）→ 零延迟自触发 → 83 秒内空转 102432 次，用户干等一分多钟才响应。

修：run_once 返回 skipped（no-op）时，auto-drain respawn 前**退避** `_SKIP_RESPAWN_BACKOFF_SEC`
（daemon 循环 tick 结束不走本 auto-drain，故续跑必须靠本路径轮询到锁释放；不能直接不
respawn 否则审批后续跑丢失）。退避把零延迟热循环压到 ~1/s：既保证续跑不丢，又不烧 CPU。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


@pytest.mark.asyncio
async def test_skip_respawn_is_throttled_not_busyloop(monkeypatch):
    from app.api import super_conversation as sc

    # 把退避压到极小，便于测试快速观测节流效果（真实是 1s）
    monkeypatch.setattr(sc, "_SKIP_RESPAWN_BACKOFF_SEC", 0.05)

    mid = uuid.uuid4()
    run_once_calls = {"n": 0}

    async def _fake_run_once(db, mission_id, payload=None):
        run_once_calls["n"] += 1
        return {"ok": True, "skipped": "tick_in_progress"}

    from app.services import mission_daemon as md
    monkeypatch.setattr(md, "run_once", _fake_run_once)

    async def _cnt(db, mission_id):
        return 3
    monkeypatch.setattr(sc.super_inbox, "count_pending", _cnt, raising=False)
    monkeypatch.setattr(sc.super_inbox, "register_task", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(sc.super_inbox, "unregister_task", lambda *a, **k: None, raising=False)

    class _Mission:
        lifecycle_status = "running"

    class _Sess:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return _Mission()
    monkeypatch.setattr(sc, "AsyncSessionLocal", lambda: _Sess())

    await sc._trigger_tick_async(mid)
    # 跑 0.3s：若零延迟 busy-loop 会飙到成百上千次；节流到 20ms 退避 → 个位数
    await asyncio.sleep(0.3)

    assert run_once_calls["n"] >= 2, "skip 后应退避轮询续跑（保证不丢），不是完全不 respawn"
    assert run_once_calls["n"] < 30, \
        f"退避应把热循环节流到 ~1/backoff；busy-loop 会成百上千次（实际 {run_once_calls['n']}）"
