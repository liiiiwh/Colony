"""M7: TesterAgent / Builder Supervisor 用的 smoke test 工具集。"""

from __future__ import annotations

import logging
import uuid

from langchain_core.tools import StructuredTool

from app.services import mission_service
from app.skills_builtin.context import BuiltinToolContext

logger = logging.getLogger(__name__)


def mission_run_test_tool(ctx: BuiltinToolContext) -> StructuredTool:
    async def _run(mission_id: str, scenario: str = "") -> dict:
        """对 mission_id 跑一次 sandbox smoke test。

        Args:
            mission_id: 要测试的目标 Mission UUID
            scenario: 用户最初的 acceptance 描述（中文 / 英文皆可），LLM 用它来判产出是否合预期
        """
        from app.services import mission_test_runner

        if ctx.db_factory is None:
            return {"ok": False, "error": "db_factory not available"}
        # A4：scenario 长度上限
        if scenario and len(scenario) > 10000:
            return {
                "ok": False,
                "error": "SCENARIO_TOO_LONG",
                "instruction": (
                    f"scenario 长度 {len(scenario)} 超过上限 10000；"
                    "请提炼到 acceptance 检查清单的关键句即可。"
                ),
            }
        async with ctx.db_factory() as db:
            pid = await mission_service.resolve_mission_id(db, mission_id)
            if pid is None:
                return {"ok": False, "error": f"mission_id={mission_id!r} 不是合法 UUID 或 slug"}
            try:
                res = await mission_test_runner.run_smoke_test(
                    db, pid, scenario_text=scenario
                )
                return {"ok": True, **res}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return StructuredTool.from_function(
        coroutine=_run,
        name="mission_run_test",
        description=(
            "（Tester / Builder Supervisor）对 mission_id 跑一次 sandbox smoke test。"
            "流程：clone → start → run_once → stop → cleanup → LLM judge。"
            "返回 {probe:{...}, judge:{verdict,reasoning,suggestions}}。"
        ),
    )
