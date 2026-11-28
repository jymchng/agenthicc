"""COMM-05: two real session runtimes, one agent messaging the other.

Two background workers run in one process on one project, each with a scripted
provider. The sender's agent lists the sessions it can message and sends to the
receiver with ``message_session``. The receiver is a different runtime with its own
provider: it must see the message framed as untrusted context from the sender, and
when its own agent then tries to answer into the same exchange the loop rule refuses
it, although the sender is alive and so nothing else would.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agenthicc.background import BackgroundInteractionInbox, BackgroundSession, BackgroundStore
from agenthicc.background.input_inbox import BackgroundInputInbox
from agenthicc.background.model import SessionStatus
from agenthicc.background.session_messages import (
    SESSION_MESSAGE_HEADER,
    SessionMessageError,
    SessionMessenger,
)
from agenthicc.background.worker import WorkerRequest, run_worker

from .test_background_session_tools import _production_builder

pytestmark = pytest.mark.integration

SENDER = "sender-session"
RECEIVER = "receiver-session"
MESSAGE = "the cache key changed, rebuild before you test"
ASK = {"questions": [{"id": "go", "text": "Proceed?", "options": ["yes", "no"]}]}


def _done(text: str) -> object:
    from lauren_ai._transport import Completion, TokenUsage

    return Completion(
        id="done",
        model="mock-model",
        content=text,
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
        tool_calls=[],
    )


def _request_text(call: object) -> str:
    return json.dumps(call.messages, default=str)  # type: ignore[attr-defined]


def _tool_results(call: object) -> list[str]:
    results: list[str] = []
    for message in call.messages:  # type: ignore[attr-defined]
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            results += [
                str(block.get("content"))
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
    return results


def _create(store: BackgroundStore, session_id: str, workspace: Path, tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / session_id
    artifact.mkdir(parents=True)
    store.create(
        BackgroundSession.create(
            session_id,
            title=f"{session_id} title",
            cwd=str(workspace),
            workflow_name="",
            intent="work with the other session",
            artifact_dir=str(artifact),
        )
    )


def _request(session_id: str, workspace: Path, store_root: Path) -> WorkerRequest:
    return WorkerRequest(
        session_id=session_id,
        workflow_name="",
        intent="work with the other session",
        cwd=str(workspace),
        config_path=None,
        # Where the workers' contexts find the store, as in production: the setting.
        set_overrides=(f"background.store_path={store_root}",),
        dangerously_skip_permissions=False,
        mode_name="Yolo",
    )


async def _until_asking(store: BackgroundStore, session_id: str) -> None:
    for _ in range(1_500):
        current = store.get(session_id)
        if current.status is SessionStatus.WAITING_INPUT and current.input_item_id:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{session_id} never asked its question")


def _answer(store: BackgroundStore, session_id: str) -> None:
    inbox = BackgroundInteractionInbox(store)
    item = inbox.get(store.get(session_id).input_item_id)
    inbox.respond(
        item.item_id,
        {"answers": {"go": "yes"}},
        expected_version=item.version,
        idempotency_key=f"answer-{session_id}",
    )


async def test_an_agent_messages_another_live_session_and_the_loop_rule_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport._mock import MockTransport

    receiver_transport, sender_transport = MockTransport(), MockTransport()
    # The receiver asks the operator a question (so it stays alive), then answers the
    # sender's message; the sender lists, sends, then asks (so it stays alive too).
    receiver_transport.queue_tool_use("ask_user", ASK, tool_use_id="ask-r", model="mock-model")
    receiver_transport.queue_tool_use(
        "message_session",
        {"session_id": SENDER, "message": "thanks, rebuilding", "reply_to": "m"},
        tool_use_id="reply-r",
        model="mock-model",
    )
    receiver_transport.queue_response(_done("Rebuilding."))
    receiver_transport.queue_response(_done("Nothing more."))
    sender_transport.queue_tool_use("list_sessions", {}, tool_use_id="list-s", model="mock-model")
    sender_transport.queue_tool_use(
        "message_session",
        {"session_id": RECEIVER, "message": MESSAGE},
        tool_use_id="send-s",
        model="mock-model",
    )
    sender_transport.queue_tool_use("ask_user", ASK, tool_use_id="ask-s", model="mock-model")
    sender_transport.queue_response(_done("Told the receiver."))

    runners = [
        AgentRunnerBase(transport=receiver_transport),
        AgentRunnerBase(transport=sender_transport),
    ]
    workspace = _production_builder(monkeypatch, tmp_path, runners[0])
    # One runner per runtime, in the order the two contexts are built: receiver first.
    from agenthicc.runners import tui_session

    monkeypatch.setattr(tui_session, "_build_agent_runner", lambda *a, **k: runners.pop(0))
    # A custom store root, set the way an operator sets it. Each worker's context must find
    # its peers there and not in the default location.
    store_root = tmp_path / "custom-background-store"
    store = BackgroundStore(store_root)
    _create(store, RECEIVER, workspace, tmp_path)
    _create(store, SENDER, workspace, tmp_path)

    receiver = asyncio.create_task(run_worker(_request(RECEIVER, workspace, store_root), store))
    await _until_asking(store, RECEIVER)
    sender = asyncio.create_task(run_worker(_request(SENDER, workspace, store_root), store))
    await _until_asking(store, SENDER)

    # The sender's agent sent while both runtimes were alive: the message is queued
    # in the receiver's durable inbox, framed, and has not been read yet.
    (queued,) = BackgroundInputInbox(store).receipts(RECEIVER)
    assert queued.text.startswith(SESSION_MESSAGE_HEADER)

    _answer(store, RECEIVER)
    assert await asyncio.wait_for(receiver, timeout=60) == 0, store.get(RECEIVER).error
    _answer(store, SENDER)
    assert await asyncio.wait_for(sender, timeout=60) == 0, store.get(SENDER).error

    # The sender was offered the tools with no setting changed (on by default), and
    # its list showed the receiver and not itself.
    offered = {tool["name"] for tool in sender_transport.calls[0].tools or []}
    assert {"list_sessions", "message_session"} <= offered
    listing = _tool_results(sender_transport.calls[1])[-1]
    assert RECEIVER in listing and SENDER not in listing
    sent = _tool_results(sender_transport.calls[2])[-1]
    assert json.loads(sent)["ok"] is True

    # The receiver, a different runtime, read it as untrusted context from the sender.
    seen = [_request_text(call) for call in receiver_transport.calls]
    carrying = [text for text in seen if SESSION_MESSAGE_HEADER in text]
    assert carrying, "the receiver's provider never saw the message"
    assert f"From session {SENDER}" in carrying[0] and MESSAGE in carrying[0]
    assert f"Origin chain: {SENDER}" in carrying[0]

    # Its agent answered into the same exchange: refused as a loop, though the sender
    # is alive. Nothing was queued for the sender.
    refusals = [r for call in receiver_transport.calls for r in _tool_results(call) if "loop:" in r]
    assert refusals, [_tool_results(call) for call in receiver_transport.calls]
    assert BackgroundInputInbox(store).receipts(SENDER) == ()

    # Both runtimes have finished, and a message never starts a finished session.
    with pytest.raises(SessionMessageError, match="^not_live:"):
        SessionMessenger(store, SENDER, project_root=workspace).send(RECEIVER, "one more thing")
