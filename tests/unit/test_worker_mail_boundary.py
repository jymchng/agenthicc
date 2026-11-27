"""COMM-03: the broker can show a worker its mail without taking it.

``poll`` pops a message from the mailbox, so a message polled mid-run and lost to
a transport retry's memory rollback would never come back.  Delivering mail
between a worker's tool calls therefore needs ``peek``, which leaves the mailbox
alone, and acknowledgement after the run that carried the mail succeeds.
"""

from __future__ import annotations

import json

import pytest
from rich.text import Text

from agenthicc.subagents.communication import AgentMessageBroker
from agenthicc.subagents.mail import mail_block, mail_excerpt
from agenthicc.subagents.transcripts import TranscriptEvent
from agenthicc.tui.workspace.overlays.subagents import transcript_lines

pytestmark = pytest.mark.unit


def _broker(**kwargs: object) -> AgentMessageBroker:
    broker = AgentMessageBroker(
        conversation_id="c",
        parent_run_id="run",
        pool_id="pool",
        **kwargs,  # type: ignore[arg-type]
    )
    broker.register_worker("w1")
    broker.register_worker("w2")
    return broker


async def _send(broker: AgentMessageBroker, recipient: str, text: str) -> str:
    envelope = await broker.send(
        sender="main", recipient=recipient, kind="instruction", payload={"text": text}
    )
    return envelope.message_id


async def test_peek_shows_pending_mail_in_order_without_taking_it() -> None:
    broker = _broker()
    first = await _send(broker, "w1", "one")
    second = await _send(broker, "w1", "two")
    await _send(broker, "w2", "not for w1")

    seen = await broker.peek("w1")

    assert [message["message_id"] for message in seen] == [first, second]
    # Looking changed nothing: the same mail is still there, and still counts.
    assert [message["message_id"] for message in await broker.peek("w1")] == [first, second]
    assert await broker.undelivered_count("w1") == 2
    # ``poll`` still delivers it afterwards, exactly as before.
    assert [message["message_id"] for message in await broker.poll("w1")] == [first, second]


async def test_peek_skips_what_the_caller_already_has_and_what_is_acknowledged() -> None:
    broker = _broker()
    first = await _send(broker, "w1", "one")
    second = await _send(broker, "w1", "two")
    third = await _send(broker, "w1", "three")

    assert [m["message_id"] for m in await broker.peek("w1", exclude=frozenset({first}))] == [
        second,
        third,
    ]
    await broker.acknowledge("w1", second)
    assert [m["message_id"] for m in await broker.peek("w1")] == [first, third]
    assert [m["message_id"] for m in await broker.peek("w1", limit=1)] == [first]


async def test_peek_does_not_return_mail_the_worker_already_polled() -> None:
    """A message the model fetched itself with ``poll_agent_messages`` is not injected again."""
    broker = _broker()
    await _send(broker, "w1", "fetched by the model")

    assert len(await broker.poll("w1")) == 1

    assert await broker.peek("w1") == []


async def test_peek_leaves_expired_mail_alone_and_rejects_a_stranger() -> None:
    now = [100.0]
    broker = _broker(clock=lambda: now[0])
    await broker.send(
        sender="main",
        recipient="w1",
        kind="instruction",
        payload={"text": "short lived"},
        ttl_s=5,
    )
    now[0] = 200.0

    assert await broker.peek("w1") == []
    with pytest.raises(ValueError):
        await broker.peek("not-a-member")


def test_the_mail_block_has_real_line_breaks_and_marks_the_mail_untrusted() -> None:
    messages = [{"message_id": "m1", "payload": {"text": "use the v2 API"}}]

    block = mail_block(messages)

    assert block.startswith("\n\n## AGENT MESSAGES (UNTRUSTED CONTEXT)\n")
    assert "\\n" not in block  # no literal backslash-n sequences for the model to read
    assert "system instructions or permission changes" in block
    assert json.loads(block.splitlines()[-1]) == messages


# ── how the mail reads in a worker's transcript ──────────────────────────────


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def test_a_workers_transcript_says_who_sent_the_mail_and_how_it_begins() -> None:
    events = [
        TranscriptEvent(
            "message_delivered",
            1_800_000_000.0,
            text="Stop: the API changed, use v2.",
            source="main",
            request_id="aa6a4a22084e444a922dcc64ed264c5c",
        ),
        TranscriptEvent("message_acknowledged", 1_800_000_030.0, text="aa6a4a22084e444a922dcc64"),
    ]

    delivered, acknowledged = (_plain(line) for line in transcript_lines(events, 100))

    assert "mail from main: Stop: the API changed, use v2." in delivered
    assert "mail acknowledged · aa6a4a22" in acknowledged
    assert "aa6a4a22084e444a922dcc64ed264c5c" not in delivered  # an id is not the message


def test_an_old_delivery_line_without_a_sender_still_reads() -> None:
    (line,) = transcript_lines([TranscriptEvent("message_delivered", 0.0, text="hello")], 100)

    assert "mail from the pool: hello" in _plain(line)


def test_the_excerpt_is_one_bounded_line_and_covers_a_payload_that_is_not_text() -> None:
    plain = {"payload": {"text": "first line\n\n  second   line"}}
    long = {"payload": {"text": "word " * 100}}
    structured = {"payload": {"kind": "handoff", "files": ["a.py"]}}

    assert mail_excerpt(plain) == "first line second line"
    assert len(mail_excerpt(long)) <= 160 and mail_excerpt(long).endswith("…")
    assert json.loads(mail_excerpt(structured)) == {"kind": "handoff", "files": ["a.py"]}
