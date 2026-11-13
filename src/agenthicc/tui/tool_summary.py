"""Human-readable tool summaries for the transcript (TUX-06).

Every tool call becomes one transcript row: an operation (``Read``, ``$``,
``Search``), a *target* (the path, command or query) and an optional quiet
*detail* (``in objective-create-a/``, ``→ 14 matches``).  This module owns
that projection and the bounded data a ``tool_complete`` event carries, so
the event log holds data, never markup, and the renderer formats it with the
active theme at display time.

Built-in tool families register here beside their operation name.  A plugin
tool supplies its own summary with :func:`summarized_by`; any other tool is
summarised by its first string argument.

The module is pure: it never imports Rich and never touches the terminal.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

__all__ = [
    "ARG_KEY_LIMIT",
    "ARG_VALUE_LIMIT",
    "OUTPUT_HEAD_LINES",
    "OUTPUT_TAIL_LINES",
    "SUMMARY_KEY",
    "ArgValue",
    "OutputProjection",
    "Summarizer",
    "SummaryInput",
    "ToolFamily",
    "ToolPresentation",
    "ToolSummary",
    "bounded_args",
    "command_summary",
    "middle_truncate",
    "output_projection",
    "register_tool_summary",
    "relative_path",
    "summarize_call",
    "summarized_by",
    "test_summary",
    "tool_presentation",
]

#: The lauren-ai tool-metadata key a plugin's summarizer is stored under.
SUMMARY_KEY = "summarize"
#: ``args`` keeps at most this many keys …
ARG_KEY_LIMIT = 8
#: … each value at most this many characters.
ARG_VALUE_LIMIT = 512
#: Lines kept from the start and from the end of a tool's output.
OUTPUT_HEAD_LINES = 8
OUTPUT_TAIL_LINES = 32
#: The longest target a summarizer produces; the renderer fits it to the width.
_TARGET_LIMIT = 160
_LINE_LIMIT = 400

ArgValue = str | int | float | bool | None

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "passwd",
        "password",
        "secret",
        "token",
        "value",
    }
)
_SENSITIVE_MARKERS = (
    "api_key=",
    "authorization:",
    "bearer ",
    "cookie=",
    "password=",
    "passwd=",
    "secret=",
    "token=",
)


class ToolFamily(StrEnum):
    """How the transcript presents a tool's result."""

    COMMAND = "command"
    """Runs a process: adds one result row and shows its exit code."""
    READ = "read"
    WRITE = "write"
    SEARCH = "search"
    LIST = "list"
    GIT = "git"
    MEMORY = "memory"
    WEB = "web"
    MCP = "mcp"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ToolSummary:
    """The human target of one call and an optional quiet detail."""

    target: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SummaryInput:
    """What a summarizer may look at.

    ``result`` is the tool's return value (``None`` when it raised); ``root``
    is the workspace root, against which paths are made relative.
    """

    name: str
    args: Mapping[str, object]
    result: object
    root: Path


Summarizer = Callable[[SummaryInput], "ToolSummary | str"]


@dataclass(frozen=True, slots=True)
class ToolPresentation:
    """A tool's operation word, family and summarizer."""

    operation: str
    family: ToolFamily = ToolFamily.OTHER
    summarize: Summarizer | None = None


@dataclass(frozen=True, slots=True)
class OutputProjection:
    """A bounded view of a tool's output: the head, then the tail.

    ``lines[:head]`` are the first lines of the output and ``lines[head:]``
    its last lines; ``total - len(lines)`` lines between them were dropped.
    """

    lines: tuple[str, ...]
    head: int
    total: int


_REGISTRY: dict[str, ToolPresentation] = {}


def register_tool_summary(
    name: str,
    *,
    operation: str,
    family: ToolFamily = ToolFamily.OTHER,
    summarize: Summarizer | None = None,
) -> None:
    """Register how the transcript shows calls to the tool *name*."""
    _REGISTRY[name] = ToolPresentation(operation, family, summarize)


_F = TypeVar("_F")


