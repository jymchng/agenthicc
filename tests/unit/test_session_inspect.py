"""Tests for durable session inspection (PRD-138 P1.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.tui.runtime import session_export

pytestmark = pytest.mark.unit


def _write_jsonl(path: Path, records: list[object], trailing: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records) + trailing,
        encoding="utf-8",
    )


def test_inspect_summarizes_artifacts_usage_workflows_and_resume_state(tmp_path: Path) -> None:
    session_id = "session-inspect"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "cwd": "/work/project",
                "model": "test-model",
                "created_at": 100.0,
                "last_active": 125.0,
                "api_key": "sk-ant-do-not-show-this",
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        tmp_path / f"{session_id}.jsonl",
        [
            {"event_type": "IntentCreated", "payload": {}},
            {
                "event_type": "WorkflowRunStarted",
                "payload": {
                    "run_id": "run-1",
                    "workflow_name": "code_plan",
                    "phase_names": ["plan", "execute"],
                    "intent": "private prompt",
                },
            },
            {
                "event_type": "WorkflowPhaseCompleted",
                "payload": {"run_id": "run-1", "phase_name": "plan"},
            },
            {
                "event_type": "WorkflowRunCompleted",
                "payload": {
                    "run_id": "run-1",
                    "workflow_name": "code_plan",
                    "phases_run": 1,
                    "status": "complete",
                },
            },
        ],
        trailing='{"event_type": "partial',
    )
    _write_jsonl(
        session_dir / "conversation.jsonl",
        [
            {"kind": "turn_start", "payload": {"turn_id": "turn-1"}},
            {"kind": "tool_complete", "payload": {"tool": "read_file"}},
            {
                "kind": "tokens",
                "payload": {"input_tokens": 12, "output_tokens": 7, "cost_usd": 0.0045},
            },
            {"kind": "error", "payload": {"message": "recoverable"}},
            {"kind": "turn_complete", "payload": {}},
        ],
    )
    _write_jsonl(
        session_dir / "conversation-journal.jsonl",
        [
            {
                "kind": "turn_started",
                "turn_id": "turn-1",
                "user_message": "private prompt",
                "base_count": 3,
            },
            {
                "kind": "tool_recorded",
                "turn_id": "turn-1",
                "key": "read_file|secret",
                "result": {"content": "private tool result"},
            },
        ],
        trailing="not-json\n",
    )

    summary = session_export.inspect_session(session_id, sessions_dir=tmp_path)

    assert summary["session_id"] == session_id
    assert summary["metadata"]["model"] == "test-model"  # type: ignore[index]
    assert summary["metadata"]["api_key"] == "<redacted>"  # type: ignore[index]
    assert summary["artifacts"]["kernel_events"] == {  # type: ignore[index]
        "present": True,
        "records": 4,
        "skipped_lines": 1,
    }
    assert summary["artifacts"]["conversation_journal"]["skipped_lines"] == 1  # type: ignore[index]
    assert summary["kernel"]["events"] == 4  # type: ignore[index]
    assert summary["kernel"]["by_type"]["WorkflowRunCompleted"] == 1  # type: ignore[index]

    conversation = summary["conversation"]  # type: ignore[assignment]
    assert conversation["events"] == 5
    assert conversation["turns"] == 1
    assert conversation["tool_calls"] == 1
    assert conversation["errors"] == 1
    assert conversation["tokens"] == {"input": 12, "output": 7, "cost_usd": 0.0045}

    workflow = summary["workflows"]  # type: ignore[assignment]
    assert workflow["total"] == 1
    assert workflow["complete"] == 1
    assert workflow["failed"] == 0
    assert workflow["incomplete"] == 0
    assert workflow["runs"][0]["status"] == "complete"

    resume = summary["resume"]  # type: ignore[assignment]
    assert resume == {
        "incomplete": True,
        "turn_id": "turn-1",
        "base_count": 3,
        "tool_records": 1,
        "turns_started": 1,
        "turns_completed": 0,
    }
    serialized = json.dumps(summary)
    assert "sk-ant-do-not-show-this" not in serialized
    assert "private prompt" not in serialized
    assert "private tool result" not in serialized


def test_inspect_reports_clean_resume_state(tmp_path: Path) -> None:
    session_id = "session-complete"
    _write_jsonl(tmp_path / f"{session_id}.jsonl", [{"event_type": "IntentCreated"}])
    _write_jsonl(
        tmp_path / session_id / "conversation-journal.jsonl",
        [
            {"kind": "turn_started", "turn_id": "turn-1", "base_count": 0},
            {"kind": "turn_completed", "turn_id": "turn-1"},
        ],
    )

    summary = session_export.inspect_session(session_id, sessions_dir=tmp_path)

    assert summary["resume"] == {
        "incomplete": False,
        "turn_id": None,
        "base_count": None,
        "tool_records": 0,
        "turns_started": 1,
        "turns_completed": 1,
    }


def test_inspect_rejects_missing_and_escaping_session_ids(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Session not found"):
        session_export.inspect_session("missing", sessions_dir=tmp_path)

    with pytest.raises(ValueError, match="single, relative"):
        session_export.inspect_session("../outside", sessions_dir=tmp_path)


def test_sessions_inspect_cli_supports_json_output(tmp_path: Path, monkeypatch, capsys) -> None:
    from agenthicc.cli.commands.sessions import sessions_inspect
    from agenthicc.cli.context import CLIContext

    session_id = "session-cli-inspect"
    _write_jsonl(tmp_path / f"{session_id}.jsonl", [{"event_type": "IntentCreated"}])
    monkeypatch.setattr(session_export, "_SESSIONS_DIR", tmp_path)

    sessions_inspect(CLIContext(), session_id, True)

    output = capsys.readouterr().out
    assert json.loads(output)["session_id"] == session_id


def test_sessions_inspect_cli_human_output_and_parser(monkeypatch, capsys, tmp_path: Path) -> None:
    from agenthicc.cli.commands.sessions import sessions_inspect
    from agenthicc.cli.context import CLIContext
    from agenthicc.cli.parser import _parse_args

    session_id = "session-cli-human"
    _write_jsonl(tmp_path / f"{session_id}.jsonl", [{"event_type": "IntentCreated"}])
    monkeypatch.setattr(session_export, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agenthicc", "sessions", "inspect", session_id, "--json"])

    args = _parse_args()

    assert args._entry.path == ("sessions", "inspect")
    assert args.session_id == session_id
    assert args.json is True

    sessions_inspect(CLIContext(), session_id)
    output = capsys.readouterr().out
    assert f"Session: {session_id}" in output
    assert "Artifacts:" in output
    assert "Resume: clean" in output
