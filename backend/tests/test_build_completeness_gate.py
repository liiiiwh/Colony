"""build_finalizer 完整性 gate：残废 super 不激活。

2026-07-03 grill 决议（问题2-c）：多步不变量靠 LLM 自觉不可靠（这次 Builder 建了 6 个
worker 却漏建发帖 worker、没装 MCP、连 required_capabilities 花名册都没声明，收尾照样
激活了残废 super）。改为 FSM 兜底：finalize 时校验 roster ⟺ worker 双向一致：
- 声明了 required_capabilities 但某 cap 没对应 worker → 缺 worker，不完整。
- 建了非系统 worker 但没有任何 super 的 roster 声明它（孤儿）→ 不完整（super 不会派它）。
不完整 → 不 kickoff/不激活，回 builder 会话报缺口，等 Builder 补齐。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.mission import Mission
from app.models.user import User
from app.services import build_finalizer as bf


async def _setup(db, *, roster, worker_caps):
    u = User(username="admin", email="bf@x.com", hashed_password="x", role="admin")
    db.add(u)
    await db.flush()
    sup = Agent(
        name="s-bf", kind="super", category="custom", slug="s-bf",
        extra_config={"required_capabilities": roster} if roster is not None else {},
    )
    db.add(sup)
    await db.flush()
    for c in worker_caps:
        db.add(Agent(name=f"w-{c}", kind="worker", category="custom", capability=c))
    m = Mission(name="sbf", slug="s-bf-mission", supervisor_agent_id=sup.id,
                created_by=u.id, status="active")
    db.add(m)
    await db.flush()
    return sup, m


@pytest.mark.asyncio
async def test_complete_when_roster_matches_workers(db_session):
    sup, _ = await _setup(db_session, roster=["a", "b"], worker_caps=["a", "b"])
    res = await bf.check_build_completeness(db_session, sup)
    assert res["complete"] is True, res


@pytest.mark.asyncio
async def test_missing_worker_for_declared_capability(db_session):
    sup, _ = await _setup(db_session, roster=["a", "b", "publisher"], worker_caps=["a", "b"])
    res = await bf.check_build_completeness(db_session, sup)
    assert res["complete"] is False
    assert "publisher" in res["missing_workers"]


@pytest.mark.asyncio
async def test_orphan_workers_not_rostered_flag_incomplete(db_session):
    """真出的 bug 形态：建了 worker 但 roster 为空 → 孤儿 → 不完整。"""
    sup, _ = await _setup(db_session, roster=[], worker_caps=["trend", "writer", "publisher"])
    res = await bf.check_build_completeness(db_session, sup)
    assert res["complete"] is False
    assert set(res["orphan_workers"]) >= {"trend", "writer", "publisher"}


@pytest.mark.asyncio
async def test_empty_build_zero_workers_empty_roster_incomplete(db_session):
    """2026-07-06 e2e 实证的 gate 空洞：installer 超时→Builder 回退→建了 super+装了 MCP
    但 0 worker、roster 也空 → missing/orphan 都空 → 旧逻辑'真空完整'放行残废 super。
    业务 super 空 roster = 什么都派不了 = 构建未完成，必须挡下。"""
    sup, _ = await _setup(db_session, roster=[], worker_caps=[])
    res = await bf.check_build_completeness(db_session, sup)
    assert res["complete"] is False, "空 roster + 0 worker 不该真空放行"
    assert res.get("empty_roster") is True


@pytest.mark.asyncio
async def test_unbound_managed_mcp_flags_incomplete(db_session):
    """2026-07-06 e2e 实证残留：installer 装好并注册了本地 MCP，但 Builder 建完 worker
    忘了 agent_mcp_bind（LLM 超时/漏步）→ worker 用不上 MCP 方法（用户原始投诉）。
    结构化强制"必绑"：有受管本地 MCP（startup_command）却没绑给任何业务 worker → 不完整，
    退回 Builder 绑（不是 auto-bind，是把'必绑'从 LLM 自觉变 gate 兜底）。"""
    from app.models.skill import MCPServer

    sup, _ = await _setup(db_session, roster=["publisher"], worker_caps=["publisher"])
    # 注册一个受管本地 MCP，但不绑给任何 worker
    db_session.add(MCPServer(
        name="xhs-mcp", server_type="http", url="http://localhost:18061/mcp",
        is_enabled=True, startup_command=["./xhs-mcp"],
    ))
    await db_session.flush()
    res = await bf.check_build_completeness(db_session, sup)
    assert res["complete"] is False, "受管 MCP 没绑给任何 worker → 不完整"
    assert "xhs-mcp" in res.get("unbound_mcp", [])


@pytest.mark.asyncio
async def test_bound_managed_mcp_is_complete(db_session):
    """MCP 绑到了业务 worker → 完整。"""
    from app.models.agent import AgentMCPServer
    from app.models.skill import MCPServer
    from sqlalchemy import select as _sel

    sup, _ = await _setup(db_session, roster=["publisher"], worker_caps=["publisher"])
    mcp = MCPServer(name="xhs-mcp", server_type="http", url="http://localhost:18061/mcp",
                    is_enabled=True, startup_command=["./xhs-mcp"])
    db_session.add(mcp)
    await db_session.flush()
    pub = (await db_session.execute(
        _sel(Agent).where(Agent.capability == "publisher"))).scalar_one()
    db_session.add(AgentMCPServer(agent_id=pub.id, mcp_server_id=mcp.id))
    await db_session.flush()
    res = await bf.check_build_completeness(db_session, sup)
    assert res["complete"] is True, res
    assert not res.get("unbound_mcp")


@pytest.mark.asyncio
async def test_system_super_empty_roster_is_ok(db_session):
    """系统 super（Builder/Worker-Opt 等）本就无 required_capabilities，不受空 roster 规则影响
    （gate 只为新建业务 super 兜底，不该误伤系统对象）。"""
    u = User(username="admin", email="sysbf@x.com", hashed_password="x", role="admin")
    db_session.add(u)
    await db_session.flush()
    sysup = Agent(name="sys-sup", kind="super", category="builder", slug="sys-sup",
                  is_system=True, extra_config={})
    db_session.add(sysup)
    await db_session.flush()
    res = await bf.check_build_completeness(db_session, sysup)
    assert res.get("empty_roster") is not True, "系统 super 空 roster 不算不完整"


@pytest.mark.asyncio
async def test_finalize_blocks_activation_when_incomplete(db_session, monkeypatch):
    """不完整 → 不 kickoff、不写 super_activated 按钮，返回 skipped=incomplete。"""
    sup, m = await _setup(db_session, roster=[], worker_caps=["trend", "publisher"])
    await db_session.commit()

    kicked = {"n": 0}

    async def _spy_start(db, pid, kickoff=False):
        kicked["n"] += 1
    from app.services import mission_daemon as md
    monkeypatch.setattr(md, "start", _spy_start)

    res = await bf.finalize_super_build(db_session, m.id, m.id, "main")

    assert res.get("skipped") == "incomplete", res
    assert kicked["n"] == 0, "残废 super 不该被 kickoff 激活"


@pytest.mark.asyncio
async def test_auto_adopt_orphans_built_by_this_mission(db_session):
    """2026-07-05 确定性自修复：LLM 三连无视 agent_update 声明指令（e2e 实证）→
    花名册不再赌 LLM 自觉。本 builder mission 建的孤儿 worker（built_by_mission_id
    匹配 + 没被任何 super 声明）→ finalize 前自动并入 super.required_capabilities。"""
    sup, m = await _setup(db_session, roster=[], worker_caps=[])
    await db_session.commit()

    # 3 个本会话建的 worker + 1 个别处建的 worker（不该被收编）
    builder_mid = m.id  # 用 built mission 冒充 builder mission（helper 只看 id 匹配）
    for c in ("trend", "writer", "publisher"):
        db_session.add(Agent(name=f"w-{c}", kind="worker", category="custom",
                             capability=c, built_by_mission_id=builder_mid))
    db_session.add(Agent(name="w-alien", kind="worker", category="custom",
                         capability="alien_cap"))
    await db_session.commit()

    adopted = await bf.auto_adopt_orphan_workers(db_session, sup, builder_mid)
    assert set(adopted) == {"trend", "writer", "publisher"}

    await db_session.refresh(sup)
    roster = set((sup.extra_config or {}).get("required_capabilities") or [])
    assert roster == {"trend", "writer", "publisher"}, roster
    assert "alien_cap" not in roster, "非本会话建的 worker 不得被收编"


@pytest.mark.asyncio
async def test_finalize_auto_repairs_roster_then_activates(db_session, monkeypatch):
    """finalize 全链：roster 空 + 本会话孤儿 worker → 自动收编 → gate 变 complete →
    正常走 kickoff 激活（不再卡死等 LLM）。"""
    sup, m = await _setup(db_session, roster=[], worker_caps=[])
    await db_session.commit()
    for c in ("trend", "publisher"):
        db_session.add(Agent(name=f"w2-{c}", kind="worker", category="custom",
                             capability=c, built_by_mission_id=m.id))
    await db_session.commit()

    kicked = {"n": 0}

    async def _spy_start(db, pid, kickoff=False):
        kicked["n"] += 1
    from app.services import mission_daemon as md
    monkeypatch.setattr(md, "start", _spy_start)

    # notify_mission_id 传 m.id（测试里 builder mission 即它自己）
    res = await bf.finalize_super_build(db_session, m.id, m.id, "main")

    assert res.get("ok") is True, f"自修复后应正常激活（实得 {res}）"
    assert kicked["n"] == 1
    await db_session.refresh(sup)
    assert set((sup.extra_config or {}).get("required_capabilities") or []) == {"trend", "publisher"}


@pytest.mark.asyncio
async def test_incomplete_gate_enqueues_drain_message(db_session, monkeypatch):
    """2026-07-03 e2e 实证停摆点：gate 拦下后只写 agent_log，builder 转 paused_idle，
    没有任何触发器让它补 roster → 永远停摆。新语义：gate 同时把缺口报告 **enqueue 进
    builder 的 pending 队列**（auto-drain / reconcile 会接力开 tick）。带防失控：
    已有未消费的 build_incomplete pending 时不重复入队。"""
    sup, m = await _setup(db_session, roster=[], worker_caps=["trend"])
    await db_session.commit()

    enq: list[tuple] = []

    from app.services import pending_queue as pq

    async def _spy_enqueue(db, mission_id, agent_id, content, **kw):
        enq.append((mission_id, content, kw.get("meta")))
        return {"ok": True}

    async def _no_dup(db, mission_id, meta_type=None):
        return 0

    monkeypatch.setattr(pq, "enqueue_user_message", _spy_enqueue)
    monkeypatch.setattr(bf, "_count_unconsumed_incomplete", _no_dup, raising=False)

    res = await bf.finalize_super_build(db_session, m.id, m.id, "main")
    assert res.get("skipped") == "incomplete"
    assert len(enq) == 1, "缺口报告应 enqueue 到 builder pending 队列（触发接力 tick）"
    assert "trend" in str(enq[0][1]), "报告应带具体缺口"
    assert (enq[0][2] or {}).get("type") == "build_incomplete"


@pytest.mark.asyncio
async def test_incomplete_gate_enqueue_capped_at_three(db_session, monkeypatch):
    """防失控：gate 每轮 tick 末都可能重评 → enqueue→drain→tick→再 enqueue 的链条若
    LLM 永远修不好会无限烧 token。同一 project 已有 ≥3 条 build_incomplete 消息记录
    （messages 表）→ 不再入队（只留 agent_log，等人工介入）。"""
    sup, m = await _setup(db_session, roster=[], worker_caps=["trend"])
    await db_session.commit()

    enq: list = []

    from app.services import pending_queue as pq

    async def _spy_enqueue(db, mission_id, agent_id, content, **kw):
        enq.append(content)
        return {"ok": True}

    async def _no_dup(db, mission_id):
        return 0

    async def _three_attempts(db, project_slug):
        return 3

    monkeypatch.setattr(pq, "enqueue_user_message", _spy_enqueue)
    monkeypatch.setattr(bf, "_count_unconsumed_incomplete", _no_dup)
    monkeypatch.setattr(bf, "_count_incomplete_reports", _three_attempts)

    res = await bf.finalize_super_build(db_session, m.id, m.id, "main")
    assert res.get("skipped") == "incomplete"
    assert enq == [], "已报 3 次缺口 → 不再入队（防无限 tick 烧 token）"


@pytest.mark.asyncio
async def test_maybe_autodrain_spawns_drain_when_pending(db_session, _patched_session_local, monkeypatch):
    """run_once API / _drain_kickoff 收尾时调 maybe_autodrain：pending>0 且 lifecycle
    可消费（paused_idle/running）→ 安排 _drain_kickoff；否则不动。
    （2026-07-03 实证：「运行一次」单发 tick 后 gate 入队的报告没人消费 → 又停摆。）"""
    import asyncio as _aio

    from app.models.user import User
    from app.services import mission_daemon as md
    from app.services import pending_queue as pq

    u = User(username="adm-ad", email="ad@x.com", hashed_password="x", role="admin")
    db_session.add(u)
    await db_session.flush()
    sup = Agent(name="s-ad", kind="super", category="builder", slug="s-ad")
    db_session.add(sup)
    await db_session.flush()
    m = Mission(name="mad", slug="m-ad", supervisor_agent_id=sup.id, created_by=u.id,
                status="active", lifecycle_status="paused_idle", runtime_status="running")
    db_session.add(m)
    await db_session.commit()

    pending = {"n": 1}

    async def _count(db, mission_id):
        return pending["n"]

    drained: list = []

    async def _spy_drain(mission_id):
        drained.append(mission_id)

    monkeypatch.setattr(pq, "count_pending", _count)
    monkeypatch.setattr(md, "_drain_kickoff", _spy_drain)

    await md.maybe_autodrain(m.id)
    await _aio.sleep(0.05)
    assert drained == [m.id]

    # pending=0 → 不再安排
    pending["n"] = 0
    await md.maybe_autodrain(m.id)
    await _aio.sleep(0.05)
    assert drained == [m.id], "无 pending 不该再安排 drain"


@pytest.mark.asyncio
async def test_drain_kickoff_no_rearm_on_failure(db_session, _patched_session_local, monkeypatch):
    """2026-07-03 生产热循环实证（9717 次/分钟）：drain tick 失败是瞬时的（如 runtime
    stopped 直接 raise），失败后仍 re-arm → 无退避热循环。失败 → 不 re-arm（等下次
    reconcile/用户触发）。"""
    import asyncio as _aio

    from app.services import mission_daemon as md

    mid = uuid.uuid4()

    async def _boom(db, mission_id, payload=None):
        raise ValueError("Mission 当前 runtime_status=stopped")

    rearmed: list = []

    async def _spy_autodrain(mission_id):
        rearmed.append(mission_id)
        return False

    monkeypatch.setattr(md, "run_once", _boom)
    monkeypatch.setattr(md, "maybe_autodrain", _spy_autodrain)

    await md._drain_kickoff(mid)
    await _aio.sleep(0.05)
    assert rearmed == [], "drain tick 失败不得 re-arm（瞬时失败 → 热循环）"


@pytest.mark.asyncio
async def test_drain_kickoff_starts_stopped_daemon(db_session, _patched_session_local, monkeypatch):
    """reconcile idle-drain 场景：paused_idle 的 mission daemon 没被 reconcile start
    （只恢复 lifecycle=running）→ runtime=stopped，run_once 必 raise。drain 前自动 start。"""
    from app.models.user import User
    from app.services import mission_daemon as md

    u = User(username="adm-dk", email="dk@x.com", hashed_password="x", role="admin")
    db_session.add(u)
    await db_session.flush()
    sup = Agent(name="s-dk", kind="super", category="builder", slug="s-dk")
    db_session.add(sup)
    await db_session.flush()
    m = Mission(name="mdk", slug="m-dk", supervisor_agent_id=sup.id, created_by=u.id,
                status="active", lifecycle_status="paused_idle", runtime_status="stopped")
    db_session.add(m)
    await db_session.commit()

    calls: list[str] = []

    async def _spy_start(db, mission_id, **kw):
        calls.append("start")
        return "running"

    async def _spy_run_once(db, mission_id, payload=None):
        calls.append("run_once")
        return {"ok": True}

    async def _no_rearm(mission_id):
        return False

    monkeypatch.setattr(md, "start", _spy_start)
    monkeypatch.setattr(md, "run_once", _spy_run_once)
    monkeypatch.setattr(md, "maybe_autodrain", _no_rearm)

    await md._drain_kickoff(m.id)
    assert calls == ["start", "run_once"], f"runtime=stopped 应先 start 再 run_once（实得 {calls}）"


@pytest.mark.asyncio
async def test_reconcile_drains_paused_idle_with_pending(db_session, _patched_session_local, monkeypatch):
    """重启接力扩展：paused_idle + pending>0 的 mission 也要 drain
    （gate 停摆场景重启后 builder 是 paused_idle，不在 lifecycle=running 恢复集里）。"""
    import asyncio as _aio

    from app.models.user import User
    from app.services import mission_daemon as md
    from app.services import pending_queue as pq

    u = User(username="adm-bfg", email="bfg@x.com", hashed_password="x", role="admin")
    db_session.add(u)
    await db_session.flush()
    sup = Agent(name="s-bfg", kind="super", category="builder", slug="s-bfg")
    db_session.add(sup)
    await db_session.flush()
    m = Mission(name="mbfg", slug="m-bfg", supervisor_agent_id=sup.id, created_by=u.id,
                status="active", lifecycle_status="paused_idle", runtime_status="running")
    db_session.add(m)
    await db_session.commit()

    async def _one_pending(db, mission_id):
        return 1 if mission_id == m.id else 0

    ticked: list = []

    async def _spy_run_once(db, mission_id, payload=None):
        ticked.append(mission_id)
        return {"ok": True}

    monkeypatch.setattr(pq, "count_pending", _one_pending)
    monkeypatch.setattr(md, "run_once", _spy_run_once)

    await md.reconcile_on_boot()
    await _aio.sleep(0.1)

    assert m.id in ticked, "paused_idle + pending>0 也应安排 drain tick"