def summarized_by(
    summarize: Summarizer,
    *,
    operation: str = "",
    family: ToolFamily = ToolFamily.OTHER,
) -> Callable[[_F], _F]:
    """Decorate a plugin tool with its own transcript summary.

    Stack it with ``@tool()`` like the capability decorators::

        @summarized_by(lambda call: f"ticket {call.args['id']}", operation="Jira")
        @tool_network_read
        @tool()
        async def jira_issue(id: str) -> dict[str, object]: ...
    """

    def decorate(target: _F) -> _F:
        from lauren_ai._tools import set_metadata

        return set_metadata(SUMMARY_KEY, ToolPresentation(operation, family, summarize))(target)

    return decorate


def presentation_from_metadata(tool: object) -> ToolPresentation | None:
    """The :func:`summarized_by` presentation attached to *tool*, if any."""
    from agenthicc.tools.capabilities import _tool_metadata

    metadata = _tool_metadata(tool)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(SUMMARY_KEY)
    return value if isinstance(value, ToolPresentation) else None


def _title(name: str) -> str:
    return name.replace("_", " ").strip().title() or "Tool"


def tool_presentation(name: str, override: ToolPresentation | None = None) -> ToolPresentation:
    """The presentation for *name*: an override, the registry, or a default."""
    registered = _REGISTRY.get(name)
    if override is not None:
        return ToolPresentation(
            override.operation or (registered.operation if registered else _title(name)),
            override.family,
            override.summarize,
        )
    if registered is not None:
        return registered
    if name.startswith("mcp:"):
        parts = name.split(":", 2)
        server, tool = (parts[1], parts[2]) if len(parts) == 3 else ("mcp", name[4:])
        return ToolPresentation(f"{server} · {tool}", ToolFamily.MCP)
    return ToolPresentation(_title(name))


# ── text helpers ──────────────────────────────────────────────────────────────


def middle_truncate(text: str, limit: int, *, path: bool = False, ellipsis: str = "…") -> str:
    """*text* in at most *limit* characters, keeping its start and its end.

    With *path*, ``src/agenthicc/tui/workspace/appender.py`` becomes
    ``src/…/appender.py`` so the filename always shows.  The renderer passes
    the theme's *ellipsis* glyph.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return ellipsis[:limit]
    if path and "/" in text.strip("/"):
        first = text.split("/", 1)[0]
        name = text.rstrip("/").rsplit("/", 1)[-1]
        for candidate in (f"{first}/{ellipsis}/{name}", f"{ellipsis}/{name}"):
            if len(candidate) <= limit:
                return candidate
    keep = limit - len(ellipsis)
    left = (keep + 1) // 2
    right = keep - left
    return text[:left] + ellipsis + (text[-right:] if right else "")


def relative_path(path: str, root: Path) -> str:
    """*path* relative to *root* when it is inside it."""
    if not path:
        return path
    try:
        candidate = Path(path)
        if candidate.is_absolute():
            relative = candidate.relative_to(root)
            text = relative.as_posix()
            return text if text != "." else "./"
    except ValueError:
        return path
    return path[2:] if path.startswith("./") and len(path) > 2 else path


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _bounded(text: str, limit: int = _TARGET_LIMIT, *, path: bool = False) -> str:
    return middle_truncate(_one_line(text), limit, path=path)


def _safe(key: str, value: object) -> str:
    """A redacted, single-line string for an argument value."""
    if key.casefold() in _SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = ", ".join(_safe(key, item) for item in value[:3])
        if len(value) > 3:
            text += ", …"
    elif value is None or isinstance(value, (bool, int, float)):
        text = str(value)
    else:
        text = json.dumps(value, default=str, ensure_ascii=False)
    if any(marker in text.casefold() for marker in _SENSITIVE_MARKERS):
        return "<redacted>"
    return text


def bounded_args(args: Mapping[str, object]) -> dict[str, ArgValue]:
    """A bounded, redacted projection of a call's arguments for the event log."""
    projected: dict[str, ArgValue] = {}
    for key, value in list(args.items())[:ARG_KEY_LIMIT]:
        if key.casefold() in _SENSITIVE_KEYS:
            projected[key] = "<redacted>"
        elif value is None or isinstance(value, (bool, int, float)):
            projected[key] = value
        else:
            text = _safe(key, value) if not isinstance(value, str) else value
            if any(marker in text.casefold() for marker in _SENSITIVE_MARKERS):
                text = "<redacted>"
            projected[key] = (
                text if len(text) <= ARG_VALUE_LIMIT else text[: ARG_VALUE_LIMIT - 1] + "…"
            )
    return projected


