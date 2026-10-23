"""Shared stand-ins for runtime objects that tests build without a full session."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from agenthicc.runners.session_context import SessionContext
from agenthicc.workflows.config import WorkflowConfig


def _stand_in(cls: type[object], fields: dict[str, object]) -> SimpleNamespace:
    """Return a namespace carrying every dataclass field of *cls*.

    Optional fields take their real defaults. Required fields the test does
    not supply are ``""`` for strings and ``None`` otherwise — pass a real
    object for any the code under test actually uses.
    """
    values: dict[str, object] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        if field.default is not dataclasses.MISSING:
            values[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:
            values[field.name] = field.default_factory()
        else:
            values[field.name] = "" if field.type in (str, "str") else None
    values.update(fields)
    return SimpleNamespace(**values)


def session_context(**fields: object) -> SimpleNamespace:
    """Return a ``SessionContext`` stand-in that carries every field.

    Production code reads ``SessionContext`` fields directly, so a fake must
    have all of them.
    """
    return _stand_in(SessionContext, fields)


def workflow_config(**fields: object) -> SimpleNamespace:
    """Return a ``WorkflowConfig`` stand-in that carries every field.

    Runners read ``WorkflowConfig`` fields directly, so a fake must have all of
    them; methods such as ``all_plugin_tools`` are the test's to supply.
    """
    return _stand_in(WorkflowConfig, fields)
