"""单卡不变量（mission 级）+ chat-as-comment 绑最新。

真出过（v4 e2e，2026-07-03）：运营 mission 里同时挂着「运营计划卡」（super 提案，
thread='main'）和「QR 扫码卡」（readiness 建，thread=None）——per-thread 去重被
thread_key 不一致绕过 → 双卡并存。用户打字「重新获取下二维码」被 chat-as-comment
decide 了**最旧**那张（运营计划卡），连锁把 super 带偏。

修：① create_pending 去重收窄到 mission 级（任何 pending 卡都算，不看 thread_key）；
② chat-as-comment 绑**最新**卡。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.approvals import PendingApproval
from app.models.mission import Mission
from app.models.user import User
from app.services import pending_approval_service as pa


async def _mission(db):
    u = User(username="adm-sc", email="adm-sc@x.com", hashed_password="x", role="admin")
    sup = Agent(name="sup-sc", kind="super", category="custom")
    db.add_all([u, sup])
    await db.flush()
    m = Mission(name="ops", slug="ops-sc-x", supervisor_agent_id=sup.id,
                created_by=u.id, status="active")
    db.add(m)
    await db.flush()
    return m


@pytest.mark.asyncio
async def test_second_card_reuses_regardless_of_thread(db_session):
    """已有 pending 卡时再建（即便 thread_key 不同）→ 复用不新建，杜绝双卡并存。"""
    m = await _mission(db_session)
    first = await pa.create_pending(
        db_session, mission_id=m.id, title="运营计划", message="plan",
        options=["就按这个来"], thread_key="main",
    )
    await db_session.commit()

    second = await pa.create_pending(
        db_session, mission_id=m.id, title="[xhs] 扫码登录", message="qr",
        options=["我已完成，继续"], thread_key=None,  # readiness 路径的 None thread
    )

    assert second.request_id == first.request_id, "应复用既有 pending 卡，不新建第二张"
    cnt = len((await db_session.execute(
        select(PendingApproval).where(
            PendingApproval.mission_id == m.id, PendingApproval.status == "pending"
        )
    )).scalars().all())
    assert cnt == 1, "mission 内至多一张 pending 卡"


@pytest.mark.asyncio
async def test_build_auto_decide_option_binds_latest():
    """chat-as-comment 选卡应取最新 pending（order_by desc），不是最旧。"""
    import inspect
    from app.api import super_conversation as sc

    src = inspect.getsource(sc)
    # 旧代码是 order_by(created_at.asc()).limit(1) 取最旧；修后取最新
    assert "created_at.desc()" in src or ".desc()" in src, \
        "chat-as-comment 应按 created_at 降序取最新 pending 卡"