def _first_string(args: Mapping[str, object]) -> str:
    for key, value in args.items():
        if isinstance(value, str) and value.strip():
            return _bounded(_safe(key, value))
    return ""


def _mapping(result: object) -> Mapping[str, object]:
    return result if isinstance(result, Mapping) else {}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _plural(count: int, word: str, plural: str = "") -> str:
    return f"{count:,} {word if count == 1 else plural or word + 's'}"


# ── command summaries ─────────────────────────────────────────────────────────

_CHAIN = re.compile(r"\s*&&\s*")


def command_summary(command: str, cwd: str = "") -> ToolSummary:
    """``$ npm test`` for ``cd /long/path && npm test``, with ``in path/``.

    Leading ``cd X &&`` steps become the quiet ``in X/`` detail; a multi-line
    script shows its first line and ``+N lines``.
    """
    lines = [line for line in command.strip().splitlines() if line.strip()]
    if not lines:
        return ToolSummary("")
    first = lines[0].strip()
    directory = cwd.strip()
    steps = _CHAIN.split(first)
    while len(steps) > 1 and re.match(r"^cd(\s|$)", steps[0]):
        target = steps.pop(0)[2:].strip()
        try:
            parts = shlex.split(target)
        except ValueError:
            parts = [target]
        if parts:
            directory = parts[0]
    shown = " && ".join(steps)
    details: list[str] = []
    if directory:
        name = directory.rstrip("/").rsplit("/", 1)[-1] or directory
        details.append(f"in {name}/")
    if len(lines) > 1:
        details.append(f"+{_plural(len(lines) - 1, 'line')}")
    return ToolSummary(_bounded(shown), " · ".join(details))


def _run_bash(call: SummaryInput) -> ToolSummary:
    command = call.args.get("command")
    cwd = call.args.get("cwd")
    return command_summary(
        command if isinstance(command, str) else "", cwd if isinstance(cwd, str) else ""
    )


def _run_command(call: SummaryInput) -> ToolSummary:
    argv = call.args.get("argv")
    words = [str(word) for word in argv] if isinstance(argv, list) else []
    cwd = call.args.get("cwd")
    return command_summary(shlex.join(words), cwd if isinstance(cwd, str) else "")


def _run_code(call: SummaryInput) -> ToolSummary:
    code = call.args.get("code") or call.args.get("expression") or ""
    lines = [line for line in str(code).splitlines() if line.strip()]
    if not lines:
        return ToolSummary("")
    more = f"+{_plural(len(lines) - 1, 'line')}" if len(lines) > 1 else ""
    return ToolSummary(_bounded(lines[0]), more)


# ── test summaries ────────────────────────────────────────────────────────────

_PYTEST = re.compile(
    r"^=*\s*((?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?|deselected)"
    r"(?:, )?)+)(?: in [\d.]+s)?.*=*\s*$"
)
_JEST = re.compile(r"^\s*Tests:\s+(.+?)\s*$")
_VITEST = re.compile(r"^\s*Tests\s+(.+?)\s*$")
_CARGO = re.compile(r"^test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored")
_GO = re.compile(r"^(ok|FAIL|---\s*FAIL:?)\s+(\S+)(?:\s+([\d.]+s))?")


