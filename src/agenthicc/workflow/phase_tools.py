"""Phase transition tools — one @tool() per edge (PRD-101 fix).

The original single ``complete_phase(output, next)`` approach had two bugs:

1. ``complete_phase.__doc__ = _doc`` patched the docstring AFTER ``@_tool()``
   had already captured the schema, so the LLM never saw the edge labels.
2. ``next: str`` was an opaque parameter — the LLM had to guess valid values,
   producing 0ms tool failures from incorrect edge labels.

The fix: generate one ``@tool()``-decorated function *per edge*, each named
after the edge label.  The tool name IS the transition signal; no ``next``
parameter; no guessing.  ``__name__``, ``__qualname__``, and ``__doc__`` are
all set BEFORE ``@_tool()`` is applied so the schema is correct.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any


_FINISH_LABEL = "finish"   # tool name used for terminal nodes (no edges)


def make_transition_tools(
    node:             Any,           # PhaseNode
    data_bus:         Any,           # DataBus
    transition_event: asyncio.Event,
    transition_data:  dict,
    approval_svc:     Any,           # ApprovalService | None
) -> list:
    """Return one @tool()-decorated callable per edge on *node*.

    For a terminal node (no edges) a single ``finish(output)`` tool is returned.
    For a non-terminal node one tool is returned per EdgeSpec, named after the
    edge label.  The tool name is set on ``__name__`` BEFORE ``@_tool()`` is
    applied so it appears correctly in the LLM tool schema.

    All tools close over shared asyncio state:
      transition_event — set when the agent commits a transition.
      transition_data  — {"edge_label": str | None, "output": dict}.
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    if not node.edges:
        return [_make_terminal_tool(node, data_bus, transition_event, transition_data, _tool)]

    return [
        _make_edge_tool(edge, node, data_bus, transition_event, transition_data, approval_svc, _tool)
        for edge in node.edges
    ]


def _make_terminal_tool(
    node:             Any,
    data_bus:         Any,
    transition_event: asyncio.Event,
    transition_data:  dict,
    _tool:            Any,
) -> Any:
    """Return a ``finish(output)`` tool for a terminal node."""
    node_name = node.name

    async def _impl(output: dict) -> dict:
        data_bus.set(node_name, output)
        transition_event.set()
        transition_data["edge_label"] = None
        transition_data["output"]     = output
        return {
            "ok":     True,
            "message": (
                "Phase complete — this is the final phase.  Write a single "
                "short confirmation and stop."
            ),
        }

    _impl.__name__     = _FINISH_LABEL
    _impl.__qualname__ = _FINISH_LABEL
    _impl.__doc__      = (
        f"Signal that the '{node_name}' phase is complete.\n\n"
        "This is the final phase — the workflow ends after this call.\n\n"
        "Args:\n"
        "    output: Structured summary of what was accomplished."
    )
    return _tool()(_impl)


def _make_edge_tool(
    edge:             Any,           # EdgeSpec
    node:             Any,           # PhaseNode
    data_bus:         Any,           # DataBus
    transition_event: asyncio.Event,
    transition_data:  dict,
    approval_svc:     Any,
    _tool:            Any,
) -> Any:
    """Return a ``<edge.label>(output)`` tool for one outgoing edge."""
    node_name   = node.name
    edge_label  = edge.label
    target_name = edge.target or "(end)"

    async def _impl(output: dict) -> dict:
        # Gate: show overlay before committing.
        if edge.gate is not None and approval_svc is not None:
            from agenthicc.tools.approval import ApprovalRequest  # noqa: PLC0415
            req = ApprovalRequest(
                tool_name=edge.gate.title or f"Review: {node_name}",
                tool_use_id=uuid.uuid4().hex,
                tool_input=output,
                capabilities=frozenset(),
                event=asyncio.Event(),
                kind=edge.gate.kind,
            )
            response = await approval_svc.request_approval(req)
            if not response.allowed:
                fb = response.message or ""
                return {
                    "approved": False,
                    "feedback": fb or "Transition not approved.  Revise and try again.",
                }
            if response.message:
                output = {**output, "_user_instructions": response.message}

        data_bus.set(node_name, output)
        data_bus.record_edge(node_name, edge_label)
        transition_event.set()
        transition_data["edge_label"] = edge_label
        transition_data["output"]     = output
        return {
            "ok":     True,
            "message": (
                f"Transitioning to '{target_name}'.  "
                "Write a single short confirmation and stop."
            ),
        }

    target_desc = f"proceed to '{target_name}'" if edge.target else "end the workflow"
    gate_note   = (
        f"  Requires human approval before committing."
        if (edge.gate is not None) else ""
    )

    _impl.__name__     = edge_label
    _impl.__qualname__ = edge_label
    _impl.__doc__      = (
        f"Call this to {target_desc} from the '{node_name}' phase.{gate_note}\n\n"
        "Args:\n"
        "    output: Structured data for downstream phases."
    )
    return _tool()(_impl)
