"""Hermetic tool stubs for cassette replay.

A recorded cassette is replayed *open loop*: the transport returns the recorded
model responses in order, regardless of what the tools return.  Executing the
recorded tool calls for real would therefore buy nothing for the replay and
would be dangerous — a recorded ``run_bash`` call typically ``cd``s into the
recording machine's checkout and mutates it.

:func:`hermetic_builtin_groups` replaces every side-effecting built-in tool
with a stub that keeps the original's exact provider-facing contract — name,
description, JSON schema and capability metadata — so the agent turn offers the
same tool catalog, the capability gate filters it the same way, and a recorded
call is routed exactly as it was when recorded.  The stub binds the recorded
arguments against the original signature, records any mismatch as a contract
violation, and returns a structured "replayed" result without touching the
filesystem, git, a shell, the network, or email.

The harness installs the stubbed groups with
:func:`agenthicc.plugins.registry.override_builtin_tools`, which replaces the
built-in catalog inside the tool registry itself.  Merely shadowing built-ins
with same-named plugin tools is not hermetic: a phase that filters its plugin
tools by capability would drop the stub and leave the real tool reachable.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from agenthicc.plugins.registry import ToolGroup
from agenthicc.tools.base import ToolLike, tool_name

__all__ = [
    "PRESERVED_GROUPS",
    "ReplayToolCall",
    "ReplayToolLog",
    "hermetic_builtin_groups",
    "make_replay_stub",
]

#: Built-in tool groups that are kept real during a hermetic replay.  The
#: introspection tools only read the installed agenthicc package, so they are
#: deterministic and side-effect free.
PRESERVED_GROUPS: frozenset[str] = frozenset({"introspect"})

_TOOL_META_ATTR = "__lauren_ai_tool__"
_TOOL_METADATA_ATTR = "__lauren_ai_tool_metadata__"


@dataclass(frozen=True)
class ReplayToolCall:
    """One call a replayed model made to a stubbed tool."""

    entry_index: int
    name: str
    arguments: dict[str, object]
    violation: str = ""


def _no_entry() -> int:
    return -1


@dataclass
class ReplayToolLog:
    """Ordered record of every stubbed tool call made during a replay.

    ``current_entry`` reports the cassette entry whose response requested the
    call, so a violation names the exact recorded request.
    """

    calls: list[ReplayToolCall] = field(default_factory=list)
    current_entry: Callable[[], int] = _no_entry

    @property
    def violations(self) -> list[str]:
        """Contract violations: recorded calls the current signature rejects."""
        return [
            f"entry {call.entry_index}: {call.name}: {call.violation}"
            for call in self.calls
            if call.violation
        ]


def make_replay_stub(original: ToolLike, log: ReplayToolLog) -> ToolLike:
    """Return a side-effect-free stub with *original*'s exact tool contract.

    The stub's schema is generated from a copy of the original signature and
    resolved annotations, so the provider sees an identical tool.  Capability
    and presentation metadata are copied, so mode filtering and the capability
    gate treat the stub exactly like the real tool.
    """
    from lauren_ai._tools import tool as _tool

    if not callable(original):
        raise TypeError(f"cannot stub non-callable tool {original!r}")
    meta = getattr(original, _TOOL_META_ATTR, None)
    if meta is None:
        raise TypeError(f"{original!r} is not a lauren-ai tool")

    name = tool_name(original)
    signature = inspect.signature(original)
    hints = typing.get_type_hints(original)

    async def _replayed(*args: object, **kwargs: object) -> dict[str, object]:
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError as exc:
            log.calls.append(
                ReplayToolCall(
                    entry_index=log.current_entry(),
                    name=name,
                    arguments=dict(kwargs),
                    violation=str(exc),
                )
            )
            return {
                "ok": False,
                "replayed": True,
                "error": f"replay contract violation: {exc}",
            }
        arguments = {
            key: value for key, value in bound.arguments.items() if key not in {"ctx", "context"}
        }
        log.calls.append(
            ReplayToolCall(entry_index=log.current_entry(), name=name, arguments=arguments)
        )
        return {
            "ok": True,
            "replayed": True,
            "tool": name,
            "note": "Side effects are disabled during cassette replay.",
        }

    _replayed.__name__ = getattr(original, "__name__", name)
    _replayed.__qualname__ = getattr(original, "__qualname__", name)
    _replayed.__doc__ = getattr(original, "__doc__", None)
    _replayed.__module__ = getattr(original, "__module__", __name__)
    _replayed.__annotations__ = dict(hints)
    _replayed.__dict__["__signature__"] = signature

    stub = _tool(
        name=name,
        description=str(getattr(meta, "description", "") or "") or None,
    )(_replayed)

    metadata = getattr(original, _TOOL_METADATA_ATTR, None)
    if isinstance(metadata, dict):
        setattr(stub, _TOOL_METADATA_ATTR, dict(metadata))
    return stub


def hermetic_builtin_groups(
    log: ReplayToolLog,
    *,
    preserved_groups: Iterable[str] = PRESERVED_GROUPS,
) -> list[ToolGroup]:
    """Return the built-in tool groups with every side-effecting tool stubbed.

    Groups named in *preserved_groups* are returned unchanged.  Every other
    group keeps its name, label, description and priority, so the registry
    describes the same catalog, but each of its tools is replaced by
    :func:`make_replay_stub`.  Install the result with
    :func:`agenthicc.plugins.registry.override_builtin_tools`.
    """
    from agenthicc.agent_tools import BUILTIN_GROUPS

    keep = frozenset(preserved_groups)
    groups: list[ToolGroup] = []
    for group in BUILTIN_GROUPS:
        if group.name in keep:
            groups.append(group)
            continue
        groups.append(
            ToolGroup(
                name=group.name,
                label=group.label,
                description=group.description,
                tools=[_as_plugin_tool(make_replay_stub(tool, log)) for tool in group.tools],
                priority=group.priority,
            )
        )
    return groups


def _as_plugin_tool(stub: ToolLike) -> Callable[..., object]:
    if not callable(stub):
        raise TypeError(f"replay stub {stub!r} is not callable")
    return stub