def test_summary(lines: Sequence[str]) -> str | None:
    """A one-line test result from pytest, jest, vitest, cargo or go test output."""
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        match = _PYTEST.match(line)
        if match:
            return match.group(1).strip().rstrip(",")
        match = _JEST.match(line)
        if match and re.search(r"\d+ (passed|failed|total)", match.group(1)):
            return match.group(1)
        match = _VITEST.match(line)
        if match and re.search(r"\d+ (passed|failed)", match.group(1)):
            return re.sub(r"\s*\(\d+\)\s*$", "", match.group(1))
        match = _CARGO.match(line)
        if match:
            status, passed, failed, ignored = match.groups()
            parts = [f"{passed} passed", f"{failed} failed"]
            if ignored != "0":
                parts.append(f"{ignored} ignored")
            return ", ".join(parts)
    go_packages = [line.strip() for line in lines if _GO.match(line.strip())]
    if go_packages:
        failed = sum(1 for line in go_packages if not line.startswith("ok"))
        passed = len(go_packages) - failed
        parts = [_plural(passed, "package") + " ok"] if passed else []
        if failed:
            parts.append(f"{failed} failed")
        return ", ".join(parts)
    return None


# ── file, search and memory summaries ─────────────────────────────────────────


def _path_arg(call: SummaryInput, key: str = "path") -> str:
    value = call.args.get(key)
    return relative_path(value, call.root) if isinstance(value, str) else ""


def _read_file(call: SummaryInput) -> ToolSummary:
    path = _path_arg(call)
    offset = _int(call.args.get("offset")) or 1
    limit = _int(call.args.get("limit")) or 0
    if limit > 0:
        path += f":{offset}-{offset + limit - 1}"
    elif offset > 1:
        path += f":{offset}-"
    return ToolSummary(_bounded(path, path=True))


def _batch_read(call: SummaryInput) -> ToolSummary:
    paths = call.args.get("paths")
    items = (
        [relative_path(p, call.root) for p in paths if isinstance(p, str)]
        if isinstance(paths, list)
        else []
    )
    if len(items) == 1:
        return ToolSummary(_bounded(items[0], path=True))
    return ToolSummary(_plural(len(items), "file"), _bounded(", ".join(items[:3]), 80))


def _diff_counts(result: object) -> str:
    diff = _mapping(result).get("diff")
    if not isinstance(diff, str) or not diff:
        return ""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return f"+{added} −{removed}" if added or removed else ""


def _write(call: SummaryInput) -> ToolSummary:
    if "source" in call.args and "destination" in call.args:
        target = f"{_path_arg(call, 'source')} → {_path_arg(call, 'destination')}"
        return ToolSummary(_bounded(target))
    return ToolSummary(_bounded(_path_arg(call), path=True), _diff_counts(call.result))


def _location(call: SummaryInput) -> str:
    path = _path_arg(call)
    if not path or path in {".", "./"}:
        return ""
    return path if path.endswith("/") or "." in path.rsplit("/", 1)[-1] else path + "/"


def _search(call: SummaryInput) -> ToolSummary:
    pattern = call.args.get("pattern", call.args.get("query", ""))
    query = f'"{_bounded(_safe("pattern", pattern), 60)}"'
    location = _location(call)
    target = f"{query} in {location}" if location else query
    result = _mapping(call.result)
    count = _int(result.get("total_matches"))
    word = "match"
    if count is None:
        count = _int(result.get("count"))
        word = "file" if call.name == "search_files" else "match"
    detail = (
        f"→ {_plural(count, word, 'matches' if word == 'match' else '')}"
        if count is not None
        else ""
    )
    return ToolSummary(target, detail)


def _list(call: SummaryInput) -> ToolSummary:
    path = _path_arg(call) or "."
    target = path if path.endswith("/") else path + "/"
    count = _int(_mapping(call.result).get("count"))
    return ToolSummary(
        _bounded(target, path=True),
        f"→ {_plural(count, 'entry', 'entries')}" if count is not None else "",
    )


