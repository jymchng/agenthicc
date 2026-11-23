"""Model-facing workflow inventory, status, and deferred call requests."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Literal, TypedDict

from lauren_ai._tools import tool

from agenthicc.runners.workflow_stack import WorkflowCall, WorkflowStackError
from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tools.capabilities import tool_control, tool_read

if TYPE_CHECKING:
    from agenthicc.agents.catalog import CapabilityCatalog
    from agenthicc.runners.workflow_execution import WorkflowWorkerRuntime
    from collections.abc import Callable, Iterator

    from agenthicc.config import AgenthiccConfig
    from agenthicc.runners.workflow_stack import WorkflowStack
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.tui.runtime.mode_manager import RuntimeMode
    from agenthicc.workflows.plugin import WorkflowEntry, WorkflowPlugin
    from agenthicc.workflows.registry import WorkflowRegistry


EntryError = Literal[
    "unknown_workflow",
    "not_agent_callable",
    "not_allowed_from_frame",
    "depth_exceeded",
    "cycle",
    "ceiling_denied",
    "invalid_params",
    "handoff_already_pending",
    "denied",
]

WORKFLOW_TOOL_NAMES = frozenset({"list_workflows", "workflow_status", "enter_workflow"})


class WorkflowHandoff(BaseException):
    """A completed agent turn yielded an accepted child call to its owner."""


_FIX: dict[EntryError, str] = {
    "unknown_workflow": "Choose a workflow from list_workflows.",
    "not_agent_callable": "Choose a workflow that permits agent entry.",
    "not_allowed_from_frame": "Continue in the current frame or ask the operator to call the workflow.",
    "depth_exceeded": "Allow the current child to return before making another workflow call.",
    "cycle": "Choose a workflow outside the current stack.",
    "ceiling_denied": "Choose a workflow whose phases fit the current capability ceiling.",
    "invalid_params": "Supply a nonempty intent and valid workflow parameters.",
    "handoff_already_pending": "Finish this turn so the accepted workflow can start.",
    "denied": "Continue the task within the current frame.",
}


class EntryReceipt(TypedDict):
    status: Literal["handoff_accepted"]
    frame_id: str
    note: str


class EntryRefusal(TypedDict):
    status: Literal["rejected"]
    code: EntryError
    fix: str
    reason: str


class WorkflowListing(TypedDict):
    name: str
    when_to_use: str
    returns: str
    params: list[str]
    interactive: bool
    source: str


class FrameListing(TypedDict):
    frame_id: str
    name: str
    status: str
    phase: str | None


class ReturnListing(TypedDict):
    frame_id: str
    name: str
    status: str
    summary: str


class WorkflowStatus(TypedDict):
    frames: list[FrameListing]
    returns: list[ReturnListing]


def _refuse(code: EntryError, reason: str = "") -> EntryRefusal:
    return {"status": "rejected", "code": code, "fix": _FIX[code], "reason": reason}


def _receipt(call: WorkflowCall) -> EntryReceipt:
    return {
        "status": "handoff_accepted",
        "frame_id": call.frame_id,
        "note": "Your turn ends now. You will be resumed with the workflow’s result.",
    }


class WorkflowToolScope:
    """The workflow registry and at most one accepted call for an active turn."""

    def __init__(
        self,
        stack: WorkflowStack,
        registry: WorkflowRegistry,
        config: AgenthiccConfig,
        *,
        active_mode: Callable[[], RuntimeMode],
        approval: ApprovalBroker | None,
        headless: bool,
        worker_frame: FrameListing | None = None,
        worker_runtime: WorkflowWorkerRuntime | None = None,
        catalog: CapabilityCatalog | None = None,
    ) -> None:
        self.stack = stack
        self.registry = registry
        self._config = config
        self._active_mode = active_mode
        self._approval = approval
        self._headless = headless
        self._worker_frame = worker_frame
        self.worker_runtime = worker_runtime
        self.catalog = catalog
        self._pending: WorkflowCall | None = None
        self._lock = asyncio.Lock()

    def for_worker(self, frame_id: str, agent_type: str) -> WorkflowToolScope:
        return WorkflowToolScope(
            self.stack,
            self.registry,
            self._config,
            active_mode=self._active_mode,
            approval=None,
            headless=self._headless,
            worker_runtime=self.worker_runtime,
            catalog=self.catalog,
            worker_frame={
                "frame_id": frame_id,
                "name": agent_type,
                "status": "running",
                "phase": None,
            },
        )

    def take_pending(self) -> WorkflowCall | None:
        pending = self._pending
        self._pending = None
        return pending

    @property
    def pending(self) -> WorkflowCall | None:
        return self._pending

    @property
    def is_worker(self) -> bool:
        return self._worker_frame is not None

    @property
    def mode_name(self) -> str:
        return self._active_mode().name

    def _allows_call(self, plugin: type[WorkflowPlugin]) -> EntryRefusal | None:
        if not plugin.invocation.agent_callable:
            return _refuse("not_agent_callable")
        if self._worker_frame is not None:
            return _refuse("not_allowed_from_frame", "worker_frame")
        if self._headless and plugin.invocation.interactive:
            return _refuse("not_allowed_from_frame", "no_human_gate_available")
        parent = self.stack.top.definition
        if parent is None and self.stack.base_dispatch_name is not None:
            parent = self.registry.get(self.stack.base_dispatch_name)
        if parent is not None and not parent.invocation.allows_child_workflows:
            return _refuse("not_allowed_from_frame")
        return None

    def callable_workflows(self) -> Iterator[WorkflowEntry]:
        """Yield the workflows eligible under the current frame and ceiling."""
        if self._worker_frame is not None:
            return
        for name in sorted(self.registry.names()):
            entry = self.registry.get_entry(name)
            if entry is None or self._allows_call(entry.plugin_cls) is not None:
                continue
            workflow = entry.plugin_cls
            invocation = workflow.invocation
            call = WorkflowCall(
                workflow, "Inspect available workflow.", "agent", invocation.reentrant
            )
            try:
                self.stack.validate(call)
            except WorkflowStackError:
                continue
            yield entry

    def inventory(self) -> list[WorkflowListing]:
        rows: list[WorkflowListing] = []
        for entry in self.callable_workflows():
            workflow = entry.plugin_cls
            invocation = workflow.invocation
            try:
                params = workflow.build_params(self._config.workflows.get(workflow.name, {}))
                keys = [item.name for item in fields(params) if not item.name.startswith("_")]
            except (TypeError, ValueError):
                keys = []
            row: WorkflowListing = {
                "name": workflow.name,
                "when_to_use": invocation.when_to_use[:160],
                "returns": invocation.returns[:160],
                "params": keys[:12],
                "interactive": invocation.interactive,
                "source": entry.source,
            }
            if len(rows) == 40 or len(json.dumps([*rows, row])) > 6_000:
                break
            rows.append(row)
        return rows

    def status(self) -> WorkflowStatus:
        if self._worker_frame is not None:
            return {"frames": [self._worker_frame.copy()], "returns": []}
        return {
            "frames": [
                {
                    "frame_id": frame.frame_id,
                    "name": frame.workflow_name,
                    "status": frame.status,
                    "phase": frame.phase,
                }
                for frame in self.stack.summaries()
            ],
            "returns": [
                {
                    "frame_id": result.frame_id,
                    "name": result.workflow_name,
                    "status": result.status,
                    "summary": result.summary,
                }
                for result in self.stack.recent_returns()
            ],
        }

    async def request(
        self, name: str, intent: str, params: dict[str, str] | None
    ) -> EntryReceipt | EntryRefusal:
        async with self._lock:
            return await self._request(name, intent, params)

    async def _request(
        self, name: str, intent: str, params: dict[str, str] | None
    ) -> EntryReceipt | EntryRefusal:
        workflow = self.registry.get(name)
        if workflow is None:
            return _refuse("unknown_workflow")
        pending = self._pending
        if pending is not None and (pending.definition.name, pending.intent) == (
            workflow.name,
            intent,
        ):
            return _receipt(pending)
        refusal = self._allows_call(workflow)
        if refusal is not None:
            return refusal
        call = WorkflowCall(workflow, intent, "agent", workflow.invocation.reentrant)
        try:
            policy = self.stack.validate(call)
        except WorkflowStackError as exc:
            code: EntryError
            match exc.code:
                case "stack_depth_exceeded":
                    code = "depth_exceeded"
                case "workflow_reentrant":
                    code = "cycle"
                case "capability_denied":
                    code = "ceiling_denied"
                case "invalid_intent":
                    code = "invalid_params"
                case _:
                    code = "not_allowed_from_frame"
            return _refuse(code, str(exc))
        try:
            prepared = workflow.build_params(
                {**self._config.workflows.get(workflow.name, {}), **(params or {})}
            )
        except (TypeError, ValueError):
            return _refuse("invalid_params")
        call = replace(call, params=prepared, model_arguments=tuple(sorted((params or {}).items())))

        if pending is not None:
            return _refuse("handoff_already_pending")

        from agenthicc.runners.workflow_execution_context import effective_workflow_mode

        mode = effective_workflow_mode(self._active_mode())
        if mode.name.casefold() == "safe":
            if self._approval is None:
                return _refuse("denied", "approval_unavailable")
            approved = await self._approval.request_approval(
                ApprovalRequest(
                    tool_name="enter_workflow",
                    tool_use_id=call.frame_id,
                    tool_input={
                        "workflow": workflow.name,
                        "intent": intent,
                        "params": params or {},
                        "interactive": workflow.invocation.interactive,
                        "capability_ceiling": {
                            "blocked": sorted(policy.blocked_capabilities),
                            "requires_approval": sorted(policy.approval_required),
                        },
                    },
                    capabilities=frozenset(),
                    event=asyncio.Event(),
                    kind="workflow_enter",
                    reason="Safe mode requires approval before starting a workflow.",
                )
            )
            if not approved.allowed:
                return _refuse("denied")
        self._pending = call
        return _receipt(call)


_CURRENT: ContextVar[WorkflowToolScope | None] = ContextVar(
    "agenthicc_workflow_tools", default=None
)


def current_workflow_tools() -> WorkflowToolScope | None:
    return _CURRENT.get()


@contextmanager
def bind_workflow_tools(scope: WorkflowToolScope) -> Iterator[None]:
    token = _CURRENT.set(scope)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@tool_read
@tool()
async def list_workflows() -> list[WorkflowListing]:
    """List workflows that can be called from the current frame."""
    scope = current_workflow_tools()
    return scope.inventory() if scope is not None else []


@tool_read
@tool()
async def workflow_status() -> WorkflowStatus:
    """Show the workflow stack and its recent return summaries."""
    scope = current_workflow_tools()
    return scope.status() if scope is not None else {"frames": [], "returns": []}


@tool_control
@tool()
async def enter_workflow(
    workflow: str, intent: str, params: dict[str, str] | None = None
) -> EntryReceipt | EntryRefusal:
    """Request a workflow call when this turn finishes."""
    scope = current_workflow_tools()
    if scope is None:
        return _refuse("not_allowed_from_frame", "no_workflow_scope")
    return await scope.request(workflow, intent, params)
