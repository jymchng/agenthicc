"""COMM-05: the tools an agent calls to message another session.

``list_sessions`` and ``message_session`` are offered with the session's other tools
unless ``[sessions] agent_messaging = false``. The properties that matter: the
setting turns them on and off, sending is an ``execute`` action (so Safe asks and
Plan withholds it) while listing is a read, and a call goes through the same
messenger, with its refusals, that the integration test drives for real.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.session_messages import (
    SessionMessageError,
    SessionMessenger,
    session_message_sender,
)
from agenthicc.background.store import BackgroundStore
from agenthicc.config import AgenthiccConfig, SessionSettings, _dict_to_config
from agenthicc.runners.session_context import SessionContext
from agenthicc.runners.session_tools import session_tools
from agenthicc.tools.base import tool_name
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tui.runtime.mode_manager import builtin_mode

pytestmark = pytest.mark.unit

MESSAGING_TOOLS = {"list_sessions", "message_session"}


def _live(store: BackgroundStore, session_id: str, cwd: Path) -> BackgroundSession:
    session = BackgroundSession.create(
        session_id, title=f"title of {session_id}", cwd=str(cwd), workflow_name="", intent="work"
    ).evolve(
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token=f"lease-{session_id}",
        attempt=1,
    )
    store.create(session)
    return session


def _source(messenger: SessionMessenger | None) -> SimpleNamespace:
    """The members ``session_tools`` reads from a session."""
    return SimpleNamespace(
        approval_svc=None,
        memory_router=None,
        semantic_index=None,
        browser_tools=(),
        reload=SimpleNamespace(),
        session_messenger=messenger,
    )


def _tools(messenger: SessionMessenger | None) -> dict[str, object]:
    return {tool_name(tool): tool for tool in session_tools(_source(messenger))}  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(tmp_path / "background")


# ── the setting ──────────────────────────────────────────────────────────────


def test_agent_messaging_is_on_by_default() -> None:
    assert AgenthiccConfig().sessions == SessionSettings(agent_messaging=True)
    assert _dict_to_config({}).sessions.agent_messaging is True
    assert _dict_to_config({"sessions": {}}).sessions.agent_messaging is True


@pytest.mark.parametrize(("value", "expected"), [(False, False), ("off", False), (True, True)])
def test_the_setting_turns_agent_messaging_off_and_on(value: object, expected: bool) -> None:
    config = _dict_to_config({"sessions": {"agent_messaging": value}})
    assert config.sessions.agent_messaging is expected


def _context(tmp_path: Path, *, setting: bool, background: bool) -> SimpleNamespace:
    """The members ``SessionContext.session_messenger`` reads from its session."""
    return SimpleNamespace(
        cfg=AgenthiccConfig(sessions=SessionSettings(agent_messaging=setting)),
        background_enabled=background,
        background_store_root=tmp_path / "background",
        session_id="me",
        _workspace_root=tmp_path,
    )


def _build_messenger(context: SimpleNamespace) -> SessionMessenger | None:
    return SessionContext.session_messenger.func(context)  # type: ignore[attr-defined, no-any-return]


def test_the_context_builds_a_messenger_only_when_the_setting_and_background_sessions_are_on(
    tmp_path: Path,
) -> None:
    messenger = _build_messenger(_context(tmp_path, setting=True, background=True))
    assert isinstance(messenger, SessionMessenger) and messenger.sender_session_id == "me"
    assert _build_messenger(_context(tmp_path, setting=False, background=True)) is None
    assert _build_messenger(_context(tmp_path, setting=True, background=False)) is None


# ── what is offered ──────────────────────────────────────────────────────────


def test_a_session_with_a_messenger_offers_both_tools(
    store: BackgroundStore, tmp_path: Path
) -> None:
    tools = _tools(SessionMessenger(store, "me", project_root=tmp_path))
    assert MESSAGING_TOOLS <= set(tools)


def test_a_session_without_a_messenger_offers_neither(
    store: BackgroundStore, tmp_path: Path
) -> None:
    assert not MESSAGING_TOOLS & set(_tools(None))


# ── what they are allowed to do ──────────────────────────────────────────────


def test_listing_is_a_read_and_sending_is_an_execute_action(
    store: BackgroundStore, tmp_path: Path
) -> None:
    tools = _tools(SessionMessenger(store, "me", project_root=tmp_path))
    assert get_tool_capabilities(tools["list_sessions"]) == frozenset({ToolCapability.READ})
    assert get_tool_capabilities(tools["message_session"]) == frozenset({ToolCapability.EXECUTE})


def test_safe_mode_asks_before_a_send_plan_mode_withholds_it_and_listing_is_never_gated() -> None:
    safe, plan, yolo = (builtin_mode(name) for name in ("Safe", "Plan", "Yolo"))
    assert ToolCapability.EXECUTE in safe.approval_required
    assert ToolCapability.EXECUTE in plan.blocked_capabilities
    assert ToolCapability.EXECUTE not in yolo.approval_required | yolo.blocked_capabilities
    for mode in (safe, plan, yolo):
        gated = mode.approval_required | mode.blocked_capabilities
        assert ToolCapability.READ not in gated


# ── a call goes through the messenger ────────────────────────────────────────


async def test_list_sessions_returns_the_other_live_sessions_of_the_project(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _live(store, "me", tmp_path)
    _live(store, "peer", tmp_path)
    tools = _tools(SessionMessenger(store, "me", project_root=tmp_path))

    result = await tools["list_sessions"]()  # type: ignore[operator]

    assert [row["session_id"] for row in result["sessions"]] == ["peer"]


async def test_message_session_delivers_framed_text_naming_this_session(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _live(store, "me", tmp_path)
    peer = _live(store, "peer", tmp_path)
    tools = _tools(SessionMessenger(store, "me", project_root=tmp_path))

    result = await tools["message_session"](session_id="peer", message="the build is green")  # type: ignore[operator]

    assert result["ok"] is True and result["session_id"] == "peer"
    from agenthicc.background.input_inbox import BackgroundInputInbox

    (receipt,) = BackgroundInputInbox(store).receipts(peer.session_id)
    assert session_message_sender(receipt.text) == ("me", "title of me")
    assert receipt.text.endswith("the build is green")


async def test_message_session_raises_the_rule_that_refused_it(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _live(store, "me", tmp_path)
    tools = _tools(SessionMessenger(store, "me", project_root=tmp_path))

    with pytest.raises(SessionMessageError, match="^unknown_session:"):
        await tools["message_session"](session_id="nobody", message="hello")  # type: ignore[operator]
