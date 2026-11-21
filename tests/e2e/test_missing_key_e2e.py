"""TUX-08 AC 1 and 3: a turn with no API key, through the real CLI and Anthropic SDK.

``agenthicc --headless`` runs a real ``code_plan`` turn in a fresh interpreter
with no key in the environment. The installed Anthropic SDK raises its
``TypeError`` before any request is sent, so the test needs no network. It
checks the headless JSON, the persisted session log (no SDK text), and how the
persisted ``error`` event renders in the scrollback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.theme import BUILTIN_THEMES, current_theme, use_theme
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout

pytestmark = pytest.mark.e2e

_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
#: Fragments of the SDK's own message, which must never be persisted.
_SDK_TEXT = ("could not resolve authentication", "x-api-key", "auth_token")


def _run_headless(tmp_path: Path) -> tuple[list[dict[str, object]], Path, str]:
    """Run the headless CLI with no key; return its JSON lines, its home, and its stderr."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _KEY_VARIABLES and not name.startswith("AGENTHICC_")
    }
    env["HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, "-m", "agenthicc", "--headless", "--workflow", "code_plan"],
        cwd=project,
        env=env,
        input="Explain this repository\n",
        capture_output=True,
        text=True,
        timeout=CLI_PROCESS_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    records = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    return records, home, completed.stderr


@cli_test_timeout(1)
def test_a_missing_key_is_a_classified_redacted_error_everywhere(tmp_path: Path) -> None:
    records, home, stderr = _run_headless(tmp_path)

    # AC 3: headless emits one {"type": "error", "kind": ...} line for the turn.
    errors = [record for record in records if record.get("type") == "error"]
    assert len(errors) == 1, records
    error = errors[0]
    # A misclassified failure shows the turn's stderr, which names the real exception.
    assert error["kind"] == "missing_credentials", (error, stderr[-3000:])
    assert error["message"] == "No API key for anthropic."
    assert error["action"] == {
        "hint": "Set ANTHROPIC_API_KEY, or run /model to switch provider.",
        "commands": ["/model"],
    }
    session_id = str(error["session_id"])
    result = next(
        record for record in records if record.get("event_type") == "WorkflowRunCompleted"
    )
    assert result["error_kind"] == "missing_credentials"
    assert result["error"] == "No API key for anthropic."
    assert not any(fragment in json.dumps(records).casefold() for fragment in _SDK_TEXT)

    # AC 1: the persisted error event has the kind and no SDK message text.
    journal = home / ".agenthicc" / "sessions" / session_id / "conversation.jsonl"
    persisted = journal.read_text(encoding="utf-8")
    assert not any(fragment in persisted.casefold() for fragment in _SDK_TEXT), persisted
    events = [json.loads(line) for line in persisted.splitlines() if line.strip()]
    turn_errors = [
        event for event in events if event["kind"] == "error" and "kind" in event["payload"]
    ]
    assert len(turn_errors) == 1, events
    payload = turn_errors[0]["payload"]
    assert payload["kind"] == "missing_credentials"
    assert payload["exception_type"] == "TypeError"
    assert payload["provider"] == "anthropic"

    # AC 1: the persisted event renders as the notice and its action line.
    before = current_theme()
    use_theme(BUILTIN_THEMES["dark"])
    try:
        console = Console(record=True, width=80, force_terminal=False, color_system=None)
        appender = ScrollBufferAppender(AppState.create(), console)
        appender._render_one(ConversationEvent(turn_errors[0]["event_id"], "error", payload))
    finally:
        use_theme(before)
    assert console.export_text().splitlines() == [
        " ✗ No API key for anthropic.",
        "   Set ANTHROPIC_API_KEY, or run /model to switch provider.",
    ]
