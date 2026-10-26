"""A worker's report line and usage in its session record (AGX-06 §5.2, AGX-07 §5.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.vocabulary import report_sentence
from agenthicc.background.worker import (
    WorkerRequest,
    _finalize_worker,
    _WorkerOutcome,
    _WorkerReport,
)
from agenthicc.runners.usage_ledger import UsageLedger
from agenthicc.tui.conversation_store import AppState, ConversationEvent, ConversationStore
from tests.session_fakes import session_context

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str) -> BackgroundSession:
    artifact = tmp_path / "sessions" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(tmp_path),
        workflow_name="",
        intent="work",
        artifact_dir=str(artifact),
    )


def _usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def _completed(outcome_status: SessionStatus = SessionStatus.COMPLETED) -> _WorkerOutcome:
    return _WorkerOutcome(
        status=outcome_status,
        error=None if outcome_status is SessionStatus.COMPLETED else "boom",
        activity="Turn complete" if outcome_status is SessionStatus.COMPLETED else "Worker failed",
        exit_reason="turn_complete",
        exit_code=0 if outcome_status is SessionStatus.COMPLETED else 1,
    )


# ── the report line ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fixed the flaky test. Next I ran the suite.", "Fixed the flaky test."),
        ("All tests pass", "All tests pass"),
        ("## Summary\n\nI fixed it! Then more.", "I fixed it!"),
        ("## Summary\n", "Summary"),
        ("```python\nprint(1)\n```\n- Updated the parser", "Updated the parser"),
        ("---\n> Quoted reply here. More.", "Quoted reply here."),
        ("1. Renamed the module. 2. Then", "Renamed the module."),
        ("Done.\nSecond line", "Done."),
        ("", ""),
        ("   \n\n", ""),
    ],
)
def test_report_sentence_takes_the_first_sentence_of_prose(text: str, expected: str) -> None:
    assert report_sentence(text) == expected


def test_report_sentence_is_one_bounded_line() -> None:
    line = report_sentence("word " * 100, limit=20)
    assert len(line) == 20 and line.endswith("…") and "\n" not in line


# ── completed usage only ─────────────────────────────────────────────────────


def test_completed_subscribers_see_only_committed_totals() -> None:
    ledger = UsageLedger("ledger-session")
    seen: list[tuple[int, int, int]] = []
    unsubscribe = ledger.subscribe_completed(
        lambda snapshot: seen.append(
            (snapshot.input_tokens, snapshot.output_tokens, snapshot.provisional_calls)
        )
    )
    call = ledger.begin_call(run_id="run")
    ledger.observe(call, _usage(10, 2))
    assert seen == []  # streamed values are provisional
    assert ledger.snapshot().input_tokens == 10
    assert ledger.completed_snapshot().input_tokens == 0
    ledger.complete(call, _usage(12, 5))
    assert seen == [(12, 5, 0)]
    ledger.complete(call, _usage(99, 99))  # a repeated completion is a no-op
    assert seen == [(12, 5, 0)]
    other = ledger.begin_call(run_id="run")
    ledger.observe(other, _usage(1, 1))
    assert ledger.completed_snapshot().input_tokens == 12
    unsubscribe()
    ledger.complete(other, _usage(3, 4))
    assert seen == [(12, 5, 0)]


# ── the worker's report ──────────────────────────────────────────────────────


def _context(ledger: UsageLedger | None) -> SimpleNamespace:
    app_state = AppState.create()
    app_state.conversation = ConversationStore()
    return session_context(app_state=app_state, usage_ledger=ledger)


def test_report_follows_assistant_text_redacted() -> None:
    report = _WorkerReport(
        BackgroundSession.create("s", title="s", cwd="/", workflow_name="", intent="i")
    )
    context = _context(None)
    unbind = report.bind(context)  # type: ignore[arg-type]
    conversation = context.app_state.conversation
    conversation.append_event("tool_call", {"name": "bash"})
    assert report.line == ""
    conversation.append_event(
        "text", {"text": "Set token=sk-live-123456 for the deploy. Then ran it."}
    )
    assert report.line.startswith("Set token=") and "sk-live-123456" not in report.line
    unbind()
    conversation.append_event("text", {"text": "Ignored after unbinding."})
    assert "Ignored" not in report.line


def test_report_records_usage_once_per_change() -> None:
    session = BackgroundSession.create("s", title="s", cwd="/", workflow_name="", intent="i")
    report = _WorkerReport(session)
    ledger = UsageLedger("s")
    report.bind(_context(ledger))  # type: ignore[arg-type]
    assert report.usage() is None  # nothing new to record
    call = ledger.begin_call(run_id="run")
    ledger.observe(call, _usage(5, 5))
    assert report.usage() is None
    ledger.complete(call, _usage(100, 20), cost_usd=0.1234567)
    usage = report.usage()
    assert usage == (100, 20, 0.123457)
    report.recorded(usage)
    assert report.usage() is None


def test_report_starts_from_totals_of_earlier_attempts() -> None:
    session = BackgroundSession.create("s", title="s", cwd="/", workflow_name="", intent="i")
    ledger = UsageLedger("s")
    call = ledger.begin_call(run_id="earlier")
    ledger.complete(call, _usage(40, 2))
    report = _WorkerReport(session)
    report.bind(_context(ledger))  # type: ignore[arg-type]
    assert report.usage() == (40, 2, 0.0)  # a record written before AGX-07 catches up


def test_heartbeat_writes_usage_as_a_delta(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "beat"))
    store.claim("beat", pid=1, lease_token="lease")
    store.heartbeat("beat", lease_token="lease", tokens_in=7, tokens_out=3, cost_usd=0.02)
    store.heartbeat("beat", lease_token="lease", activity="Still going")
    session = store.get("beat")
    assert (session.tokens_in, session.tokens_out, session.cost_usd) == (7, 3, 0.02)
    last = store._read_events()[-1]
    assert "tokens_in" not in last.get("changes", {})  # unchanged totals are not rewritten


def test_finalizer_keeps_the_report_line_and_usage(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for session_id in ("done", "broken"):
        store.create(_session(tmp_path, session_id))
        store.claim(session_id, pid=1, lease_token="lease")
    for session_id, status in (("done", SessionStatus.COMPLETED), ("broken", SessionStatus.FAILED)):
        report = _WorkerReport(store.get(session_id))
        report.line = "Fixed the flaky scheduler test."
        ledger = UsageLedger(session_id)
        call = ledger.begin_call(run_id="run")
        ledger.complete(call, _usage(1200, 300), cost_usd=0.18)
        report.bind(_context(ledger))  # type: ignore[arg-type]
        _finalize_worker(
            store,
            WorkerRequest(session_id, "", "work", str(tmp_path), None, (), False),
            lease_token="lease",
            expected_attempt=store.get(session_id).attempt,
            outcome=_completed(status),
            worker_pid=1,
            report=report,
        )
    done, broken = store.get("done"), store.get("broken")
    assert done.status is SessionStatus.COMPLETED
    assert done.latest_activity == "Fixed the flaky scheduler test."
    assert done.exit_reason == "Turn complete"
    assert (done.tokens_in, done.tokens_out, done.cost_usd) == (1200, 300, 0.18)
    assert broken.status is SessionStatus.FAILED
    assert broken.latest_activity == "Worker failed"  # a failure keeps its outcome
    assert broken.tokens_in == 1200


@pytest.mark.asyncio
async def test_worker_records_its_report_line_and_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agenthicc.background.worker as worker
    import agenthicc.runners.headless as headless
    import agenthicc.runners.tui_session as tui_session
    import agenthicc.tui.runtime.session_log as session_log

    stop = asyncio.Event()

    class Processor:
        async def run(self) -> None:
            await stop.wait()

        async def drain(self) -> None:
            return None

    ledger = UsageLedger("reporting")
    context = _context(ledger)
    context.processor = Processor()
    context.cfg = SimpleNamespace()
    context.mode_manager = SimpleNamespace(active_name="Safe")
    context.app_state.workflow_run = lambda: None

    async def build(*args: object, **kwargs: object) -> SimpleNamespace:
        return context

    async def close(*args: object, **kwargs: object) -> None:
        stop.set()

    async def direct(*args: object, **kwargs: object) -> None:
        conversation = context.app_state.conversation
        conversation.begin_turn("default", turn_id="turn-1")
        call = ledger.begin_call(run_id="turn-1")
        ledger.complete(call, _usage(900, 100), cost_usd=0.05)
        conversation.append_event("text", {"text": "Renamed the scheduler module. Tests pass."})
        conversation.close_turn()

    monkeypatch.setattr(tui_session, "_build_session_context", build)
    monkeypatch.setattr(headless, "_close_headless_session", close)
    monkeypatch.setattr(session_log, "register_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_run_direct_turn", direct)

    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "reporting"))
    request = WorkerRequest("reporting", "", "work", str(tmp_path), None, (), False)
    assert await worker.run_worker(request, store) == 0
    session = store.get("reporting")
    assert session.status is SessionStatus.COMPLETED
    assert session.latest_activity == "Renamed the scheduler module."
    assert (session.tokens_in, session.tokens_out, session.cost_usd) == (900, 100, 0.05)


def test_conversation_event_type_is_what_the_report_reads() -> None:
    event = ConversationEvent(event_id="e", kind="text", payload={"text": "Hello. World."})
    report = _WorkerReport(
        BackgroundSession.create("s", title="s", cwd="/", workflow_name="", intent="i")
    )
    report.observe_event(event)
    assert report.line == "Hello."
