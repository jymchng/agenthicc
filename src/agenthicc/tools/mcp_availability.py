"""Whether the MCP client library can be used, and the exact fix when it cannot (MCP-01).

``lauren-mcp`` is a base dependency of agenthicc, but an environment can still lack it
(an install made before it became one, a broken upgrade) or hold a release older than the
code needs. This module answers that question without importing the library, so a
configuration listing, ``mcp doctor`` and a failed server's notice can say **what is wrong,
in which interpreter, and the command that fixes it**.

It is kept apart from :mod:`agenthicc.tools.mcp` for the same reason
:mod:`agenthicc.tools.mcp_server_config` is: loading it never imports MCP transports.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import shlex
import sys
from dataclasses import dataclass

__all__ = [
    "MINIMUM_VERSION",
    "PACKAGE",
    "REQUIREMENT",
    "McpClientStatus",
    "McpUnavailable",
    "check_mcp_client",
    "fix_command",
]

#: The distribution agenthicc needs for MCP.
PACKAGE = "lauren-mcp"
#: The importable module the distribution provides.
_MODULE = "lauren_mcp"
#: The requirement agenthicc declares for it. ``pyproject.toml`` repeats this exact string and a
#: test keeps the two equal, because this text is what a user is told to install.
REQUIREMENT = "lauren-mcp[http,ws]>=1.2.1,<2"
#: The oldest release the launcher works with: 1.2.0 added the stdio ``cwd`` and ``env``
#: arguments, and 1.2.1 is the first release this project locks.
MINIMUM_VERSION: tuple[int, int, int] = (1, 2, 1)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """The leading ``major.minor.patch`` of *text*, ignoring any dev or local suffix."""
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def fix_command(executable: str | None = None) -> str:
    """The commands that install the client into the interpreter agenthicc is running under.

    They name the interpreter, not whichever ``pip`` is first on ``PATH``, so the fix lands in
    the environment that failed, whether agenthicc was installed with pip, uv, ``uv tool`` or
    from a checkout. Nothing here comes from configuration.
    """
    python = shlex.quote(executable if executable is not None else sys.executable)
    requirement = shlex.quote(REQUIREMENT)
    return (
        f"{python} -m pip install {requirement}"
        f"  (or: uv pip install --python {python} {requirement})"
    )


@dataclass(frozen=True, slots=True)
class McpClientStatus:
    """Whether the MCP client library is usable, and why not."""

    available: bool
    #: ``""`` when available; otherwise ``dependency_missing``, ``dependency_incompatible``
    #: or ``dependency_broken``.
    reason: str = ""
    installed: str = ""
    detail: str = ""
    fix: str = ""

    def summary(self) -> str:
        """One line naming the cause, with no fix and no secrets."""
        if self.available:
            return "the MCP client is available"
        if self.reason == "dependency_missing":
            return f"{PACKAGE} is not installed"
        if self.reason == "dependency_incompatible":
            return (
                f"{PACKAGE} {self.installed or 'of unknown version'} is older than the "
                f"{'.'.join(map(str, MINIMUM_VERSION))} agenthicc needs"
            )
        return f"{PACKAGE} {self.installed} is installed but cannot be imported: {self.detail}"

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready form for ``mcp doctor``, ``mcp list`` and ``/mcp``."""
        return {
            "client_available": self.available,
            "package": PACKAGE,
            "installed": self.installed,
            "required": REQUIREMENT,
            "reason": self.reason,
            "cause": "" if self.available else self.summary(),
            "fix": self.fix,
        }


class McpUnavailable(RuntimeError):
    """The MCP client library cannot be used; carries the :class:`McpClientStatus` that says why."""

    def __init__(self, status: McpClientStatus) -> None:
        super().__init__(f"{status.summary()}. Fix: {status.fix}")
        self.status = status


def check_mcp_client(*, imported: bool | None = None, import_error: str = "") -> McpClientStatus:
    """Decide whether the MCP client library is usable, without importing it.

    ``imported`` is what the caller knows about an import it already tried: ``False`` means
    the import failed (``import_error`` is its text), ``True`` or ``None`` that it succeeded
    or was not attempted. A library that is installed but failed to import is reported as
    ``dependency_broken`` rather than "not installed".
    """
    fix = fix_command()
    if importlib.util.find_spec(_MODULE) is None:
        return McpClientStatus(False, "dependency_missing", fix=fix)
    try:
        installed = importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        installed = ""
    parsed = _parse_version(installed) if installed else None
    if imported is False:
        return McpClientStatus(
            False, "dependency_broken", installed, import_error or "import failed", fix
        )
    if parsed is not None and parsed < MINIMUM_VERSION:
        return McpClientStatus(False, "dependency_incompatible", installed, fix=fix)
    return McpClientStatus(True, installed=installed)
