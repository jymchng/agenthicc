"""The agents hub inside a session: the composer gesture, the overlay, switching (AGX-05 §5.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background.settings import BackgroundSettings
from agenthicc.runners.tui_session import SessionSwitch, TUISession
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime.commands import CommandBus, OpenAgentsHubCommand
from agenthicc.tui.workspace.agents_hub import HubResult, Tab
from agenthicc.tui.workspace.agents_hub.overlay import AgentsHubOverlay
from agenthicc.tui.workspace.overlay import OverlayHost

pytestmark = pytest.mark.unit


# ── the composer gesture ─────────────────────────────────────────────────────


def _composer() -> tuple[AppState, UnifiedInputSession, list[OpenAgentsHubCommand]]:
    state = AppState.create()
    bus = CommandBus()
    opened: list[OpenAgentsHubCommand] = []
    bus.register(OpenAgentsHubCommand, opened.append)
    return state, UnifiedInputSession(state, bus), opened


async def test_ctrl_g_and_left_on_an_empty_prompt_open_the_hub() -> None:
    _state, session, opened = _composer()
    await session._dispatch(Key.CTRL_G, "")
    await session._dispatch(Key.LEFT, "")
    assert len(opened) == 2


async def test_left_inside_text_moves_the_cursor() -> None:
    state, session, opened = _composer()
    for ch in "ab":
        await session._dispatch(Key.CHAR, ch)
    await session._dispatch(Key.LEFT, "")
    assert opened == []
    assert state.input.cursor() == 1


async def test_left_right_after_a_deletion_asks_for_a_second_press() -> None:
    state, session, opened = _composer()
    await session._dispatch(Key.CHAR, "x")
    await session._dispatch(Key.BACKSPACE, "")
    await session._dispatch(Key.LEFT, "")
    assert opened == []
    assert "Press ← again to open agents" in str(state.conversation.notification())
    await session._dispatch(Key.LEFT, "")
    assert len(opened) == 1


async def test_an_editor_without_a_hub_ignores_the_gesture() -> None:
    state = AppState.create()
    session = UnifiedInputSession(state, CommandBus())
    await session._dispatch(Key.CTRL_G, "")
    await session._dispatch(Key.LEFT, "")
    assert state.input.buf() == []


def test_the_footer_names_the_hub_key() -> None:
    from agenthicc.tui.workspace.components import FooterComponent

    hints = " ".join(FooterComponent(AppState.create()).markup_lines(200))
    assert "Ctrl+G" in hints and "agents" in hints


# ── the session ──────────────────────────────────────────────────────────────


def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TUISession:
    monkeypatch.setattr(
        "agenthicc.background.load_background_settings",
        lambda **_kwargs: BackgroundSettings(store_path=str(tmp_path / "background")),
    )
    monkeypatch.setenv("AGENTHICC_AGENTS_HUB_STATE", str(tmp_path / "hub.json"))
    state = AppState.create()
    session = TUISession.__new__(TUISession)
    exits: list[bool] = []
    session._ctx = SimpleNamespace(  # type: ignore[assignment]
        app_state=state,
        console=None,
        config_path=None,
        cfg_overrides=(),
        cfg_secret_overrides=(),
        session_id="origin-session",
    )
    redraws: list[bool] = []
    session._workspace = SimpleNamespace(  # type: ignore[assignment]
        overlays=OverlayHost(state), _redraw=lambda: redraws.append(True)
    )
    session._input_session = SimpleNamespace(request_exit=lambda: exits.append(True))  # type: ignore[assignment]
    session._agent_task = None
    session._switch = None
    session._agents_service = None
    session._agents_settings = None
    session._background_handoff_state = "idle"
    session.exits = exits  # type: ignore[attr-defined]
    return session


async def test_agents_opens_the_hub_over_the_conversation_and_esc_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, monkeypatch)
    overlays = session._workspace.overlays
    session.open_agents_hub("needs-you")
    overlay = overlays.widget
    assert isinstance(overlay, AgentsHubOverlay)
    assert overlay.hub.tab is Tab.NEEDS_YOU and overlay.hub.in_session
    await asyncio.sleep(0.05)
    session.open_agents_hub("history")  # already open: switches its tab
    assert overlays.widget is overlay and overlay.hub.tab is Tab.HISTORY
    assert overlay.handle_key(Key.ESC, "")
    assert not overlays.active
    await asyncio.sleep(0.05)
    assert overlay._task is None
    # Reopening reuses the session's agents service: no new store or executor.
    service = session._agents_service
    assert service is not None
    session.open_agents_hub("")
    reopened = overlays.widget
    assert isinstance(reopened, AgentsHubOverlay) and reopened is not overlay
    assert reopened.hub._service is service and reopened.hub.store is service.store
    assert reopened.handle_key(Key.ESC, "")
    await asyncio.sleep(0.05)
    assert not service._closed  # the hub does not close what the session owns


async def test_reopening_the_hub_selects_the_oldest_attention_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from agenthicc.background import BackgroundSession, SessionStatus
    from agenthicc.background.inbox import BackgroundInboxItem, BackgroundInteractionInbox

    session = _session(tmp_path, monkeypatch)
    service, settings = session._agents_hub_service()
    assert service is not None and settings is not None
    inbox = BackgroundInteractionInbox(service.store)

    def add_waiting_item(session_id: str, *, created_at: float) -> BackgroundInboxItem:
        service.store.create(
            BackgroundSession.create(
                session_id,
                title=session_id,
                cwd=str(tmp_path),
                workflow_name="",
                intent="",
            ).evolve(
                status=SessionStatus.RUNNING,
                worker_pid=os.getpid(),
                lease_token=f"lease-{session_id}",
                attempt=1,
            )
        )
        item = inbox.register(
            session_id=session_id,
            attempt=1,
            request_id=f"request-{session_id}",
            kind="input",
            target={"prompt": "continue?"},
            expires_at=None,
            now=created_at,
        )
        service.store.transition(
            session_id,
            SessionStatus.WAITING_INPUT,
            expected_status=SessionStatus.RUNNING,
            input_item_id=item.item_id,
        )
        inbox.activate(item.item_id)
        return item

    oldest = add_waiting_item("oldest", created_at=10.0)
    newer = add_waiting_item("newer", created_at=20.0)
    conversation = session._ctx.app_state.conversation
    conversation.background_needs_you.set(2)
    conversation.agents_target.set("oldest")
    conversation.agents_item_target.set(oldest.item_id)
    session.open_agents_hub("needs-you")
    overlay = session._workspace.overlays.widget
    assert isinstance(overlay, AgentsHubOverlay)

    try:
        await asyncio.sleep(0.05)
        await overlay.hub.settle()
        selected = overlay.hub.selected_row()
        assert selected is not None and selected.item is not None
        assert selected.item.item_id == oldest.item_id

        newer_index = next(
            index for index, row in enumerate(overlay.hub._rows) if row.session_id == "newer"
        )
        overlay.hub._select_index(newer_index)
        assert overlay.hub.selected_session_id == newer.session_id
        selected = overlay.hub.selected_row()
        assert selected is not None and selected.item is not None
        assert selected.item.item_id == newer.item_id

        session.open_agents_hub("")
        await overlay.hub.settle()
        selected = overlay.hub.selected_row()
        assert selected is not None and selected.item is not None
        assert selected.item.item_id == oldest.item_id
    finally:
        overlay.handle_key(Key.ESC, "")
        await asyncio.sleep(0.05)
        await service.close()


async def test_opening_the_hub_prioritizes_the_visible_attention_toast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background import BackgroundSession, SessionStatus

    session = _session(tmp_path, monkeypatch)
    service, settings = session._agents_hub_service()
    assert service is not None and settings is not None
    for session_id, status in (
        ("waiting", SessionStatus.WAITING_INPUT),
        ("running", SessionStatus.RUNNING),
    ):
        service.store.create(
            BackgroundSession.create(
                session_id,
                title=session_id,
                cwd=str(tmp_path),
                workflow_name="",
                intent="",
            ).evolve(status=status)
        )

    conversation = session._ctx.app_state.conversation
    conversation.background_needs_you.set(1)
    conversation.agents_target.set("waiting")
    conversation.agents_item_target.set("waiting-item")
    conversation.background_toast.set("✓ running finished")
    conversation.background_toast_kind.set("finished")
    conversation.agents_toast_target.set("running")

    session.open_agents_hub()
    overlay = session._workspace.overlays.widget
    assert isinstance(overlay, AgentsHubOverlay)

    try:
        await asyncio.sleep(0.05)
        await overlay.hub.settle()
        assert overlay.hub.tab is Tab.RUNNING
        assert overlay.hub.selected_session_id == "running"
    finally:
        overlay.handle_key(Key.ESC, "")
        await asyncio.sleep(0.05)
        await service.close()


async def test_approving_from_the_overlay_leaves_a_running_turn_and_its_transcript_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGX-05 §7: open during a turn, approve an inbox item, close; nothing else changes."""
    import os

    from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
    from agenthicc.background.inbox import BackgroundInteractionInbox

    session = _session(tmp_path, monkeypatch)
    store = BackgroundStore(tmp_path / "background")
    store.create(
        BackgroundSession.create(
            "waiting", title="Needs a decision", cwd=str(tmp_path), workflow_name="", intent="i"
        ).evolve(
            status=SessionStatus.RUNNING, worker_pid=os.getpid(), lease_token="lease", attempt=1
        )
    )
    inbox = BackgroundInteractionInbox(store)
    item = inbox.register(
        session_id="waiting",
        attempt=1,
        request_id="request-1",
        kind="approval",
        target={"tool_name": "bash", "tool_input": {"command": "pytest"}},
        expires_at=None,
    )
    store.transition(
        "waiting",
        SessionStatus.WAITING_APPROVAL,
        expected_status=SessionStatus.RUNNING,
        approval_item_id=item.item_id,
    )
    inbox.activate(item.item_id)
    conversation = session._ctx.app_state.conversation
    conversation.begin_turn("default", turn_id="turn-1")
    conversation.append_event("text", {"text": "Working on it."})
    before = [
        (turn.turn_id, [event.event_id for event in turn.events]) for turn in conversation.turns()
    ]
    turn = asyncio.get_running_loop().create_future()  # the turn still streaming
    session._agent_task = turn
    session.open_agents_hub("needs-you")
    overlay = session._workspace.overlays.widget
    assert isinstance(overlay, AgentsHubOverlay)
    await asyncio.sleep(0.05)
    await overlay.hub.settle()
    assert overlay.handle_key(Key.CHAR, "y")
    await overlay.hub.settle()
    assert inbox.get(item.item_id).status == "accepted"
    assert overlay.handle_key(Key.ESC, "")
    await asyncio.sleep(0.05)
    assert not session._workspace.overlays.active
    assert not turn.done()
    after = [
        (turn.turn_id, [event.event_id for event in turn.events]) for turn in conversation.turns()
    ]
    assert after == before
    turn.cancel()