def _git(call: SummaryInput) -> ToolSummary:
    command = call.name.removeprefix("git_")
    words = [command]
    for key in ("ref", "branch", "path", "pattern", "action"):
        value = call.args.get(key)
        if isinstance(value, str) and value:
            words.append(relative_path(value, call.root) if key == "path" else value)
    if command == "diff" and call.args.get("staged") is True:
        words.insert(1, "--staged")
    paths = call.args.get("paths")
    if isinstance(paths, list):
        words.extend(str(p) for p in paths[:3])
    result = _mapping(call.result)
    detail = ""
    if command == "status":
        changed = sum(
            len(value)
            for key in ("staged", "unstaged", "untracked")
            if isinstance(value := result.get(key), (list, dict))
        )
        detail = f"→ {changed:,} changed" if result else ""
    elif command == "diff":
        detail = _diff_counts(result)
    elif command == "log":
        commits = result.get("commits")
        detail = f"→ {_plural(len(commits), 'commit')}" if isinstance(commits, list) else ""
    return ToolSummary(_bounded(" ".join(words)), detail)


def _memory(call: SummaryInput) -> ToolSummary:
    key = call.args.get("key", call.args.get("query", ""))
    target = (
        f'memory: "{_bounded(_safe("key", key) if call.name != "memory_write" else str(key), 60)}"'
    )
    result = _mapping(call.result)
    detail = ""
    if call.name == "memory_read" and result:
        detail = "found" if result.get("found") is True else "not found"
    elif call.name == "semantic_search":
        hits = result.get("results")
        if isinstance(hits, list):
            detail = f"({_plural(len(hits), 'entry', 'entries')})"
    return ToolSummary(target, detail)


def _publish(call: SummaryInput) -> ToolSummary:
    artifact = _mapping(call.result).get("artifact_id")
    content_type = call.args.get("content_type", "text/plain")
    return ToolSummary(f"artifact {content_type}", str(artifact)[:16] if artifact else "")


def _web(call: SummaryInput) -> ToolSummary:
    url = call.args.get("url")
    if isinstance(url, str) and url:
        parts = urlsplit(url)
        return ToolSummary(_bounded(f"{parts.netloc}{parts.path}".rstrip("/") or url))
    query = call.args.get("query")
    count = _int(_mapping(call.result).get("count"))
    detail = f"→ {_plural(count, 'result')}" if count is not None else ""
    return ToolSummary(f'"{_bounded(str(query or ""), 80)}"', detail)


def _generic(call: SummaryInput) -> ToolSummary:
    url = call.args.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return _web(call)
    return ToolSummary(_first_string(call.args))


def summarize_call(
    name: str,
    args: Mapping[str, object],
    result: object,
    *,
    root: Path | None = None,
    presentation: ToolPresentation | None = None,
) -> ToolSummary:
    """The transcript summary of one call; never raises.

    A summarizer that fails, or returns something else than a string or
    :class:`ToolSummary`, falls back to the first string argument.
    """
    call = SummaryInput(name, args, result, root or Path(os.getcwd()))
    chosen = tool_presentation(name, presentation)
    summarize = chosen.summarize or _generic
    try:
        summary = summarize(call)
    except Exception:  # noqa: BLE001 - a plugin summarizer must not break the turn
        summary = _generic(call)
    if isinstance(summary, str):
        return ToolSummary(_bounded(summary))
    if isinstance(summary, ToolSummary):
        return ToolSummary(_bounded(summary.target), _bounded(summary.detail, 80))
    return _generic(call)


# ── output projection ─────────────────────────────────────────────────────────


