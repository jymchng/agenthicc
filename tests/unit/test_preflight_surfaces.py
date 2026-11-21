"""TUX-03 §5: ``/status``, ``agenthicc config show`` and ``[ui] banner`` reuse the one pre-flight check."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.cli.context import CLIContext
from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.commands.command import CommandContext
from agenthicc.commands.dispatcher import CommandDispatcher
from agenthicc.config import AgenthiccConfig, UISettings, load_config

pytestmark = pytest.mark.unit

_MISSING_KEY = (
    "No API key for anthropic. Set ANTHROPIC_API_KEY, or run /model to pick another provider."
)


@pytest.fixture
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# ── /status ───────────────────────────────────────────────────────────────────


def _status(config: AgenthiccConfig) -> str:
    console = Console(file=io.StringIO(), width=140, record=True, force_terminal=False)
    context = CommandContext(
        text="/status", args="", model="anthropic/m", console=console, config=config
    )
    assert CommandDispatcher(build_builtin_registry()).dispatch("/status", context)
    return console.export_text()


def test_status_says_a_missing_key_with_the_headers_words(no_key: None) -> None:
    assert f"Pre-flight: {_MISSING_KEY}" in _status(AgenthiccConfig())


def test_status_says_when_every_check_passed(
    no_key: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rg").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "rg").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    assert "Pre-flight: all checks passed" in _status(AgenthiccConfig())


# ── config show ───────────────────────────────────────────────────────────────


def test_config_show_ends_with_the_preflight_findings(
    no_key: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agenthicc.cli.commands import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config.config_show(CLIContext())

    shown = capsys.readouterr().out
    assert "\n# Pre-flight\n" in shown
    assert f"# ! {_MISSING_KEY}" in shown
    assert shown.rstrip().splitlines()[-1].startswith("# ")


# ── [ui] banner ───────────────────────────────────────────────────────────────


def test_the_banner_defaults_to_compact_and_accepts_full_and_off() -> None:
    assert UISettings().banner == "compact"
    assert UISettings(banner="full").banner == "full"
    assert UISettings(banner="off").banner == "off"


def test_an_unknown_banner_is_rejected_with_the_choices() -> None:
    with pytest.raises(ValueError, match="ui.banner must be one of: full, compact, off"):
        UISettings(banner="loud")


def test_the_banner_is_read_from_the_configuration_file(tmp_path: Path) -> None:
    path = tmp_path / "agenthicc.toml"
    path.write_text('[ui]\nbanner = "off"\n', encoding="utf-8")

    assert load_config(user_path=path, project_path=tmp_path / "none.toml").ui.banner == "off"


# ── /resume ───────────────────────────────────────────────────────────────────


def test_resume_opens_the_history_tab_of_the_agents_hub() -> None:
    opened: list[str] = []
    context = CommandContext(
        text="/resume",
        args="",
        model="m",
        console=Console(file=io.StringIO()),
        config=AgenthiccConfig(),
        open_agents_hub=opened.append,
    )

    assert CommandDispatcher(build_builtin_registry()).dispatch("/resume", context)
    assert opened == ["history"]


def test_resume_outside_the_tui_says_how_to_do_it_instead() -> None:
    console = Console(file=io.StringIO(), record=True, width=120)
    context = CommandContext(
        text="/resume", args="", model="m", console=console, config=AgenthiccConfig()
    )

    assert CommandDispatcher(build_builtin_registry()).dispatch("/resume", context)
    assert "agenthicc agents --tab history" in console.export_text()
