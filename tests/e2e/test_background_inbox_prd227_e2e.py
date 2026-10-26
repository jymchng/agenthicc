"""CLI user journey for the durable background interaction inbox."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.background import (
    BackgroundInteractionInbox,
    BackgroundSession,
    BackgroundStore,
    SessionStatus,
)
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.e2e


@cli_test_timeout(2)
def test_cli_lists_and_answers_exact_persisted_question(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = BackgroundStore(home / ".agenthicc" / "background")
    session = BackgroundSession.create(
        "inbox-cli-session",
        title="Inbox CLI test",
        cwd=str(tmp_path / "workspace"),
        workflow_name="goal_flow",
        intent="answer a question",
    ).evolve(status=SessionStatus.RUNNING, attempt=3, lease_token="cli-e2e-lease")
    store.create(session)
    inbox = BackgroundInteractionInbox(store)
    item = inbox.register(
        session_id=session.session_id,
        attempt=session.attempt,
        request_id="question-cli-1",
        kind="question",
        target={
            "tool_name": "ask_user",
            "question_ids": ["runtime"],
            "questions": [{"id": "runtime", "text": "Which runtime?", "options": ["Python"]}],
        },
        expires_at=None,
    )
    store.transition(
        session.session_id,
        SessionStatus.WAITING_INPUT,
        expected_status=SessionStatus.RUNNING,
        input_item_id=item.item_id,
    )
    item = inbox.activate(item.item_id)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    listing = subprocess.run(
        [sys.executable, "-m", "agenthicc", "agents", "inbox", "list", "--json"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr
    listed = json.loads(listing.stdout)
    assert listed[0]["item_id"] == item.item_id
    assert listed[0]["version"] == item.version
    assert "response" not in listed[0]

    answered = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenthicc",
            "agents",
            "inbox",
            "answer",
            item.item_id,
            "  exact answer  ",
            str(item.version),
            "cli-e2e-idempotency-key",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert answered.returncode == 0, answered.stderr
    assert "accepted" in answered.stdout

    accepted = BackgroundInteractionInbox(BackgroundStore(store.root)).accepted_answer(item.item_id)
    assert accepted is not None
    assert accepted.response == {"answers": {"runtime": "  exact answer  "}}
