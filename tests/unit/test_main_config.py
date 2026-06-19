"""Tests for agenthicc config subcommand (PRD-21)."""
from __future__ import annotations
import tomllib
import pytest
from unittest.mock import patch
from agenthicc.cli.config import _do_config_show, _do_config_init, TEMPLATE_CONFIG
from agenthicc.cli.parser import _parse_args

pytestmark = pytest.mark.unit


class TestTemplateConfig:
    def test_valid_toml(self):
        tomllib.loads(TEMPLATE_CONFIG)  # must not raise

    def test_has_key_sections(self):
        for section in ("[execution]", "[memory]", "[security]", "[api]"):
            assert section in TEMPLATE_CONFIG


class TestConfigShowCommand:
    def test_prints_execution_section(self, capsys):
        import argparse
        args = argparse.Namespace(set_overrides=[], config_command="show")
        _do_config_show(args)
        out = capsys.readouterr().out
        assert "[execution]" in out or "max_parallel_tasks" in out

    def test_cli_override_reflected(self, capsys):
        import argparse
        args = argparse.Namespace(set_overrides=["execution.max_parallel_tasks=77"], config_command="show")
        _do_config_show(args)
        out = capsys.readouterr().out
        assert "77" in out


class TestConfigInitCommand:
    def test_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import argparse
        args = argparse.Namespace(force=False, config_command="init")
        _do_config_init(args)
        assert (tmp_path / ".agenthicc" / "agenthicc.toml").exists()

    def test_content_is_valid_toml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import argparse
        args = argparse.Namespace(force=False, config_command="init")
        _do_config_init(args)
        content = (tmp_path / ".agenthicc" / "agenthicc.toml").read_text()
        tomllib.loads(content)  # must not raise

    def test_warns_if_exists_no_force(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agenthicc").mkdir()
        (tmp_path / ".agenthicc" / "agenthicc.toml").write_text("[execution]\n")
        import argparse
        args = argparse.Namespace(force=False, config_command="init")
        _do_config_init(args)
        out = capsys.readouterr().out
        assert "already exists" in out
        assert (tmp_path / ".agenthicc" / "agenthicc.toml").read_text() == "[execution]\n"

    def test_force_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".agenthicc").mkdir()
        (tmp_path / ".agenthicc" / "agenthicc.toml").write_text("[execution]\n")
        import argparse
        args = argparse.Namespace(force=True, config_command="init")
        _do_config_init(args)
        content = (tmp_path / ".agenthicc" / "agenthicc.toml").read_text()
        assert "[memory]" in content  # template content


class TestArgParsing:
    def test_set_flag_single(self):
        with patch("sys.argv", ["agenthicc", "--set", "execution.max_parallel_tasks=5"]):
            args = _parse_args()
        assert args.set_overrides == ["execution.max_parallel_tasks=5"]

    def test_set_flag_multiple(self):
        with patch("sys.argv", ["agenthicc", "--set", "a.x=1", "--set", "b.y=2"]):
            args = _parse_args()
        assert len(args.set_overrides) == 2

    def test_config_show_subcommand(self):
        with patch("sys.argv", ["agenthicc", "config", "show"]):
            args = _parse_args()
        assert getattr(args, "_entry", None) is not None
        assert args._entry.path == ("config", "show")

    def test_config_init_subcommand(self):
        with patch("sys.argv", ["agenthicc", "config", "init"]):
            args = _parse_args()
        assert getattr(args, "_entry", None) is not None
        assert args._entry.path == ("config", "init")

    def test_config_init_force_flag(self):
        with patch("sys.argv", ["agenthicc", "config", "init", "--force"]):
            args = _parse_args()
        assert args.force is True