async def test_switching_ends_the_session_with_a_switch_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, monkeypatch)
    session._switch_to(HubResult("attach", "other-agent"))
    assert session.switch == SessionSwitch(
        "attach", "other-agent", origin="origin-session", origin_cwd=str(Path.cwd())
    )
    assert session.exits == [True]  # type: ignore[attr-defined]


async def test_switching_waits_for_the_running_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, monkeypatch)
    session._agent_task = asyncio.get_running_loop().create_future()  # a turn in progress
    session._switch_to(HubResult("open_saved", "saved"))
    assert session.switch is None
    assert "Finish or stop the current turn" in str(
        session._ctx.app_state.conversation.notification()
    )


def test_background_handoff_opens_the_hub_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, monkeypatch)
    session._background_handoff_state = "started"
    switch = session.switch
    assert switch is not None and (switch.action, switch.target) == ("hub", "origin-session")


def test_agents_inbox_and_workers_commands_open_tabs() -> None:
    from agenthicc.commands.builtins import _cmd_agents, _cmd_inbox, _cmd_workers
    from agenthicc.commands.command import CommandContext

    opened: list[str] = []
    printed: list[str] = []
    console = SimpleNamespace(print=lambda text, **_kwargs: printed.append(str(text)))

    def context(args: str) -> CommandContext:
        return CommandContext(
            text=f"/x {args}",
            args=args,
            model="test-model",
            console=console,  # type: ignore[arg-type]
            config=None,  # type: ignore[arg-type]
            open_agents_hub=opened.append,
        )

    assert _cmd_agents(context(""))
    assert _cmd_agents(context("history"))
    assert _cmd_inbox(context(""))
    assert _cmd_workers(context(""))
    assert opened == ["", "history", "needs-you", "running"]
    assert _cmd_agents(context("nowhere"))
    assert "unknown tab" in printed[-1]


