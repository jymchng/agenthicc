"""populate_agent_tools — production replacement for lauren_ai.testing._build_runner_for_agent (PRD-95).

``_build_runner_for_agent`` was imported from the testing module solely for its
side effect of populating ``meta.tools`` from ``meta.tool_classes``.  This
module exposes that same logic without touching testing infrastructure.
"""
from __future__ import annotations

import contextlib


def populate_agent_tools(agent_instance: object, tools: list[object]) -> None:
    """Populate ``meta.tools`` on *agent_instance* from *tools*.

    *tools* is the list of tool classes previously passed to ``@use_tools()``.
    This function converts them into the ``meta.tools`` dict that the runner
    uses at inference time.

    Parameters
    ----------
    agent_instance:
        An instance of an ``@agent()``-decorated class.
    tools:
        The tool class list — typically ``registry.tools`` or
        ``meta.tool_classes`` of the agent class.
    """
    from lauren_ai._agents import AGENT_META              # noqa: PLC0415
    from lauren_ai._tools import TOOL_META, _add_to_tool_map  # noqa: PLC0415

    meta = getattr(type(agent_instance), AGENT_META, None)
    if meta is None:
        return

    tool_map: dict = {}
    for tool_ref in tools:
        if tool_ref is None:
            continue
        if getattr(tool_ref, TOOL_META, None) is not None:
            with contextlib.suppress(Exception):
                _add_to_tool_map(tool_map, tool_ref)
    meta.tools = tool_map
