"""CLI views of the Agent Library catalog (AGX-09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.agents.definitions import AgentSpec
from agenthicc.cli.commands import background
from agenthicc.cli.context import CLIContext
from agenthicc.tui.workspace.agents_hub.model import LibraryEntry

pytestmark = pytest.mark.unit


def _entry(
    *,
    name: str = "reviewer",
    source: str = "project",
    state: str = "active",
    note: str = "",
    prompt: str = "Review carefully.",
) -> LibraryEntry:
    spec = AgentSpec(
        name=name,
        description="Reviews changes.",
        prompt=prompt,
        mode="subagent",
        tools=("read_file", "git_diff"),
        source=source,
        path=Path(f"/project/.agenthicc/agents/{name}.md"),
    )
    return LibraryEntry(
        name=name,
        source=source,
        description=spec.description,
        tools="read_file git_diff",
        mode=spec.mode,
        state=state,
        note=note,
        path=str(spec.path),
        spec=spec,
    )


def test_definitions_list_json_includes_catalog_state_and_policy_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(background, "_definition_entries", lambda: [_entry()])

    background.agents_definitions_list(CLIContext(), json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "reviewer"
    assert payload[0]["source"] == "project"
    assert payload[0]["state"] == "active"
    assert payload[0]["tools"] == ["read_file", "git_diff"]
    assert payload[0]["path"].endswith("reviewer.md")


def test_definitions_show_includes_shadowed_candidates_and_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        background,
        "_definition_entries",
        lambda: [
            _entry(source="user", state="shadowed", note="shadowed by the project definition"),
            _entry(source="project"),
        ],
    )

    background.agents_definitions_show(CLIContext(), "reviewer")

    output = capsys.readouterr().out
    assert "reviewer · user · shadowed" in output
    assert "shadowed by the project definition" in output
    assert "reviewer · project · active" in output
    assert "Review carefully." in output


def test_definitions_show_returns_error_for_unknown_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(background, "_definition_entries", list)

    with pytest.raises(SystemExit) as exc_info:
        background.agents_definitions_show(CLIContext(), "missing")

    assert exc_info.value.code == 1
    assert "Agent definition not found: missing" in capsys.readouterr().out


def test_definitions_validate_reports_file_and_fails_for_invalid_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = _entry(state="invalid", note="invalid: unknown key 'toolz' (line 4)")
    monkeypatch.setattr(background, "_definition_entries", lambda: [invalid])

    with pytest.raises(SystemExit) as exc_info:
        background.agents_definitions_validate(CLIContext())

    output = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert "unknown key 'toolz' (line 4)" in output
    assert "/project/.agenthicc/agents/reviewer.md" in output


def test_manifest_registers_nested_definition_commands() -> None:
    from agenthicc.cli import registry

    index = registry.command_index()
    assert index.resolve(["agents", "definitions", "list"]) == (
        "agents",
        "definitions",
        "list",
    )
    assert index.resolve(["agents", "definitions", "show"]) == (
        "agents",
        "definitions",
        "show",
    )
    assert index.resolve(["agents", "definitions", "validate"]) == (
        "agents",
        "definitions",
        "validate",
    )
