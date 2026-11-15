"""Copying and paging an item's text (TUX-09 §2.1 ``y`` and ``e``).

``y`` writes an OSC 52 sequence, which carries the text to the clipboard of
the machine the terminal runs on, over SSH and tmux alike. A terminal that
cannot take it (``TERM=linux``, ``dumb``) gets the system clipboard command
instead (``pbcopy``, ``wl-copy``, ``xclip``, ``xsel``, ``clip.exe``). The
payload is plain text: no styles, no markup.

``e`` opens the text in ``$PAGER``, or in ``$EDITOR`` read-only (a private,
read-only temporary file, and ``-R`` for the vi family).
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

__all__ = [
    "MAX_COPY_BYTES",
    "CopyResult",
    "copy_text",
    "osc52_sequence",
    "pager_command",
    "run_pager",
]

#: OSC 52 payloads past about 100 KB are dropped by common terminals.
MAX_COPY_BYTES = 74_994
_OSC = "\x1b]52;c;"
_ST = "\x07"
#: ``TERM`` values whose terminals do not take OSC 52.
_NO_OSC52 = frozenset({"linux", "dumb", "unknown"})
_SYSTEM_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("pbcopy",),
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
    ("clip.exe",),
)
_VI_FAMILY = frozenset({"vi", "vim", "nvim", "view", "gvim"})


def osc52_sequence(text: str) -> str:
    """The OSC 52 sequence that sets the clipboard to *text* (bounded to 74,994 bytes)."""
    data = text.encode("utf-8")[:MAX_COPY_BYTES]
    return f"{_OSC}{base64.b64encode(data).decode('ascii')}{_ST}"


class CopyResult:
    """How a copy went: the method used and whether the text was cut."""

    __slots__ = ("cut", "method")

    def __init__(self, method: str, cut: bool) -> None:
        self.method = method
        self.cut = cut


def copy_text(
    text: str,
    *,
    write: Callable[[str], None],
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str], bytes], bool] | None = None,
) -> CopyResult:
    """Copy *text*: OSC 52 through *write*, or a system command where OSC 52 is not available."""
    environment = os.environ if env is None else env
    cut = len(text.encode("utf-8")) > MAX_COPY_BYTES
    if environment.get("TERM", "") not in _NO_OSC52:
        write(osc52_sequence(text))
        return CopyResult("osc52", cut)
    runner = run or _run_clipboard
    for command in _SYSTEM_COMMANDS:
        if which(command[0]) is not None and runner(list(command), text.encode("utf-8")):
            return CopyResult("system", False)
    # Nothing else is left to try; a terminal that ignores OSC 52 loses nothing.
    write(osc52_sequence(text))
    return CopyResult("osc52", cut)


def _run_clipboard(command: list[str], data: bytes) -> bool:
    try:
        return subprocess.run(command, input=data, check=False, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pager_command(env: Mapping[str, str], path: Path) -> list[str] | None:
    """The command that shows *path*: ``$PAGER``, else ``$EDITOR`` read-only, else ``less``."""
    pager = env.get("PAGER", "").strip()
    if pager:
        return [*shlex.split(pager), str(path)]
    editor = env.get("VISUAL", "").strip() or env.get("EDITOR", "").strip()
    if editor:
        words = shlex.split(editor)
        flags = ["-R"] if words and Path(words[0]).name in _VI_FAMILY else []
        return [*words, *flags, str(path)]
    return ["less", str(path)] if shutil.which("less") else None


def run_pager(text: str, env: Mapping[str, str] | None = None) -> str:
    """Show *text* in a pager and wait; return ``""`` or why it could not be shown."""
    environment = os.environ if env is None else env
    handle, name = tempfile.mkstemp(prefix="agenthicc-item-", suffix=".txt")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text if text.endswith("\n") else text + "\n")
        path.chmod(0o400)
        command = pager_command(environment, path)
        if command is None:
            return "no pager: set $PAGER or $EDITOR"
        try:
            subprocess.run(command, check=False)
        except OSError as exc:
            return f"could not start {command[0]}: {exc.strerror or type(exc).__name__}"
        return ""
    finally:
        path.unlink(missing_ok=True)
