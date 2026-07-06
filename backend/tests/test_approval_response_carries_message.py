"""审批回执必须携带原方案正文。

真出过（v4 e2e，2026-07-02）：改造方案卡确认后，回执只写「审批说明：（同 pending 内容）」
占位符；原卡片正文早已滑出 LLM 上下文（隔 30 分钟）→ Builder 只能靠标题「发布链路改造」
脑补，幻觉出一个不存在于方案里的「发布审核人 Approval Gate」worker 开始建。
确认动作与被确认内容必须在同一条消息里自包含。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.approvals import PendingApproval
from app.models.message import Message
from app.models.mission import Mission
from app.models.user import User
from app.services.pending_approval_service import _write_response_message


@pytest.mark.asyncio
async def test_response_message_embeds_original_plan(db_session):
    u = User(username="adm-appr", email="adm-appr@x.com", hashed_password="x", role="admin")
    sup = Agent(name="sup-appr", kind="super", category="custom")
    db_session.add_all([u, sup])
    await db_session.flush()
    m = Mission(name="m", slug="m-appr-x", supervisor_agent_id=sup.id,
                created_by=u.id, status="active")
    db_session.add(m)
    await db_session.flush()
    plan_body = "## 改造方案\n1. 安装本地 xiaohongshu-mcp 服务\n2. 注册并绑定到发布 worker"
    row = PendingApproval(
        request_id="req-embed-1", mission_id=m.id, thread_key="main",
        title="确认发布链路改造方案？", message=plan_body,
        options=["确认，开始改造", "先不改了"],
    )
    db_session.add(row)
    await db_session.commit()

    await _write_response_message(db_session, row, "确认，开始改造", "inline-card")

    msg = (await db_session.execute(
        select(Message).where(Message.mission_id == m.id).order_by(Message.created_at.desc())
    )).scalars().first()
    assert "安装本地 xiaohongshu-mcp 服务" in msg.content, "回执必须自包含被确认的方案正文"
    assert "（同 pending 内容）" not in msg.content
