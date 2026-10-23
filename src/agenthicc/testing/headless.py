"""run_headless_replay — run a recorded cassette through a workflow with no TUI.

This is the core regression-test entry point.  It creates the minimal set of
services a workflow runner needs (no Rich console, no kernel event queue, no
MCP, no TUI subscriptions), runs the named workflow against the recorded model
responses, and returns a :class:`ReplayResult` describing what happened::

    from agenthicc.testing import SessionCassette, run_headless_replay

    cassette = SessionCassette.from_path(
        "tests/fixtures/plan_mode/cassette.jsonl",
        approvals_path="tests/fixtures/plan_mode/approvals.jsonl",
        intent="enhance this repo",
    )
    result = await run_headless_replay(cassette)

    assert result.status == "complete"
    assert "finalize_plan" in result.tools_called

Pass ``hermetic=True`` to replay without side effects: every side-effecting
built-in tool is replaced by a schema-identical stub inside the tool registry
(see :mod:`agenthicc.testing.replay_tools`), no plugin, agent or MCP code is
loaded from disk, auto-compaction is disabled, and the recorded
conversation-summary calls are skipped.  A hermetic replay exercises the real
workflow state machine, tool routing, capability metadata and approval
handling, while recorded shell commands, file edits and git operations are
never executed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig
    from agenthicc.kernel.events import Event
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.tui.conversation_store import ConversationEvent
    from agenthicc.workflows.plugin import WorkflowPlugin
    from agenthicc.testing.cassette import CassetteEntry, SessionCassette
    from agenthicc.testing.replay_transport import OfferedTools


# ── ReplayResult ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolExchange:
    """One completed tool call and the result the runtime recorded for it."""

    name: str
    arguments: dict[str, object]
    result: object
    """The decoded JSON result, or the raw text when it is not JSON."""
    is_error: bool

    @property
    def accepted(self) -> bool:
        """False when the call errored or the tool answered ``{"ok": false}``."""
        if self.is_error:
            return False
        return not (isinstance(self.result, dict) and self.result.get("ok") is False)


def _decode_result(content: object) -> object:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    if isinstance(content, list):
        text = "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        return _decode_result(text)
    return content


def _tool_exchanges(messages: Sequence[Mapping[str, object]]) -> list[ToolExchange]:
    """Pair every recorded ``tool_use`` block with its ``tool_result``, in order."""
    calls: dict[str, tuple[str, dict[str, object]]] = {}
    exchanges: list[ToolExchange] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                raw_input = block.get("input")
                calls[str(block.get("id", ""))] = (
                    str(block.get("name", "")),
                    dict(raw_input) if isinstance(raw_input, Mapping) else {},
                )
            elif block.get("type") == "tool_result":
                name, arguments = calls.get(str(block.get("tool_use_id", "")), ("", {}))
                exchanges.append(
                    ToolExchange(
                        name=name,
                        arguments=arguments,
                        result=_decode_result(block.get("content")),
                        is_error=block.get("is_error") is True,
                    )
                )
    return exchanges


@dataclass
class ReplayResult:
    """Outcome of a headless cassette replay run.

    Use this for assertions in regression tests::

        assert result.status == "complete"
        assert "finalize_plan" in result.tools_called
        assert not result.contract_violations
    """

    status: str  # "complete" | "failed" | "exhausted" | "timeout" | "unknown"
    phases: list[str]  # phases that emitted WorkflowPhaseCompleted, in order
    tools_called: list[str]  # tool names from conv_store tool_complete events
    approvals_consumed: int  # number of approval requests answered
    transport_calls: int  # number of recorded responses served
    error: str | None = None  # exception text if the runner raised
    tool_outcomes: list[tuple[str, bool]] = field(default_factory=list)
    """``(tool name, succeeded)`` for every completed tool call, in order."""
    contract_violations: list[str] = field(default_factory=list)
    """Recorded calls the current runtime no longer routes or accepts."""
    exhausted: bool = False
    """True when the workflow requested more responses than were recorded."""
    skipped_entries: int = 0
    """Recorded conversation-summary calls skipped by a hermetic replay."""
    approvals_unrecorded: int = 0
    """Approval requests answered by the exhaustion fallback, not a recording."""
    tool_exchanges: list[ToolExchange] = field(default_factory=list)
    """Every tool call with its recorded result, read from the session memory."""
    context: object | None = None
    """The context returned by the runner when it finished on its own."""


# ── HeadlessProcessor ─────────────────────────────────────────────────────────


class _HeadlessProcessor:
    """No-op EventProcessor that collects emitted kernel events for assertions."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []

    async def emit(self, event: Event) -> None:
        self._events.append({"type": event.event_type, "payload": dict(event.payload)})

    def get_state(self) -> None:
        return None

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [e for e in self._events if e["type"] == event_type]


