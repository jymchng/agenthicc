from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.agents.definitions import (
    AgentSpec,
    DefinitionError,
    parse_definition,
    render_definition,
)
from agenthicc.agents.library import delete_definition, write_definition
from agenthicc.plugins.registry import ToolRegistry
from agenthicc.runners.agent_turn import _effective_agent_tool_names
from agenthicc.tools.capabilities import tool_read, tool_write
from agenthicc.tui.conversation_store import AppState


def _spec(name: str = "reviewer") -> AgentSpec:
    return AgentSpec(
        name=name, description="Reviews code", prompt="Review carefully.", mode="subagent"
    )


def test_effective_agent_tool_names_apply_name_and_capability_intersections() -> None:
    @tool_read
    def read_file() -> str:
        return "read"

    @tool_write
    def write_file() -> str:
        return "written"

    registry = ToolRegistry()
    registry.register_many([read_file, write_file], group="fs")
    spec = AgentSpec(
        name="reader",
        description="Reads files",
        tools=("fs.*",),
        capabilities=("read",),
    )

    names = _effective_agent_tool_names(spec, registry, AppState.create())

    assert names == ("read_file",)


def test_definition_override_key_round_trips_and_requires_a_boolean() -> None:
    source = (
        "---\nname: explorer\ndescription: Custom explorer\nmode: subagent\n"
        "override: true\n---\nProject prompt."
    )

    spec = parse_definition(source, source="project")

    assert spec.override is True
    assert parse_definition(render_definition(spec), source="project").override is True
    with pytest.raises(DefinitionError, match="override must be true or false"):
        parse_definition(source.replace("override: true", "override: yes"), source="project")


def test_definition_output_schema_round_trips_as_json() -> None:
    source = (
        "---\nname: reporter\ndescription: Returns a JSON report\nmode: subagent\n"
        'output: {"type":"object","properties":{"summary":{"type":"string"}},'
        '"required":["summary"],"additionalProperties":false}\n'
        "---\nReturn the report."
    )

    spec = parse_definition(source)

    assert spec.output_schema is not None
    restored = parse_definition(render_definition(spec))
    assert restored.output_schema == spec.output_schema


def test_definition_rejects_an_invalid_output_schema_with_source_location() -> None:
    source = (
        "---\nname: reporter\ndescription: Returns a JSON report\nmode: subagent\n"
        'output: {"type":"array"}\n---\nReturn the report.'
    )

    with pytest.raises(DefinitionError, match=r"reporter\.md:5: output: output schema root"):
        parse_definition(source, path=Path("reporter.md"))


def test_definition_model_effort_and_context_round_trip() -> None:
    source = (
        "---\nname: verifier\ndescription: Checks a result\nmode: subagent\n"
        "model: inherit\neffort: HIGH\ncontext: 12000\n---\nVerify carefully."
    )

    spec = parse_definition(source)
    rendered = render_definition(spec)
    restored = parse_definition(rendered)

    assert spec.model == "inherit"
    assert spec.effort == "high"
    assert spec.context == 12_000
    assert restored.model == "inherit"
    assert restored.effort == "high"
    assert restored.context == 12_000


def test_definition_rejects_invalid_model_identifier_with_source_location() -> None:
    path = Path(".agenthicc/agents/verifier.md")
    source = (
        "---\nname: verifier\ndescription: Checks a result\nmode: subagent\n"
        "model: invalid model\n---\nVerify carefully."
    )

    with pytest.raises(DefinitionError, match=r"verifier\.md:5: model must be an identifier"):
        parse_definition(source, path=path)


def test_definition_rejects_invalid_effort_with_source_location() -> None:
    path = Path(".agenthicc/agents/verifier.md")
    source = (
        "---\nname: verifier\ndescription: Checks a result\nmode: subagent\n"
        "effort: turbo\n---\nVerify carefully."
    )

    with pytest.raises(DefinitionError, match=r"verifier\.md:5: effort must be one of"):
        parse_definition(source, path=path)


def test_write_definition_is_atomic_markdown_and_trusted_for_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"

    path = write_definition(_spec(), scope="project", project_root=project)

    assert path == project / ".agenthicc" / "agents" / "reviewer.md"
    assert parse_definition(
        path.read_text(encoding="utf-8"), path=path, source="project"
    ).prompt == ("Review carefully.")
    from agenthicc.plugins.trust import current_extension_trust

    assert current_extension_trust().check(path, kind="agent") is None


def test_write_definition_refuses_existing_target_unless_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust

    trust = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=())
    project = tmp_path / "project"
    with override_extension_trust(trust):
        path = write_definition(_spec(), scope="project", project_root=project)

        with pytest.raises(FileExistsError):
            write_definition(_spec(), scope="project", project_root=project)

        changed = AgentSpec(name="reviewer", description="New description", mode="subagent")
        write_definition(changed, scope="project", project_root=project, overwrite=True)
        assert "description: New description" in path.read_text(encoding="utf-8")
        assert trust.check(path, kind="agent") is not None


def test_write_definition_refuses_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "project"
    directory = project / ".agenthicc" / "agents"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("do not replace", encoding="utf-8")
    (directory / "reviewer.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        write_definition(_spec(), scope="project", project_root=project, overwrite=True)
    assert outside.read_text(encoding="utf-8") == "do not replace"


def test_delete_definition_only_removes_project_or_user_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    project = tmp_path / "project"
    path = write_definition(_spec(), scope="project", project_root=project)

    delete_definition(path, project_root=project)

    assert not path.exists()
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        delete_definition(outside, project_root=project)
    assert outside.exists()
