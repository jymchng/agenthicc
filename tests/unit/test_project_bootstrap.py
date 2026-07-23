"""Unit tests for the PRD-139 project bootstrap service and entry points."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agenthicc.project_bootstrap import (
    MANAGED_END,
    MANAGED_START,
    BootstrapError,
    BootstrapWriteError,
    build_bootstrap_plan,
    inspect_project,
    write_bootstrap_plan,
)

pytestmark = pytest.mark.unit


def _python_project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample-app"\n\n[project.optional-dependencies]\n'
        'dev = ["pytest", "ruff", "mypy"]\n'
    )
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("# Sample app\n")
    (tmp_path / ".env").write_text("SECRET=must-not-be-inspected\n")
    (tmp_path / ".agenthicc").mkdir()
    return tmp_path


class TestInspectProject:
    def test_detects_project_metadata_without_reading_arbitrary_files(self, tmp_path):
        root = _python_project(tmp_path)

        snapshot = inspect_project(root)

        assert snapshot.project_name == "sample-app"
        assert snapshot.stacks == ("Python",)
        assert snapshot.manifests == ("pyproject.toml",)
        assert snapshot.test_paths == ("tests",)
        assert "src" in snapshot.top_level_entries
        assert ".env" not in snapshot.top_level_entries
        assert ".agenthicc" not in snapshot.top_level_entries
        assert "uv run pytest tests/ -q" in snapshot.verification_commands
        assert "uv run ruff check src/ tests/" in snapshot.verification_commands
        assert "uv run mypy src" in snapshot.verification_commands

    def test_detects_other_supported_stacks_and_commands(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name":"frontend","scripts":{"test":"vitest","lint":"eslint ."}}'
        )
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "native"\n')
        (tmp_path / "go.mod").write_text("module example.com/service\n")
        (tmp_path / "Makefile").write_text("check:\n\ttrue\n")

        snapshot = inspect_project(tmp_path)

        assert snapshot.project_name == "frontend"
        assert snapshot.stacks == ("Node.js", "Rust", "Go")
        assert "npm run test" in snapshot.verification_commands
        assert "cargo test" in snapshot.verification_commands
        assert "go test ./..." in snapshot.verification_commands
        assert "make check" in snapshot.verification_commands

    def test_invalid_manifest_falls_back_safely(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("not = [valid\n")

        snapshot = inspect_project(tmp_path)

        assert snapshot.project_name == tmp_path.name
        assert snapshot.stacks == ("Python",)

    def test_rejects_non_directory_root(self, tmp_path):
        file_path = tmp_path / "file"
        file_path.write_text("x")

        with pytest.raises(BootstrapError, match="not a directory"):
            inspect_project(file_path)


class TestBootstrapPlan:
    def test_new_plan_is_preview_only_and_contains_managed_guidance(self, tmp_path):
        _python_project(tmp_path)

        plan = build_bootstrap_plan(tmp_path)

        assert plan.exists is False
        assert plan.changed is True
        assert not (tmp_path / "AGENTS.md").exists()
        assert MANAGED_START in plan.proposed_content
        assert MANAGED_END in plan.proposed_content
        assert "sample-app" in plan.proposed_content
        assert "--- /dev/null" in plan.preview()

    def test_existing_guidance_is_preserved_and_managed_block_is_updated(self, tmp_path):
        _python_project(tmp_path)
        existing = (
            "# Team guidance\n\nKeep the deployment notes intact.\n\n"
            f"{MANAGED_START}\nold snapshot\n{MANAGED_END}\n"
        )
        (tmp_path / "AGENTS.md").write_text(existing)

        plan = build_bootstrap_plan(tmp_path)

        assert plan.exists is True
        assert "Keep the deployment notes intact." in plan.proposed_content
        assert "old snapshot" not in plan.proposed_content
        assert plan.proposed_content.count(MANAGED_START) == 1
        assert plan.proposed_content.count(MANAGED_END) == 1

    def test_incomplete_managed_markers_fail_closed(self, tmp_path):
        _python_project(tmp_path)
        (tmp_path / "AGENTS.md").write_text(f"{MANAGED_START}\nunfinished\n")

        with pytest.raises(BootstrapError, match="incomplete"):
            build_bootstrap_plan(tmp_path)

    def test_up_to_date_plan_is_not_changed(self, tmp_path):
        _python_project(tmp_path)
        first = build_bootstrap_plan(tmp_path)
        write_bootstrap_plan(first)

        second = build_bootstrap_plan(tmp_path)

        assert second.changed is False
        assert second.preview() == "AGENTS.md is already up to date."


class TestBootstrapWrite:
    def test_writes_new_file_atomically(self, tmp_path):
        _python_project(tmp_path)
        plan = build_bootstrap_plan(tmp_path)

        target = write_bootstrap_plan(plan)

        assert target == tmp_path / "AGENTS.md"
        assert target.read_text().startswith("# AGENTS.md — Project guidance")
        assert not list(tmp_path.glob(".AGENTS.md.*.tmp"))

    def test_existing_file_requires_force(self, tmp_path):
        _python_project(tmp_path)
        (tmp_path / "AGENTS.md").write_text("# Existing guidance\n")
        plan = build_bootstrap_plan(tmp_path)

        with pytest.raises(BootstrapWriteError, match="Refusing to overwrite"):
            write_bootstrap_plan(plan)
        assert (tmp_path / "AGENTS.md").read_text() == "# Existing guidance\n"

        write_bootstrap_plan(plan, force=True)
        assert "# Existing guidance" in (tmp_path / "AGENTS.md").read_text()
        assert MANAGED_START in (tmp_path / "AGENTS.md").read_text()

    def test_detects_target_changed_after_preview(self, tmp_path):
        _python_project(tmp_path)
        (tmp_path / "AGENTS.md").write_text("# Existing guidance\n")
        plan = build_bootstrap_plan(tmp_path)
        (tmp_path / "AGENTS.md").write_text("# Changed while reviewing\n")

        with pytest.raises(BootstrapWriteError, match="changed after the preview"):
            write_bootstrap_plan(plan, force=True)

    def test_rejects_target_that_becomes_directory_after_preview(self, tmp_path):
        _python_project(tmp_path)
        plan = build_bootstrap_plan(tmp_path)
        (tmp_path / "AGENTS.md").mkdir()

        with pytest.raises(BootstrapWriteError, match="Cannot read"):
            write_bootstrap_plan(plan)

    @pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
    def test_rejects_symlink_target(self, tmp_path):
        _python_project(tmp_path)
        outside = tmp_path.parent / "outside-agents.md"
        outside.write_text("do not overwrite\n")
        (tmp_path / "AGENTS.md").symlink_to(outside)

        with pytest.raises(BootstrapError, match="symlink"):
            build_bootstrap_plan(tmp_path)
        assert outside.read_text() == "do not overwrite\n"


class TestCliInit:
    def test_cli_parser_registers_init_and_flags(self):
        from agenthicc.cli.parser import _parse_args

        with patch("sys.argv", ["agenthicc", "init", "--write", "--force"]):
            args = _parse_args()

        assert args._entry.path == ("init",)
        assert args.write is True
        assert args.force is True

    def test_cli_defaults_to_preview_without_writing(self, tmp_path, monkeypatch, capsys):
        from agenthicc.cli.commands.init import init_project
        from agenthicc.cli.context import CLIContext

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "preview"\n')

        init_project(CLIContext())

        output = capsys.readouterr().out
        assert "Preview only" in output
        assert not (tmp_path / "AGENTS.md").exists()

    def test_cli_write_creates_guidance(self, tmp_path, monkeypatch, capsys):
        from agenthicc.cli.commands.init import init_project
        from agenthicc.cli.context import CLIContext

        monkeypatch.chdir(tmp_path)
        init_project(CLIContext(), write=True)

        assert (tmp_path / "AGENTS.md").exists()
        assert "Updated" in capsys.readouterr().out


class TestTuiInit:
    def test_builtin_registry_exposes_init(self):
        from agenthicc.commands import build_builtin_registry

        command = build_builtin_registry().get("/init")

        assert command is not None
        assert command.handler is not None

    def test_slash_init_previews_without_writing(self, tmp_path, monkeypatch):
        from agenthicc.commands import CommandContext, CommandDispatcher, build_builtin_registry

        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "tui-preview"\n')
        console = MagicMock()
        context = CommandContext(
            text="/init",
            args="",
            model="",
            console=console,
            config=MagicMock(),
        )

        assert CommandDispatcher(build_builtin_registry()).dispatch("/init", context)
        assert not (tmp_path / "AGENTS.md").exists()
        assert any("Preview only" in str(call) for call in console.print.call_args_list)

    def test_slash_init_write_creates_guidance(self, tmp_path, monkeypatch):
        from agenthicc.commands import CommandContext, CommandDispatcher, build_builtin_registry

        monkeypatch.chdir(tmp_path)
        console = MagicMock()
        context = CommandContext(
            text="/init write",
            args="write",
            model="",
            console=console,
            config=MagicMock(),
        )

        assert CommandDispatcher(build_builtin_registry()).dispatch("/init write", context)
        assert (tmp_path / "AGENTS.md").exists()
