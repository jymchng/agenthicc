"""CLI-level ownership journey for PRD-171."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.runners.session_lease import SessionOwnerStore

pytestmark = pytest.mark.e2e


def test_second_cli_continue_exits_before_session_construction(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    project = tmp_path / "project"
    session_id = "owned-session"
    project.mkdir()
    (sessions / session_id).mkdir(parents=True)
    (sessions / "index.json").write_text(
        json.dumps({session_id: {"cwd": str(project), "last_active": 100.0}}),
        encoding="utf-8",
    )

    owner = SessionOwnerStore(sessions).acquire(session_id, entrypoint="tui")
    try:
        env = os.environ.copy()
        env["HOME"] = str(home)
        result = subprocess.run(
            [sys.executable, "-m", "agenthicc", "--continue"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
    finally:
        owner.release()

    assert result.returncode == 3
    assert "session_already_active" in result.stderr
    assert session_id in result.stderr
    assert "No transcript was loaded" in result.stderr
    assert "TUI error" not in result.stderr


def test_explicit_resume_conflict_has_the_same_early_exit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    project = tmp_path / "project"
    session_id = "explicit-session"
    project.mkdir()
    (sessions / session_id).mkdir(parents=True)

    owner = SessionOwnerStore(sessions).acquire(session_id, entrypoint="tui")
    try:
        env = os.environ.copy()
        env["HOME"] = str(home)
        result = subprocess.run(
            [sys.executable, "-m", "agenthicc", "--resume", session_id],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
    finally:
        owner.release()

    assert result.returncode == 3
    assert "session_already_active" in result.stderr
    assert "No transcript was loaded" in result.stderr
    assert "TUI error" not in result.stderr


def test_headless_workflow_conflict_is_machine_readable_and_pre_startup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    project = tmp_path / "project"
    session_id = "headless-session"
    project.mkdir()
    (sessions / session_id).mkdir(parents=True)
    (sessions / "index.json").write_text(
        json.dumps({session_id: {"cwd": str(project), "last_active": 100.0}}),
        encoding="utf-8",
    )

    owner = SessionOwnerStore(sessions).acquire(session_id, entrypoint="tui")
    try:
        env = os.environ.copy()
        env["HOME"] = str(home)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agenthicc",
                "--headless",
                "--continue",
                "--workflow",
                "code_plan",
            ],
            cwd=project,
            env=env,
            input="",
            text=True,
            capture_output=True,
            timeout=15,
        )
    finally:
        owner.release()

    assert result.returncode == 3
    records = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert len(records) == 1
    assert records[0]["code"] == "session_already_active"
    assert records[0]["session_id"] == session_id
    assert all(record.get("status") != "ready" for record in records)


def test_generic_headless_resume_cannot_bypass_the_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions = home / ".agenthicc" / "sessions"
    project = tmp_path / "project"
    session_id = "generic-headless-session"
    project.mkdir()
    (sessions / session_id).mkdir(parents=True)

    owner = SessionOwnerStore(sessions).acquire(session_id, entrypoint="tui")
    try:
        env = os.environ.copy()
        env["HOME"] = str(home)
        result = subprocess.run(
            [sys.executable, "-m", "agenthicc", "--headless", "--resume", session_id],
            cwd=project,
            env=env,
            input="",
            text=True,
            capture_output=True,
            timeout=15,
        )
    finally:
        owner.release()

    assert result.returncode == 3
    records = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert len(records) == 1
    assert records[0]["code"] == "session_already_active"
