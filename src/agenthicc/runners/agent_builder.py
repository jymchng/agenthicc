"""Factory for the lauren-ai AgentRunnerBase wired to a SignalBus."""
from __future__ import annotations

from typing import Any


def _build_agent_runner(llm_cfg: Any) -> Any:
    """Build a lauren-ai AgentRunnerBase wired to a SignalBus."""
    if llm_cfg is None:
        return None
    from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
    from lauren_ai._module import _build_transport          # noqa: PLC0415
    from lauren_ai._signals import SignalBus                # noqa: PLC0415

    transport = _build_transport(llm_cfg)
    return AgentRunnerBase(transport=transport, signals=SignalBus())