def _routing_violations(
    entries: list[CassetteEntry],
    offered: list[OfferedTools],
) -> list[str]:
    """Report recorded calls to tools that were offered then but are not now.

    A call to a name the recording never offered (a model hallucination) failed
    when it was recorded too, so it is not a regression and is not reported.
    """
    recorded = {entry.index: entry for entry in entries}
    violations: list[str] = []
    for served in offered:
        entry = recorded.get(served.entry_index)
        if entry is None:
            continue
        for name in entry.called_tool_names:
            if name in entry.tool_names_available and name not in served.names:
                violations.append(
                    f"entry {entry.index}: {name} was offered when recorded but is not offered now"
                )
    return violations


# ── run_headless_replay ───────────────────────────────────────────────────────


async def run_headless_replay(
    cassette: SessionCassette,
    *,
    workflow: str = "code_plan",
    intent: str | None = None,
    cfg: AgenthiccConfig | None = None,
    hermetic: bool = False,
    mode: str | None = None,
    timeout_s: float = 600.0,
) -> ReplayResult:
    """Run *cassette* through the built-in workflow named *workflow*.

    Parameters
    ----------
    cassette:
        A :class:`~agenthicc.testing.cassette.SessionCassette`.
    workflow:
        Name of the built-in workflow that produced the recording.
    intent:
        The user intent to pass to the runner.  Defaults to
        ``cassette.intent``; you must supply one or the other.
    cfg:
        Optional :class:`~agenthicc.config.AgenthiccConfig`.  Loaded from disk
        when ``None``.  It is never mutated.
    hermetic:
        Replace side-effecting built-in tools with schema-identical stubs in
        the tool registry, load no plugin, agent or MCP code from disk,
        disable auto-compaction, and skip recorded summary calls.
    mode:
        Runtime mode the recording ran in (``"Safe"``, ``"Plan"``, ``"Yolo"``).
        Defaults to the session default.  The mode decides which tool calls
        raise approval requests, so it must match the recording for recorded
        approval answers to line up.
    timeout_s:
        Wall-clock ceiling for the whole replay.
    """
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._signals import SignalBus

    from agenthicc.config import load_config
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.plugins.registry import override_builtin_tools
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.runners.workflow_checkpoint_store import (
        WorkflowCheckpointStore,
    )
    from agenthicc.runners.workflow_execution_context import (
        bind_workflow_execution,
    )
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.testing.replay_tools import (
        ReplayToolLog,
        hermetic_builtin_groups,
    )
    from agenthicc.tools.workspace_access import (
        WorkspaceAccessPolicy,
        WorkspaceScope,
        reset_current_workspace_access,
        set_current_workspace_access,
    )
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime.mode_manager import (
        DEFAULT_MODE_NAME,
        ModeManager,
        UnknownModeError,
    )
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.loader import (
        builtin_workflow_descriptors,
        load_builtin_workflow,
    )

    run_intent: str = intent or cassette.intent
    if not run_intent:
        raise ValueError(
            "No intent supplied.  Pass intent= to run_headless_replay() "
            "or set cassette.intent (loaded from meta.json)."
        )

    descriptor = next(
        (item for item in builtin_workflow_descriptors() if item.name == workflow),
        None,
    )
    if descriptor is None:
        available = ", ".join(item.name for item in builtin_workflow_descriptors())
        raise ValueError(f"unknown workflow {workflow!r}; built-in workflows: {available}")
    plugin_cls = load_builtin_workflow(descriptor)

    base_cfg = cfg if cfg is not None else load_config()
    run_cfg = (
        dataclasses.replace(
            base_cfg,
            execution=dataclasses.replace(base_cfg.execution, auto_compact=False),
        )
        if hermetic
        else base_cfg
    )

    # ── Minimal services ──────────────────────────────────────────────────────
    replay_entries = cassette.replay_entries(skip_tool_free=hermetic)
    transport = cassette.to_replay_transport(skip_tool_free=hermetic)
    approvals = cassette.to_mock_approval_service()
    processor = _HeadlessProcessor()
    app_state = AppState.create()
    # A real mode manager lets phase mode overrides (for example a Plan-mode
    # workflow switching to Yolo for execution) behave as in a live session.
    try:
        mode_manager = ModeManager(app_state=app_state, default_name=mode or DEFAULT_MODE_NAME)
    except UnknownModeError as exc:
        raise ValueError(f"unknown mode {mode!r}") from exc
    agent_runner = AgentRunnerBase(transport=transport, signals=SignalBus())
    tool_log = ReplayToolLog(current_entry=lambda: transport.last_served_index)

    try:
        from agenthicc.agents.registry import build_agents_registry

        # A hermetic replay must not import user or project agent modules.
        agents_registry = build_agents_registry(
            project_dir=Path(".agenthicc"),
            user_dir=Path.home() / ".agenthicc",
            load_external=not hermetic,
        )
    except Exception:  # noqa: BLE001
        agents_registry = None

    workspace_scope = WorkspaceScope.create(
        Path.cwd(), allowed_paths=run_cfg.security.allowed_paths
    )
    workspace_access = WorkspaceAccessPolicy(
        workspace_scope,
        mode_provider=app_state.active_mode,
        approval_service=approvals,
    )

    # ── Collect tool_complete events from conv_store ──────────────────────────
    tool_outcomes: list[tuple[str, bool]] = []

    def _record_event(ev: ConversationEvent) -> None:
        if ev.kind == "tool_complete":
            name = ev.payload.get("name")
            if isinstance(name, str):
                tool_outcomes.append((name, ev.payload.get("success") is True))

    # A live session owns a journaled conversation and a durable checkpoint
    # store; workflows such as goal_flow refuse state changes without one.
    # The replay's copies live in a private directory that is removed after
    # the run, never in the working directory or ~/.agenthicc.
    with tempfile.TemporaryDirectory(prefix="agenthicc-replay-") as state_dir:
        state_root = Path(state_dir)
        conversation_id = f"replay-{uuid.uuid4().hex}"
        conversation = SessionConversation.open(
            conversation_id,
            max_tokens=run_cfg.execution.effective_usable_budget(),
            journal_path=state_root / "conversation.jsonl",
        )
        handle = WorkflowRunHandle.create(
            run_id=uuid.uuid4().hex,
            workflow=plugin_cls,
            conversation=conversation,
            intent=run_intent,
            checkpoint_store=WorkflowCheckpointStore(conversation_id, root=state_root),
            workspace_root=str(workspace_scope.primary_root),
        )
        _bootstrap_handle(handle, plugin_cls, run_intent, conversation)

        wf_config = WorkflowConfig(
            conv_store=app_state.conversation,
            app_state=app_state,
            processor=cast("EventProcessor", processor),
            agent_runner=agent_runner,
            approval_svc=approvals,
            cfg=run_cfg,
            skills={},
            plugin_tools=[],
            mcp_registry=None,
            mention_cache=MentionCache(),
            agents_registry=agents_registry,  # type: ignore[arg-type]
            params=plugin_cls.build_params(run_cfg.workflows.get(workflow, {})),
            workspace_scope=workspace_scope,
            workspace_access=workspace_access,
            session_memory=conversation.memory,
            conversation_id=conversation_id,
            workflow_handle=handle,
            terminal_wait_policies={
                phase.name: phase.terminal_wait_policy for phase in plugin_cls.phases
            },
        )

        # ── Run ───────────────────────────────────────────────────────────────
        runner = plugin_cls.build_runner(wf_config, mode_manager)
        status = "unknown"
        error: str | None = None
        context: object | None = None
        unsubscribe = app_state.conversation.on_event(_record_event)
        workspace_token = set_current_workspace_access(workspace_access)
        # The run task copies the current context when it is created, so the
        # workflow binding and a hermetic catalog override stay installed for
        # every turn it runs.
        catalog: contextlib.AbstractContextManager[None] = (
            override_builtin_tools(hermetic_builtin_groups(tool_log))
            if hermetic
            else contextlib.nullcontext()
        )
        with catalog, bind_workflow_execution(handle.run_id):
            run_task: asyncio.Task[object] = asyncio.create_task(runner.run(run_intent))
        exhausted_task = asyncio.create_task(transport.exhausted.wait())
        try:
            done, _pending = await asyncio.wait(
                {run_task, exhausted_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if run_task in done:
                try:
                    context = run_task.result()
                    wf_run = app_state.workflow_run()
                    status = (wf_run.status if wf_run is not None else "") or "complete"
                except Exception as exc:  # noqa: BLE001
                    status = "failed"
                    error = repr(exc)
            else:
                status = "exhausted" if exhausted_task in done else "timeout"
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
        finally:
            exhausted_task.cancel()
            await asyncio.gather(exhausted_task, return_exceptions=True)
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            unsubscribe()
            reset_current_workspace_access(workspace_token)
            handle.release_claim()
        exchanges = _tool_exchanges(conversation.memory.messages())

    # ── Collect phases from kernel events ────────────────────────────────────
    phases = [
        str(payload.get("phase_name", ""))
        for e in processor.events_of_type("WorkflowPhaseCompleted")
        if isinstance((payload := e["payload"]), dict) and payload.get("phase_name")
    ]

    return ReplayResult(
        status=status,
        phases=phases,
        tools_called=[name for name, _ok in tool_outcomes],
        approvals_consumed=approvals.consumed,
        transport_calls=transport.served,
        error=error,
        tool_outcomes=tool_outcomes,
        contract_violations=[
            *tool_log.violations,
            *_routing_violations(replay_entries, transport.offered),
        ],
        exhausted=status == "exhausted",
        skipped_entries=len(cassette.entries) - len(replay_entries),
        approvals_unrecorded=approvals.unrecorded,
        tool_exchanges=exchanges,
        context=context,
    )


def _bootstrap_handle(
    handle: WorkflowRunHandle,
    plugin_cls: type[WorkflowPlugin],
    intent: str,
    conversation: SessionConversation,
) -> None:
    """Give *handle* the durable identity a live session establishes first.

    Mirrors the headless session owner: an identity-only bootstrap context, a
    run claim, the first phase checkpoint, and the plugin's optional typed
    initial context.
    """
    from agenthicc.workflows.plugin import WorkflowContext

    first_phase = plugin_cls.phases[0].name if plugin_cls.phases else None
    handle.attach_bootstrap_context(
        WorkflowContext(
            intent=intent,
            run_id=handle.run_id,
            workflow_name=plugin_cls.name,
            current_phase=first_phase,
        )
    )
    handle.claim(f"replay:{handle.run_id}")
    if first_phase is None:
        handle.save_checkpoint(reason="started")
        return
    handle.update_phase(first_phase, 0, 0)
    initial_context = plugin_cls.create_initial_context(intent, handle.run_id, conversation.memory)
    if initial_context is not None:
        handle.attach_context(initial_context)
        handle.update_phase(first_phase, 0, 0)
