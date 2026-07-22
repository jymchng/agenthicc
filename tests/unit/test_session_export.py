"""Tests for portable, redacted session exports (PRD-138 P1.3)."""

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


def test_export_includes_session_artifacts_and_redacts_secrets(tmp_path: Path) -> None:
    session_id = "session-123"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "cwd": "/work/project",
                "model": "test-model",
                "api_key": "sk-ant-metadata-secret",
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        tmp_path / f"{session_id}.jsonl",
        [
            {
                "event_type": "IntentCreated",
                "payload": {"input_tokens": 3, "authorization": "Bearer event-secret"},
            }
        ],
        trailing='{"event_type": "partial',
    )
    _write_jsonl(
        session_dir / "conversation.jsonl",
        [{"kind": "message", "payload": {"content": "sk-openai-secret-value"}}],
    )
    _write_jsonl(
        session_dir / "conversation-journal.jsonl",
        [{"kind": "turn_started", "user_message": "inspect the project"}],
    )
    _write_jsonl(
        session_dir / "cassette" / "cassette.jsonl",
        [{"input": {"headers": {"Authorization": "Bearer cassette-secret"}}}],
    )
    _write_jsonl(
        session_dir / "cassette" / "approvals.jsonl",
        [{"kind": "approval", "token": "approval-secret"}],
    )
    (session_dir / "cassette" / "meta.json").write_text(
        json.dumps({"provider": "test", "client_secret": "meta-secret"}),
        encoding="utf-8",
    )

    output = tmp_path / "exports" / "session.json"
    exported = session_export.export_session(session_id, output, sessions_dir=tmp_path)

    assert exported == output
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["format"] == "agenthicc.session.export"
    assert document["schema_version"] == 1
    assert document["session_id"] == session_id
    assert document["metadata"]["model"] == "test-model"
    assert document["artifacts"]["kernel_events"][0]["payload"]["input_tokens"] == 3
    assert document["manifest"]["kernel_events"]["skipped_lines"] == 1
    assert document["manifest"]["conversation_events"]["records"] == 1
    assert document["redactions"] >= 6
    assert "sk-ant-metadata-secret" not in output.read_text(encoding="utf-8")
    assert "event-secret" not in output.read_text(encoding="utf-8")
    assert "cassette-secret" not in output.read_text(encoding="utf-8")
    assert "approval-secret" not in output.read_text(encoding="utf-8")


def test_export_preserves_valid_records_after_corrupt_lines(tmp_path: Path) -> None:
    session_id = "session-corrupt"
    _write_jsonl(
        tmp_path / f"{session_id}.jsonl",
        [{"event_type": "first"}, {"event_type": "second"}],
        trailing="not-json\n",
    )

    output = session_export.export_session(
        session_id, tmp_path / "export.json", sessions_dir=tmp_path
    )
    document = json.loads(output.read_text(encoding="utf-8"))

    assert [event["event_type"] for event in document["artifacts"]["kernel_events"]] == [
        "first",
        "second",
    ]
    assert document["manifest"]["kernel_events"] == {
        "present": True,
        "records": 2,
        "skipped_lines": 1,
    }


def test_export_rejects_unknown_or_escaping_session_ids(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Session not found"):
        session_export.export_session("missing", tmp_path / "missing.json", sessions_dir=tmp_path)

    with pytest.raises(ValueError, match="single, relative"):
        session_export.export_session(
            "../outside", tmp_path / "outside.json", sessions_dir=tmp_path
        )

    with pytest.raises(ValueError, match="single, relative"):
        session_export.export_session(
            "nested\\session", tmp_path / "outside.json", sessions_dir=tmp_path
        )


def test_export_replaces_destination_atomically(tmp_path: Path) -> None:
    session_id = "session-atomic"
    _write_jsonl(tmp_path / f"{session_id}.jsonl", [{"event_type": "complete"}])
    output = tmp_path / "export.json"
    output.write_text("old content", encoding="utf-8")

    session_export.export_session(session_id, output, sessions_dir=tmp_path)

    assert json.loads(output.read_text(encoding="utf-8"))["session_id"] == session_id
    assert not list(tmp_path.glob(".export.json.*.tmp"))


def test_sessions_export_cli_command_accepts_output_option(monkeypatch) -> None:
    from agenthicc.cli.parser import _parse_args

    monkeypatch.setattr(
        "sys.argv",
        ["agenthicc", "sessions", "export", "session-123", "--output", "bundle.json"],
    )

    args = _parse_args()

    assert args._entry.path == ("sessions", "export")
    assert args.session_id == "session-123"
    assert args.output == "bundle.json"


def test_sessions_export_cli_handler_writes_requested_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from agenthicc.cli.commands.sessions import sessions_export
    from agenthicc.cli.context import CLIContext

    session_id = "session-cli"
    _write_jsonl(tmp_path / f"{session_id}.jsonl", [{"event_type": "complete"}])
    monkeypatch.setattr(session_export, "_SESSIONS_DIR", tmp_path)
    output = tmp_path / "cli-export.json"

    sessions_export(CLIContext(), session_id, str(output))

    assert json.loads(output.read_text(encoding="utf-8"))["session_id"] == session_id
    assert f"Exported session {session_id}" in capsys.readouterr().out