def _output_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        if isinstance(stdout, str) or isinstance(stderr, str):
            parts = [part for part in (stdout, stderr) if isinstance(part, str) and part]
            return "\n".join(part.rstrip("\n") for part in parts)
        for key in ("content", "output", "result", "error"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def output_projection(result: object) -> OutputProjection:
    """The first and last lines of a tool's output, with the total count."""
    text = _output_text(result)
    lines = text.splitlines() or ([text] if text else [])
    total = len(lines)
    if total <= OUTPUT_HEAD_LINES + OUTPUT_TAIL_LINES:
        kept = lines
        head = total
    else:
        kept = lines[:OUTPUT_HEAD_LINES] + lines[-OUTPUT_TAIL_LINES:]
        head = OUTPUT_HEAD_LINES
    return OutputProjection(
        tuple(line if len(line) <= _LINE_LIMIT else line[: _LINE_LIMIT - 1] + "…" for line in kept),
        head,
        total,
    )


def exit_code_of(result: object) -> int | None:
    """The process exit code a command result reports, if it ran."""
    result_map = _mapping(result)
    code = _int(result_map.get("returncode"))
    if code is None or code < 0:
        return None
    state = result_map.get("state")
    return code if state in (None, "exited", "failed") else None


def error_code_of(result: object) -> str | None:
    """The TOOLR-04 ``error_code`` of a failed result."""
    result_map = _mapping(result)
    code = result_map.get("error_code")
    return code if result_map.get("ok") is False and isinstance(code, str) and code else None


# ── built-in registrations ────────────────────────────────────────────────────

_C, _R, _W, _S, _L, _G, _M, _N = (
    ToolFamily.COMMAND,
    ToolFamily.READ,
    ToolFamily.WRITE,
    ToolFamily.SEARCH,
    ToolFamily.LIST,
    ToolFamily.GIT,
    ToolFamily.MEMORY,
    ToolFamily.WEB,
)

for _name, _operation, _family, _summarize in (
    ("run_bash", "$", _C, _run_bash),
    ("run_command", "$", _C, _run_command),
    ("run_python", "Python", _C, _run_code),
    ("run_python_expr", "Python", _C, _run_code),
    ("run_tests", "Test", _C, None),
    ("inspect_terminal", "Inspect", ToolFamily.OTHER, None),
    ("wait_terminal", "Wait", ToolFamily.OTHER, None),
    ("wait_terminal_ready", "Wait", ToolFamily.OTHER, None),
    ("stop_terminal", "Stop", ToolFamily.OTHER, None),
    ("read_file", "Read", _R, _read_file),
    ("batch_read", "Read", _R, _batch_read),
    ("read_agenthicc_doc", "Read", _R, None),
    ("get_file_info", "Inspect", _R, _write),
    ("checksum_file", "Checksum", _R, _write),
    ("inspect_agenthicc_source", "Inspect", _R, None),
    ("write_file", "Write", _W, _write),
    ("modify_file", "Edit", _W, _write),
    ("patch_file", "Edit", _W, _write),
    ("apply_diff", "Edit", _W, _write),
    ("append_file", "Append", _W, _write),
    ("truncate_file", "Truncate", _W, _write),
    ("touch_file", "Create", _W, _write),
    ("make_directory", "Create", _W, _write),
    ("delete_file", "Delete", _W, _write),
    ("move_file", "Move", _W, _write),
    ("copy_file", "Copy", _W, _write),
    ("grep_files", "Search", _S, _search),
    ("search_files", "Find", _S, _search),
    ("search_agenthicc_docs", "Search", _S, _search),
    ("search_agenthicc_source", "Search", _S, _search),
    ("git_grep", "Search", _S, _search),
    ("list_directory", "List", _L, _list),
    ("list_agenthicc_docs", "List", _L, None),
    ("git_status", "Git", _G, _git),
    ("git_diff", "Git", _G, _git),
    ("git_log", "Git", _G, _git),
    ("git_show", "Git", _G, _git),
    ("git_branch", "Git", _G, _git),
    ("git_blame", "Git", _G, _git),
    ("git_add", "Git", _G, _git),
    ("git_commit", "Git", _G, _git),
    ("git_checkout", "Git", _G, _git),
    ("git_stash", "Git", _G, _git),
    ("memory_read", "Read", _M, _memory),
    ("memory_write", "Write", _M, _memory),
    ("semantic_search", "Search", _M, _memory),
    ("publish_artifact", "Publish", _M, _publish),
    ("fetch_page", "Fetch", _N, _web),
    ("web_search", "Search", _N, _web),
):
    register_tool_summary(_name, operation=_operation, family=_family, summarize=_summarize)
