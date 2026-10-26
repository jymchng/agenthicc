"""Agenthicc kernel — event-sourced AppState (PRD-01).

Public names resolve lazily (AGX-01): importing ``agenthicc.kernel`` — or
``from agenthicc.kernel import SystemSettings`` — loads only the submodule that
defines the requested name, so configuration code does not import the event
processor and asyncio.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .events import PROJECTION_EVENT_TYPES, Effect, EffectType, Event, UnknownEventError
    from .processor import (
        EffectExecutor,
        EventProcessor,
        EventSink,
        NoOpEffectExecutor,
        restore_from_log,
    )
    from .reducer import REDUCED_EVENT_TYPES, ReducerFn, root_reducer
    from .state import (
        AppState,
        Intent,
        IntentStatus,
        NodeStatus,
        PermissionRule,
        SecurityPolicy,
        SystemSettings,
        ToolRegistration,
        Workflow,
        WorkflowNode,
    )

__all__ = [
    "PROJECTION_EVENT_TYPES",
    "REDUCED_EVENT_TYPES",
    "AppState",
    "Effect",
    "EffectExecutor",
    "EffectType",
    "Event",
    "EventProcessor",
    "EventSink",
    "Intent",
    "IntentStatus",
    "NodeStatus",
    "NoOpEffectExecutor",
    "PermissionRule",
    "ReducerFn",
    "SecurityPolicy",
    "SystemSettings",
    "ToolRegistration",
    "UnknownEventError",
    "Workflow",
    "WorkflowNode",
    "restore_from_log",
    "root_reducer",
]

_EXPORTS: dict[str, str] = {
    "PROJECTION_EVENT_TYPES": "events",
    "Effect": "events",
    "EffectType": "events",
    "Event": "events",
    "UnknownEventError": "events",
    "EffectExecutor": "processor",
    "EventProcessor": "processor",
    "EventSink": "processor",
    "NoOpEffectExecutor": "processor",
    "restore_from_log": "processor",
    "REDUCED_EVENT_TYPES": "reducer",
    "ReducerFn": "reducer",
    "root_reducer": "reducer",
    "AppState": "state",
    "Intent": "state",
    "IntentStatus": "state",
    "NodeStatus": "state",
    "PermissionRule": "state",
    "SecurityPolicy": "state",
    "SystemSettings": "state",
    "ToolRegistration": "state",
    "Workflow": "state",
    "WorkflowNode": "state",
}
_SUBMODULES = frozenset({"events", "processor", "reducer", "state"})


def __getattr__(name: str) -> object:
    if name in _SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value: object = vars(importlib.import_module(f".{module}", __name__))[name]
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__, *_SUBMODULES})
