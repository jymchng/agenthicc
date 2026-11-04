"""How agenthicc starts a model-requested child process (TOOLR-02).

One place decides what every command tool and background terminal gives its
child:

- **No shared stdin.** Children read ``/dev/null``. Under the TUI the parent's
  stdin is the raw-mode terminal, and a child reading it stole keystrokes.
- **A scrubbed environment.** Variables that look like credentials (provider
  API keys, ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD``…) and agenthicc's own
  ``AGENTHICC_*`` controls are removed unless ``[tools.exec] env_passthrough``
  names them. Pagers and git prompts are disabled.
- **Resource limits.** ``[tools.exec] max_cpu_seconds`` / ``max_memory_mb``
  become ``RLIMIT_CPU`` / ``RLIMIT_AS`` in the child on POSIX.
- **Bounded capture.** :class:`StreamCapture` keeps a head and a rolling tail
  of each stream, so memory per command stays constant however much it prints.
- **The project's interpreter.** :func:`project_python` finds the Python the
  project itself uses rather than agenthicc's own.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agenthicc.config import ExecSettings

__all__ = [
    "HEAD_BYTES",
    "TAIL_BYTES",
    "ProjectPython",
    "StreamCapture",
    "child_env",
    "child_preexec",
    "configure",
    "exec_settings",
    "project_python",
]

#: Bytes kept from the start and from the end of each output stream.
HEAD_BYTES: Final = 16 * 1024
TAIL_BYTES: Final = 48 * 1024

_settings = ExecSettings()

_SECRET_NAMES: Final = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "MSGRAPH_TOKEN",
        "BRAVE_API_KEY",
        "CLOAKBROWSER_LICENSE_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
_SECRET_PATTERN: Final = re.compile(
    r"(?:API_?KEY|_TOKEN|_SECRET|PASSWORD|PASSWD|_CREDENTIALS?)$|^AGENTHICC_", re.IGNORECASE
)
#: Set for every child so tools never open a pager or a credential prompt.
_NON_INTERACTIVE: Final[Mapping[str, str]] = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "TERM": "dumb",
}


def configure(settings: ExecSettings) -> None:
    """Install the session's ``[tools.exec]`` settings (once, at startup)."""
    global _settings
    _settings = settings


def exec_settings() -> ExecSettings:
    """The settings every child process is started with."""
    return _settings


def _is_secret(name: str) -> bool:
    return name in _SECRET_NAMES or bool(_SECRET_PATTERN.search(name))


def child_env(overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a child: the parent's, scrubbed, plus *overlay*.

    The overlay is the model's explicit ``env`` argument and is applied as
    given — it supplies values, it cannot re-admit a scrubbed parent value.
    """
    passthrough = set(_settings.env_passthrough)
    env = {
        name: value
        for name, value in os.environ.items()
        if name in passthrough or not _is_secret(name)
    }
    env.update(_NON_INTERACTIVE)
    env.update(overlay or {})
    return env


def child_preexec() -> Callable[[], None] | None:
    """A ``preexec_fn`` that applies the configured resource limits, if any."""
    cpu = _settings.max_cpu_seconds
    memory = _settings.max_memory_mb
    if os.name != "posix" or (cpu == 0 and memory == 0):
        return None
    import resource

    def _apply() -> None:
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if memory:
            limit = memory * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return _apply


class StreamCapture:
    """The first :data:`HEAD_BYTES` and last :data:`TAIL_BYTES` of a stream."""

    __slots__ = ("_head", "_tail", "total")

    def __init__(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = HEAD_BYTES - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self._tail += chunk
            excess = len(self._tail) - TAIL_BYTES
            if excess > 0:
                del self._tail[:excess]

    @property
    def omitted(self) -> int:
        return self.total - len(self._head) - len(self._tail)

    def render(self) -> str:
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        if self.omitted > 0:
            return f"{head}\n…[{self.omitted} bytes omitted]…\n{tail}"
        return head + tail


@dataclass(frozen=True, slots=True)
class ProjectPython:
    """The interpreter command a Python tool runs, and where it was found."""

    argv: tuple[str, ...]
    source: str


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_python(cwd: str, workspace_root: str) -> ProjectPython:
    """The project's Python for *cwd*, searched no higher than *workspace_root*.

    Order: ``[tools.exec] python``; an active ``VIRTUAL_ENV`` other than
    agenthicc's own (unless it lies inside the workspace); a ``.venv`` at or
    above *cwd*; ``uv run python`` for a uv project; agenthicc's interpreter.
    """
    if _settings.python:
        return ProjectPython(tuple(_settings.python.split()), "configured")
    root = Path(workspace_root).resolve()
    here = Path(cwd).resolve()
    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    active = os.environ.get("VIRTUAL_ENV", "")
    if active:
        candidate = Path(active).resolve()
        interpreter = candidate / scripts / executable
        own = Path(sys.prefix).resolve() == candidate
        if interpreter.is_file() and (not own or _within(candidate, root)):
            return ProjectPython((str(interpreter),), "virtual_env")
    directory = here
    while True:
        interpreter = directory / ".venv" / scripts / executable
        if interpreter.is_file():
            return ProjectPython((str(interpreter),), ".venv")
        if (directory / "uv.lock").is_file() and (directory / "pyproject.toml").is_file():
            uv = shutil.which("uv")
            if uv is not None:
                return ProjectPython((uv, "run", "python"), "uv")
        if directory == root or directory.parent == directory or not _within(directory, root):
            break
        directory = directory.parent
    return ProjectPython((sys.executable,), "agenthicc")
