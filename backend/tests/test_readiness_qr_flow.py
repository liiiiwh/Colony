"""QR 登录 readiness 流：真实块形抽图 / 错误不吞 / 完成按钮复验恢复。

真出过（2026-07-03 grill）：
- xhs-mcp get_login_qrcode 返回 image 块键名是 `mime_type`（非 mimeType/media_type），
  _extract_qr_image 漏配（虽有 base64 兜底，但显式支持更稳）。
- 容器缺 Chromium → get_login_qrcode 报错，_fetch_qr_url `except: return None` **静默吞**，
  installer 永远看不到"缺浏览器"这个可自愈错误 → 卡在"（二维码获取中…）"。
- 「我已完成，继续」卡决策后**无复验/恢复钩子**（死按钮）：decide 不重跑 readiness。
"""
from __future__ import annotations

import uuid

import pytest

from app.services import readiness as rd


def test_extract_qr_image_handles_mcp_block_shape():
    """真实块：{'type':'image','base64':...,'mime_type':'image/png'} → data URI。"""
    blocks = [
        {"type": "text", "text": "请扫码"},
        {"type": "image", "base64": "iVBORw0KGgoAAAANS" + "A" * 40, "mime_type": "image/png"},
    ]
    uri = rd._extract_qr_image(blocks)
    assert uri is not None
    assert uri.startswith("data:image/png;base64,iVBORw0KGgo")


@pytest.mark.asyncio
async def test_fetch_qr_surfaces_error_not_swallow(monkeypatch):
    """QR 拉取异常 → 返回结构化 (None, error)，不再静默吞成 None。"""
    class _Server:
        name = "xhs"
        url = "http://localhost:18060/mcp"
        headers = None

    async def _boom(*a, **k):
        raise RuntimeError("can't find a browser binary for your OS")

    monkeypatch.setattr(rd, "_qr_via_mcp", _boom, raising=False)
    img, err = await rd.fetch_qr(_Server())
    assert img is None
    assert err is not None and "browser binary" in err


@pytest.mark.asyncio
async def test_decide_readiness_card_reruns_ensure_ready(db_session, monkeypatch):
    """paused_waiting_capability + readiness: 卡决策后 → 重跑 ensure_ready（复验恢复钩子）。"""
    from app.models.agent import Agent
    from app.models.mission import Mission
    from app.models.skill import MCPServer
    from app.models.user import User
    from app.services import pending_approval_service as pa

    u = User(username="adm-qr", email="adm-qr@x.com", hashed_password="x", role="admin")
    sup = Agent(name="sup-qr", kind="super", category="custom")
    server = MCPServer(name="xhs-qr", server_type="http", url="http://x/mcp")
    db_session.add_all([u, sup, server])
    await db_session.flush()
    m = Mission(
        name="ops", slug="ops-qr-x", supervisor_agent_id=sup.id, created_by=u.id,
        status="active", lifecycle_status="paused_waiting_capability",
        paused_reason=f"readiness:xhs-qr:logged_in 需人工介入（human-qr）",
    )
    db_session.add(m)
    await db_session.flush()
    card = await pa.create_pending(
        db_session, mission_id=m.id, title="[xhs-qr] 扫码登录",
        message="扫码", options=["我已完成，继续"],
    )

    called = {}

    async def _spy(db, sid, **kw):
        called["sid"] = sid
        called["mission_id"] = kw.get("mission_id")
        return {"ready": True, "pending": []}

    monkeypatch.setattr(rd, "ensure_ready_for_server", _spy)

    await pa.decide(db_session, request_id=card.request_id, option="我已完成，继续",
                    decided_by="user:x")

    assert called.get("mission_id") == m.id, "readiness 卡决策后应重跑 ensure_ready 复验"
