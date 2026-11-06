"""TUX-01: no TUI module names a colour; every colour comes from a theme token.

The scan covers string literals and raw SGR escapes in ``agenthicc.tui`` and
the slash commands. ``bold``, ``dim``, ``italic``, ``reverse`` and
``underline`` are attributes, not colours, and stay allowed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "agenthicc"
_SCANNED = (_SRC / "tui", _SRC / "commands")
_EXEMPT = {_SRC / "tui" / "theme.py"}

#: Files still to migrate: each later TUX wave removes the files it rewrites.
#: The series is complete when this set is empty.
_PENDING: frozenset[str] = frozenset(
    {
        "commands/builtins.py",
        "tui/keymap_table.py",
        "tui/runtime/mode_manager.py",
        "tui/welcome.py",
        "tui/workspace/agents_hub/answer.py",
        "tui/workspace/agents_hub/hub.py",
        "tui/workspace/agents_hub/render.py",
        "tui/workspace/appender.py",
        "tui/workspace/components.py",
        "tui/workspace/overlays/approval.py",
        "tui/workspace/overlays/config_menu.py",
        "tui/workspace/overlays/help.py",
        "tui/workspace/overlays/loops.py",
        "tui/workspace/overlays/plan_approval.py",
        "tui/workspace/overlays/prompt.py",
        "tui/workspace/overlays/questions.py",
        "tui/workspace/overlays/registry_list.py",
        "tui/workspace/overlays/schedules.py",
        "tui/workspace/overlays/subagents.py",
        "tui/workspace/overlays/terminals.py",
        "tui/workspace/overlays/transcript_history.py",
        "tui/workspace/subagent_panel.py",
        "tui/workspace/workspace.py",
    }
)

_COLOR = re.compile(
    r"(?<![A-Za-z_0-9.-])"
    r"(?:black|red|green|yellow|blue|magenta|cyan|white|grey\d{1,3}|gray\d{1,3}"
    r"|bright_[a-z]+|orange\d*|purple\d*|dark_[a-z_]+|#[0-9a-fA-F]{6}"
    r"|color\(\d+\)|rgb\([^)]*\))"
    r"(?![A-Za-z_0-9])"
)
_STRING = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")
_SGR_COLOR = re.compile(r"\\x1b\[[0-9;]*(?:3[0-7]|9[0-7]|4[0-7]|10[0-7]|38;|48;)[0-9;]*m")


def _offenders(path: Path, base: Path = _SRC) -> list[str]:
    found: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _SGR_COLOR.search(line) or any(
            _COLOR.search(literal) for literal in _STRING.findall(line)
        ):
            found.append(f"{path.relative_to(base)}:{number}: {stripped[:100]}")
    return found


def _files() -> list[Path]:
    return sorted(path for root in _SCANNED for path in root.rglob("*.py") if path not in _EXEMPT)


def test_no_tui_module_names_a_colour() -> None:
    offenders = [
        line
        for path in _files()
        if str(path.relative_to(_SRC)) not in _PENDING
        for line in _offenders(path)
    ]
    assert offenders == [], "\n".join(offenders)


def test_the_pending_list_only_names_files_that_still_need_migrating() -> None:
    stale = sorted(name for name in _PENDING if not _offenders(_SRC / name))
    assert stale == [], f"remove migrated files from _PENDING: {stale}"


def test_the_scanner_catches_each_literal_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        'a = "[red]x[/red]"\n'
        "b = Text('x', style='bold cyan')\n"
        'c = "\\x1b[1;33m>"\n'
        'd = "on grey11"\n'
        'e = "[bold]fine[/bold] [dim]also[/dim] [reverse]ok[/reverse]"\n'
        "# a comment naming red is fine\n",
        encoding="utf-8",
    )
    lines = [line.split(":")[1] for line in _offenders(sample, tmp_path)]
    assert lines == ["1", "2", "3", "4"]