# ── leaving and coming back ──────────────────────────────────────────────────


async def test_a_switched_session_reopens_its_origin_when_the_target_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.runners import tui_session

    calls: list[tuple[str | None, str | None]] = []
    script = [
        SessionSwitch("attach", "target", origin="origin", origin_cwd="/origin"),
        None,  # the target ends normally
        None,  # the origin ends normally
    ]

    async def run_in(cwd: str | None, *, resume_id: str | None, **_kwargs: object) -> object:
        calls.append((resume_id, cwd))
        return script.pop(0)

    async def target_directory(action: str, target: str, **_kwargs: object) -> str:
        assert action == "attach"
        return f"/projects/{target}"

    monkeypatch.setattr(tui_session, "_run_tui_session_in", run_in)
    monkeypatch.setattr(tui_session, "_switch_target_directory", target_directory)
    await tui_session._run_tui_session(resume_id="origin")
    assert calls == [("origin", None), ("target", "/projects/target"), ("origin", "/origin")]


async def test_background_handoff_shows_the_hub_and_can_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.runners import tui_session

    calls: list[str | None] = []
    script = [SessionSwitch("hub", "bg-session", origin="bg-session", origin_cwd="/x"), None]

    async def run_in(_cwd: str | None, *, resume_id: str | None, **_kwargs: object) -> object:
        calls.append(resume_id)
        return script.pop(0)

    async def hub(session_id: str, **_kwargs: object) -> HubResult:
        assert session_id == "bg-session"
        return HubResult("attach", "bg-session")

    async def target_directory(_action: str, target: str, **_kwargs: object) -> str:
        return "/projects"

    monkeypatch.setattr(tui_session, "_run_tui_session_in", run_in)
    monkeypatch.setattr(tui_session, "_agents_hub_after_background", hub)
    monkeypatch.setattr(tui_session, "_switch_target_directory", target_directory)
    await tui_session._run_tui_session(resume_id=None)
    assert calls == [None, "bg-session"]
