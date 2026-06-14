"""Factory for the lauren-ai AgentRunnerBase wired to a SignalBus."""
from __future__ import annotations

from typing import Any


def _build_agent_runner(llm_cfg: Any, transcript: Any = None) -> Any:
    """Build a lauren-ai AgentRunnerBase wired to a SignalBus for tool-call tracking."""
    if llm_cfg is None:
        return None
    from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
    from lauren_ai._module import _build_transport  # noqa: PLC0415
    from lauren_ai._signals import SignalBus  # noqa: PLC0415

    bus = SignalBus()

    if transcript is not None:
        # Capture the current agent_id at signal time via closure over a mutable cell
        _current_agent: list[str] = ["system"]

        @bus.on_any
        async def _route(sig: Any) -> None:
            sig_type = type(sig).__name__
            if sig_type == "ToolCallStarted":
                transcript.add_tool_call(
                    agent_id=_current_agent[0],
                    tool_use_id=getattr(sig, "tool_use_id", ""),
                    name=getattr(sig, "tool_name", ""),
                    args=dict(getattr(sig, "input", {}) or {}),
                )
            elif sig_type == "ToolCallComplete":
                transcript.finish_tool_call(
                    tool_use_id=getattr(sig, "tool_use_id", ""),
                    success=bool(getattr(sig, "success", True)),
                    duration_ms=getattr(sig, "duration_ms", None),
                    error=getattr(sig, "error", None),
                )

        # Expose the agent cell so _run_agent_turn can set it
        bus._current_agent_cell = _current_agent  # type: ignore[attr-defined]

    transport = _build_transport(llm_cfg)
    return AgentRunnerBase(transport=transport, signals=bus)
