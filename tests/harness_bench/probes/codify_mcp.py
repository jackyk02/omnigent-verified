"""Codify-MCP probe — can a native harness call the Codify relay?

This is intentionally separate from ``ToolCallingProbe``: that probe exercises
whatever tool mechanism is native to the transport (for example Bash in Claude
Code), while this probe targets the read-only ``sys_session_list`` tool exposed
through the generated ``codify`` MCP server.
"""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.mcp_tools import (
    TARGET_CODIFY_MCP_TOOL,
    is_target_codify_mcp_tool,
)
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class CodifyMcpProbe(CapabilityProbe):
    name = "codify_mcp"
    title = "Codify MCP"
    priority = Priority.P1
    applies_to = Applicability.NATIVE

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_mcp_tool_turn()
        names = [str(call.get("name") or "") for call in result.tool_calls]
        matched = [name for name in names if is_target_codify_mcp_tool(name)]
        detail = {"tool_calls": names, "matched_calls": matched, "completed": result.completed}

        if matched:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"called {TARGET_CODIFY_MCP_TOOL} through the Codify MCP relay",
                detail=detail,
            )
        infra = infra_failure_reason(result)
        if infra is not None:
            return ProbeResult(Verdict.SKIPPED, note=infra, detail=detail)
        if result.error:
            return ProbeResult(Verdict.SKIPPED, note=str(result.error), detail=detail)
        if result.timed_out:
            return ProbeResult(
                Verdict.SKIPPED,
                note=(f"timed out before calling {TARGET_CODIFY_MCP_TOOL} through Codify MCP"),
                detail=detail,
            )
        return ProbeResult(
            Verdict.SKIPPED,
            note=(f"model did not call {TARGET_CODIFY_MCP_TOOL} through the Codify MCP relay"),
            detail=detail,
        )
