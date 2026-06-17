"""run_headless_replay — run a cassette through CodePlanRunner with no TUI.

This is the core regression-test entry point.  It creates the minimal set of
services needed by CodePlanRunner (no Rich console, no kernel event queue, no
MCP, no TUI AppState subscriptions), runs the workflow, and returns a
:class:`ReplayResult` describing what happened::

    from agenthicc.testing import SessionCassette, run_headless_replay

    cassette = SessionCassette.from_path(
        "tests/fixtures/plan_mode/cassette.jsonl",
        approvals_path="tests/fixtures/plan_mode/approvals.jsonl",
        intent="enhance this repo",
    )
    result = await run_headless_replay(cassette)

    assert result.status == "complete"
    assert "finalize_plan" in result.tools_called
    assert result.phases == ["plan", "execute", "review", "summarize"]
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agenthicc.testing.cassette import SessionCassette
    from agenthicc.testing.mock_approval import MockApprovalService


# ── ReplayResult ──────────────────────────────────────────────────────────────

@dataclass
class ReplayResult:
    """Outcome of a headless cassette replay run.

    Use this for assertions in regression tests::

        assert result.status == "complete"
        assert "finalize_plan" in result.tools_called
        assert result.phases == ["plan", "execute", "review", "summarize"]
    """
    status:             str               # "complete" | "failed" | "unknown"
    phases:             list[str]         # phases that emitted PhaseCompleted, in order
    tools_called:       list[str]         # tool names from conv_store tool_complete events
    approvals_consumed: int               # number of approval requests dequeued
    transport_calls:    int               # number of transport.complete() calls made
    error:              str | None = None  # exception text if the runner raised


# ── HeadlessProcessor ─────────────────────────────────────────────────────────

class _HeadlessProcessor:
    """No-op EventProcessor that collects emitted kernel events for assertions."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    async def emit(self, event: Any) -> None:
        self._events.append({
            "type": getattr(event, "event_type", ""),
            "payload": dict(getattr(event, "payload", {}) or {}),
        })

    def get_state(self) -> None:
        return None

    def events_of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self._events if e["type"] == event_type]


# ── run_headless_replay ───────────────────────────────────────────────────────

async def run_headless_replay(
    cassette: SessionCassette,
    *,
    intent: str | None = None,
    cfg: Any = None,
) -> ReplayResult:
    """Run *cassette* through :class:`~agenthicc.workflows.code_plan.CodePlanRunner`.

    Parameters
    ----------
    cassette:
        A :class:`~agenthicc.testing.cassette.SessionCassette` loaded via
        :meth:`~agenthicc.testing.cassette.SessionCassette.from_path` or
        :meth:`~agenthicc.testing.cassette.SessionCassette.from_session`.
    intent:
        The user intent string to pass to the runner.  Defaults to
        ``cassette.intent``; you must supply one or the other.
    cfg:
        Optional :class:`~agenthicc.config.AgenthiccConfig`.  Loaded from
        disk when ``None`` (uses the project's ``agenthicc.toml``).

    Returns
    -------
    ReplayResult
        Structured outcome describing status, phases, tool calls, etc.
    """
    from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
    from lauren_ai._signals import SignalBus                # noqa: PLC0415

    from agenthicc.config import load_config                # noqa: PLC0415
    from agenthicc.mentions.cache import MentionCache       # noqa: PLC0415
    from agenthicc.tui.conversation_store import AppState   # noqa: PLC0415
    from agenthicc.workflows.code_plan import CodePlanRunner # noqa: PLC0415
    from agenthicc.workflows.config import WorkflowConfig   # noqa: PLC0415

    run_intent: str = intent or cassette.intent
    if not run_intent:
        raise ValueError(
            "No intent supplied.  Pass intent= to run_headless_replay() "
            "or set cassette.intent (loaded from meta.json)."
        )

    if cfg is None:
        cfg = load_config()

    # ── Minimal services ──────────────────────────────────────────────────────
    mock_transport = cassette.to_mock_transport()
    mock_approval  = cassette.to_mock_approval_service()
    processor      = _HeadlessProcessor()
    app_state      = AppState.create()
    agent_runner   = AgentRunnerBase(transport=mock_transport, signals=SignalBus())

    # Build AgentsRegistry (lightweight, reads config files only)
    try:
        from agenthicc.agents.registry import build_agents_registry  # noqa: PLC0415
        agents_registry = build_agents_registry(
            project_dir=Path(".agenthicc"),
            user_dir=Path.home() / ".agenthicc",
        )
    except Exception:  # noqa: BLE001
        agents_registry = None  # type: ignore[assignment]

    mention_cache = MentionCache()

    wf_config = WorkflowConfig(
        conv_store=app_state.conversation,
        app_state=app_state,
        processor=processor,      # type: ignore[arg-type]  # _HeadlessProcessor satisfies emit()
        agent_runner=agent_runner,
        approval_svc=mock_approval,   # type: ignore[arg-type]
        cfg=cfg,
        skills={},
        plugin_tools=[],
        mcp_registry=None,
        mention_cache=mention_cache,
        agents_registry=agents_registry,   # type: ignore[arg-type]
    )

    # ── Collect tool_complete events from conv_store ──────────────────────────
    tools_called: list[str] = []

    def _record_event(ev: Any) -> None:
        if getattr(ev, "kind", "") == "tool_complete":
            tools_called.append(ev.payload.get("name", ""))

    _unsub = app_state.conversation.on_event(_record_event)

    # ── Run ───────────────────────────────────────────────────────────────────
    runner = CodePlanRunner(wf_config, mode_manager=None)
    status: str = "unknown"
    error:  str | None = None

    try:
        await runner.run(run_intent)
        wf_run = app_state.workflow_run()
        status = str(getattr(wf_run, "status", "complete") or "complete")
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:
        status = "failed"
        error  = repr(exc)
    finally:
        _unsub()

    # ── Collect phases from kernel events ────────────────────────────────────
    phases = [
        str(e["payload"].get("phase_name", ""))
        for e in processor.events_of_type("WorkflowPhaseCompleted")
        if e["payload"].get("phase_name")
    ]

    return ReplayResult(
        status=status,
        phases=phases,
        tools_called=tools_called,
        approvals_consumed=mock_approval.consumed,
        transport_calls=len(mock_transport.calls),
        error=error,
    )
