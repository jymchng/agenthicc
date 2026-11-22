"""Shared stand-ins for runtime objects that tests build without a full session."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.runners.reload import ReloadService, SurfaceHolder
from agenthicc.runners.session_context import SessionContext
from agenthicc.runners.workflow_stack import WorkflowStack
from agenthicc.subagents.policy import SubagentExecutionPolicy
from agenthicc.subagents.types import DEFAULT_REGISTRY, SubagentTypeRegistry
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.tui.trigger import TriggerManager
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.workflows.config import WorkflowConfig


if TYPE_CHECKING:
    from agenthicc.skills.loader import SkillDef
    from agenthicc.tools.base import ToolLike


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
    have all of them. A session always has a trigger registry, so the stand-in
    carries an empty one unless the test supplies its own.
    """
    context = _stand_in(
        SessionContext, {"trigger_registry": TriggerManager(), "authored": None, **fields}
    )
    if "workflow_stack" not in fields:
        supplied_state = fields.get("app_state")
        state = supplied_state if isinstance(supplied_state, TUIAppState) else TUIAppState.create()
        policy = SubagentExecutionPolicy.from_app_state(state)
        assert policy is not None
        selected = fields.get("initial_workflow")
        context.workflow_stack = WorkflowStack(
            policy,
            mode_default=state.active_mode().default_workflow,
            selection=selected if isinstance(selected, str) else None,
        )
    if "reload" not in fields:
        kernel = fields.get("processor")
        workflows = fields.get("workflow_registry")
        commands = fields.get("cmd_registry")
        subagents = fields.get("subagent_registry")
        skills = cast("Mapping[str, SkillDef]", fields.get("skills", {}))
        tools = cast("Sequence[ToolLike]", fields.get("tools", ()))
        context.reload = ReloadService(
            processor=kernel
            if isinstance(kernel, EventProcessor)
            else EventProcessor(initial_state=AppState.create(), persist=False),
            project_dir=Path.cwd() / ".agenthicc",
            user_dir=Path.home() / ".agenthicc",
            workflow_registry=workflows
            if isinstance(workflows, WorkflowRegistry)
            else WorkflowRegistry(),
            command_registry=commands
            if isinstance(commands, UnifiedCommandRegistry)
            else UnifiedCommandRegistry(),
            subagent_registry=subagents
            if isinstance(subagents, SubagentTypeRegistry)
            else DEFAULT_REGISTRY.copy(),
            skills=skills,
            tools=tools,
        )
    return context


def workflow_config(**fields: object) -> SimpleNamespace:
    """Return a ``WorkflowConfig`` stand-in that carries every field.

    Runners read ``WorkflowConfig`` fields directly, so a fake must have all of
    them; methods such as ``all_plugin_tools`` are the test's to supply.
    """
    skills = cast("Mapping[str, SkillDef]", fields.pop("skills", {}))
    return _stand_in(
        WorkflowConfig,
        {
            "skills": SurfaceHolder(MappingProxyType(dict(skills))),
            "tool_surface": SurfaceHolder(()),
            **fields,
        },
    )
