"""Phase 3 Skill API 测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.init_db import seed_builtin_skills
from app.models.skill import Skill

pytestmark = pytest.mark.asyncio


async def _auth(client: AsyncClient) -> dict[str, str]:
    resp = await client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_seed_builtin_skills_idempotent(seeded_db) -> None:
    # M4 起：27 个旧 skill + 8 个 Builder skill = 35
    from app.skills_builtin.registry import BUILTIN_SKILL_METADATA

    expected_count = len(BUILTIN_SKILL_METADATA)

    await seed_builtin_skills(seeded_db)
    result = await seeded_db.execute(select(Skill).where(Skill.is_builtin.is_(True)))
    first = result.scalars().all()
    assert len(first) == expected_count

    # 再跑一次不产生重复
    await seed_builtin_skills(seeded_db)
    result2 = await seeded_db.execute(select(Skill).where(Skill.is_builtin.is_(True)))
    second = result2.scalars().all()
    assert len(second) == len(first)


def test_format_artifact_fallback_chain() -> None:
    """workspace_read 渲染产物的 fallback 链：content → s3_url → s3_key → label。

    历史 bug：view_designer artifact 只有 s3_url（content 被清空且未上传 S3），
    旧版 _format_artifact 返回空串 → 下游 LLM 反复读到空内容自我循环。
    """
    from app.skills_builtin.worker_io.workspace_skills import _format_artifact

    # 1. content 优先
    assert _format_artifact({"type": "markdown", "label": "X", "content": "hello"}) == "hello"

    # 2. content 空 → fallback 到 s3_url
    out = _format_artifact({
        "type": "image", "label": "参考图", "content": "",
        "s3_url": "https://example.com/a.png", "s3_key": None,
    })
    assert "https://example.com/a.png" in out
    assert "参考图" in out

    # 3. content + s3_url 都空 → fallback 到 s3_key
    out2 = _format_artifact({
        "type": "3d-model", "label": "GLB", "content": "",
        "s3_url": None, "s3_key": "colony/x.glb",
    })
    assert "s3_key=colony/x.glb" in out2

    # 4. 全部空 → 兜底（不返回空串）
    out3 = _format_artifact({"type": "image", "label": "兜底"})
    assert out3 != ""
    assert "兜底" in out3 or "image" in out3

    # 5. content=None（vs 空串）也走 fallback
    out4 = _format_artifact({
        "type": "image", "label": "L", "content": None,
        "s3_url": "https://example.com/b.png",
    })
    assert "https://example.com/b.png" in out4


async def test_list_skills_after_seed(seeded_client: AsyncClient, seeded_db) -> None:
    await seed_builtin_skills(seeded_db)
    auth = await _auth(seeded_client)
    resp = await seeded_client.get("/api/skills", headers=auth)
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) >= 13
    assert all(
        s["is_builtin"] for s in payload if s["slug"].startswith(("workspace", "memory", "s3"))
    )


async def test_create_instruction_skill(seeded_client: AsyncClient) -> None:
    auth = await _auth(seeded_client)
    resp = await seeded_client.post(
        "/api/skills",
        headers=auth,
        json={
            "name": "产品思维",
            "slug": "product-thinking",
            "description": "指引 Agent 从用户价值出发思考",
            "skill_type": "instruction",
            "content_md": "---\nname: product-thinking\n---\n\n请优先考虑用户价值。",
            "is_enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["skill_type"] == "instruction"
    assert resp.json()["is_builtin"] is False


async def test_create_skill_slug_conflict(seeded_client: AsyncClient, seeded_db) -> None:
    await seed_builtin_skills(seeded_db)
    auth = await _auth(seeded_client)
    resp = await seeded_client.post(
        "/api/skills",
        headers=auth,
        json={
            "name": "尝试覆盖",
            "slug": "workspace_read",
            "skill_type": "instruction",
        },
    )
    assert resp.status_code == 409


async def test_cannot_delete_builtin_skill(seeded_client: AsyncClient, seeded_db) -> None:
    await seed_builtin_skills(seeded_db)
    auth = await _auth(seeded_client)
    lst = await seeded_client.get("/api/skills", headers=auth)
    target = next(s for s in lst.json() if s["is_builtin"])
    dele = await seeded_client.delete(f"/api/skills/{target['id']}", headers=auth)
    assert dele.status_code == 400


async def test_builtin_skill_only_toggle_allowed(seeded_client: AsyncClient, seeded_db) -> None:
    await seed_builtin_skills(seeded_db)
    auth = await _auth(seeded_client)
    lst = await seeded_client.get("/api/skills", headers=auth)
    target = next(s for s in lst.json() if s["is_builtin"])

    # 改 is_enabled 允许
    ok = await seeded_client.put(
        f"/api/skills/{target['id']}", headers=auth, json={"is_enabled": False}
    )
    assert ok.status_code == 200
    assert ok.json()["is_enabled"] is False

    # 改其他字段应拒绝
    bad = await seeded_client.put(
        f"/api/skills/{target['id']}", headers=auth, json={"name": "新名字"}
    )
    assert bad.status_code == 400
