"""What a tool-approval dialog shows: the action explained (TUX-07 §2.2).

:func:`approval_content` turns an :class:`~agenthicc.tools.approval.ApprovalRequest`
into a title (a verb phrase), a one-line summary, a bounded body and the full
details ``Ctrl+T`` shows:

- **execute** — the whole command, syntax-highlighted and wrapped, never cut;
  a ``cd X && …`` prefix (or a ``cwd`` argument) is shown as "in X";
- **write** — a unified diff against the current file, read through the
  workspace scope, with ``+N -M`` totals per file; a new file shows its first
  40 lines, marked "new file";
- **delete / move / copy** — the paths with their sizes, and "N files,
  M directories" for a directory;
- **network** — the method, host and path, and the allow-list entry that
  admits the host or the fact that none does;
- **workspace access** — the exact targets, the workspace root and the mode;
  raw arguments are never shown, since writes carry file contents and
  commands can carry credentials;
- **anything else** (MCP and plugin tools) — the arguments as a key/value
  table, values wrapped rather than cut.

Nothing here redacts more or less than before: the projection rules are
unchanged. Large content is bounded at :data:`PREVIEW_LINES` lines in the
body, with "+N more lines · ctrl+t".
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from agenthicc.tools.approval import capability_values
from agenthicc.tui.theme import current_theme, glyphs, palette

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.tools.approval import ApprovalRequest
    from agenthicc.tools.workspace_access import WorkspaceScope

__all__ = [
    "ApprovalContent",
    "PREVIEW_LINES",
    "approval_content",
    "grant_noun",
    "request_announcement",
]

#: Body rows of content shown before "+N more lines · ctrl+t".
PREVIEW_LINES = 40
#: Files larger than this are not read for a diff.
_MAX_DIFF_BYTES = 512 * 1024
#: Entries counted under a directory that would be deleted.
_MAX_WALK = 10_000

_EXECUTE_TOOLS = frozenset(
    {"run_bash", "run_command", "run_python", "run_python_expr", "run_tests"}
)
_WRITE_TOOLS = frozenset({"write_file", "patch_file", "apply_diff", "batch_write"})
_CD_PREFIX = re.compile(r"""^\s*cd\s+("[^"]+"|'[^']+'|\S+)\s*&&\s*(.+)$""", re.DOTALL)

#: Grant nouns for "Yes, and allow … this session", in reading order.
_GRANT_NOUNS: tuple[tuple[str, str], ...] = (
    ("execute", "commands"),
    ("write", "file changes"),
    ("git_write", "git changes"),
    ("network", "network access"),
    ("undeclared", "tools without declared capabilities"),
)


@dataclass(frozen=True)
class ApprovalContent:
    """The explained action: what the dialog's title, body and details show."""

    title: str
    summary: str
    body: tuple[RenderableType, ...]
    details: tuple[RenderableType, ...] | None = None


def grant_noun(capabilities: frozenset[str]) -> str:
    """What a turn or session grant covers: ``commands``, ``file changes`` …"""
    values = capability_values(capabilities)
    nouns = [noun for name, noun in _GRANT_NOUNS if name in values]
    if not nouns:
        return "this tool"
    return nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " and " + nouns[-1]


def _humanize(tool_name: str) -> str:
    words = tool_name.replace("__", " ").replace("_", " ").replace("-", " ").split()
    return " ".join(words) or "a tool"


def _text_arg(args: Mapping[str, object], key: str) -> str:
    value = args.get(key)
    return value if isinstance(value, str) else ""


def _bounded(lines: list[str]) -> tuple[list[str], int]:
    if len(lines) <= PREVIEW_LINES:
        return lines, 0
    return lines[:PREVIEW_LINES], len(lines) - PREVIEW_LINES


def _more(hidden: int) -> Text:
    return Text(f"+{hidden} more lines {glyphs().sep} ctrl+t", style=palette().muted)


def _code(code: str, language: str) -> Syntax:
    return Syntax(
        code,
        language,
        theme=current_theme().syntax_theme,
        word_wrap=True,
        background_color="default",
    )


# ── execute ──────────────────────────────────────────────────────────────────


def _split_cd(command: str) -> tuple[str, str]:
    """``("dir", "rest")`` for ``cd dir && rest``, else ``("", command)``."""
    match = _CD_PREFIX.match(command)
    if match is None:
        return "", command
    directory = match.group(1)
    if directory[:1] in ("'", '"'):
        directory = directory[1:-1]
    return directory, match.group(2).strip()


def _execute(tool: str, args: Mapping[str, object]) -> ApprovalContent:
    p = palette()
    title = "Run a command"
    language = "bash"
    if tool == "run_command":
        argv = args.get("argv")
        items = [str(item) for item in argv] if isinstance(argv, list) else []
        command = shlex.join(items)
    elif tool == "run_python":
        title, language = "Run Python code", "python"
        command = _text_arg(args, "code")
    elif tool == "run_python_expr":
        title, language = "Evaluate a Python expression", "python"
        command = _text_arg(args, "expression")
    elif tool == "run_tests":
        title = "Run the tests"
        extra = args.get("args")
        parts = [
            _text_arg(args, "framework") or "pytest",
            _text_arg(args, "path") or "tests/",
            *([str(item) for item in extra] if isinstance(extra, list) else []),
        ]
        command = shlex.join(parts)
    else:
        command = _text_arg(args, "command")
    directory, shown = _split_cd(command) if language == "bash" else ("", command)
    cwd = _text_arg(args, "cwd")
    where = directory or cwd
    body: list[RenderableType] = []
    if where:
        body.append(Text(f"in {where}", style=p.muted))
    prefix = "$ " if language == "bash" else ""
    lines, hidden = _bounded(shown.splitlines() or [""])
    body.append(_code(prefix + "\n".join(lines), language))
    if hidden:
        body.append(_more(hidden))
    details: list[RenderableType] = []
    if where:
        details.append(Text(f"in {where}", style=p.muted))
    details.append(_code(prefix + shown, language))
    summary = command.splitlines()[0] if command else ""
    return ApprovalContent(title, summary, tuple(body), tuple(details) if hidden else None)


# ── write ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FileChange:
    display: str
    old: list[str] | None
    new: list[str]
    #: A unified diff supplied by the tool (``apply_diff``).
    diff_text: str = ""


def _read_current(path: str, scope: WorkspaceScope | None) -> tuple[str, list[str] | None]:
    """``(display path, current lines or None)`` read inside the workspace scope."""
    if scope is None or not path:
        return path, None
    try:
        resolved = scope.resolve(path, operation="write", probe_exists=False)
    except (OSError, ValueError):
        return path, None
    if not resolved.in_scope:
        return resolved.display, None
    target = resolved.absolute
    try:
        if not target.is_file() or target.stat().st_size > _MAX_DIFF_BYTES:
            return resolved.display, None
        return resolved.display, target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return resolved.display, None


def _changes(
    tool: str, args: Mapping[str, object], scope: WorkspaceScope | None
) -> list[_FileChange]:
    if tool == "batch_write":
        files = args.get("files")
        changes: list[_FileChange] = []
        for item in files if isinstance(files, list) else []:
            if isinstance(item, Mapping):
                changes.extend(_changes("write_file", item, scope))
        return changes
    path = _text_arg(args, "path")
    display, current = _read_current(path, scope)
    if tool == "write_file":
        return [_FileChange(display, current, _text_arg(args, "content").splitlines())]
    if tool == "apply_diff":
        return [_FileChange(display, current, [], diff_text=_text_arg(args, "diff"))]
    old_content = _text_arg(args, "old_content")
    new_content = _text_arg(args, "new_content")
    if current is not None:
        text = "\n".join(current)
        if old_content and old_content in text:
            count = -1 if args.get("replace_all") is True else 1
            return [
                _FileChange(
                    display, current, text.replace(old_content, new_content, count).splitlines()
                )
            ]
    # The file could not be read, or the text is not in it: show the replacement.
    return [_FileChange(display, old_content.splitlines(), new_content.splitlines())]


def _language(path: str) -> str:
    try:
        return Syntax.guess_lexer(path, "")
    except Exception:  # noqa: BLE001 - highlighting is best-effort
        return "text"


def _diff_lines(change: _FileChange) -> list[str]:
    if change.diff_text:
        return change.diff_text.splitlines()
    return list(
        difflib.unified_diff(
            change.old or [], change.new, f"a/{change.display}", f"b/{change.display}", lineterm=""
        )
    )


def _totals(lines: Sequence[str]) -> tuple[int, int]:
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _styled_diff(lines: Sequence[str]) -> Text:
    p = palette()
    out = Text()
    for index, line in enumerate(lines):
        if index:
            out.append("\n")
        if line.startswith(("+++", "---")):
            out.append(line, "bold")
        elif line.startswith("@@"):
            out.append(line, p.info)
        elif line.startswith("+"):
            out.append(line, f"{p.diff_add} {p.on('diff_add_bg')}")
        elif line.startswith("-"):
            out.append(line, f"{p.diff_del} {p.on('diff_del_bg')}")
        else:
            out.append(line)
    return out


def _totals_row(display: str, added: int, removed: int, *, new: bool = False) -> Text:
    p = palette()
    row = Text(display, style="bold")
    if new:
        row.append("  new file", p.muted)
    row.append(f"  +{added}", p.diff_add)
    row.append(f" -{removed}", p.diff_del)
    return row


def _write(tool: str, args: Mapping[str, object], scope: WorkspaceScope | None) -> ApprovalContent:
    from agenthicc.tui.diff_renderer import render_file_create, render_file_diff

    changes = _changes(tool, args, scope)
    body: list[RenderableType] = []
    details: list[RenderableType] = []
    total_added = total_removed = 0
    created = 0
    for change in changes:
        diff = _diff_lines(change)
        added, removed = _totals(diff)
        total_added += added
        total_removed += removed
        language = _language(change.display)
        is_new = change.old is None and not change.diff_text
        created += is_new
        body.append(_totals_row(change.display, added, removed, new=is_new))
        if change.diff_text:
            shown, hidden = _bounded(diff)
            body.append(_styled_diff(shown))
            if hidden:
                body.append(_more(hidden))
        elif is_new:
            body.append(
                render_file_create(
                    change.display, change.new, max_lines=PREVIEW_LINES, language=language
                )
            )
        else:
            body.append(
                render_file_diff(change.display, change.old or [], change.new, language=language)
            )
        details.append(_totals_row(change.display, added, removed, new=is_new))
        details.append(_styled_diff(diff))
        details.append(Text(""))
    count = len(changes)
    if tool == "batch_write":
        title = f"Write {count} file{'s' if count != 1 else ''}"
    elif created == count and count:
        title = "Create a file"
    else:
        title = "Edit a file"
    names = ", ".join(change.display for change in changes) or "no files"
    summary = f"{names}, +{total_added} -{total_removed}"
    return ApprovalContent(title, summary, tuple(body), tuple(details))


# ── delete, move, copy ───────────────────────────────────────────────────────


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _describe_path(path: str, scope: WorkspaceScope | None) -> tuple[str, str]:
    """``(display, "12.0 KB" | "3 files, 1 directory" | "")`` inside the scope."""
    if scope is None or not path:
        return path, ""
    try:
        resolved = scope.resolve(path, operation="metadata", probe_exists=False)
    except (OSError, ValueError):
        return path, ""
    if not resolved.in_scope:
        return resolved.display, ""
    target = resolved.absolute
    try:
        if target.is_file():
            return resolved.display, _size(target.stat().st_size)
        if target.is_dir():
            files = directories = 0
            for _root, dirs, names in os.walk(target):
                files += len(names)
                directories += len(dirs)
                if files + directories > _MAX_WALK:
                    break
            return (
                resolved.display,
                f"{files} file{'s' if files != 1 else ''}, "
                f"{directories} director{'ies' if directories != 1 else 'y'}",
            )
    except OSError:
        pass
    return resolved.display, ""


def _paths(tool: str, args: Mapping[str, object], scope: WorkspaceScope | None) -> ApprovalContent:
    p, g = palette(), glyphs()
    body: list[RenderableType] = []
    if tool == "delete_file":
        display, size = _describe_path(_text_arg(args, "path"), scope)
        row = Text(display, style="bold")
        if size:
            row.append(f"  {size}", p.muted)
        body.append(row)
        return ApprovalContent("Delete a file", display, tuple(body))
    if tool == "make_directory":
        display, _size_text = _describe_path(_text_arg(args, "path"), scope)
        return ApprovalContent("Create a directory", display, (Text(display, style="bold"),))
    source, size = _describe_path(_text_arg(args, "source"), scope)
    destination, _existing = _describe_path(_text_arg(args, "destination"), scope)
    row = Text(source, style="bold")
    if size:
        row.append(f"  {size}", p.muted)
    body.append(row)
    body.append(Text(f"{g.right} {destination}"))
    verb = "Move a file" if tool == "move_file" else "Copy a file"
    return ApprovalContent(verb, f"{source} to {destination}", tuple(body))


# ── network ──────────────────────────────────────────────────────────────────


def _network(tool: str, args: Mapping[str, object]) -> ApprovalContent | None:
    url = _text_arg(args, "url")
    if not url:
        return None
    from agenthicc.tools.http import network_policy

    p = palette()
    parts = urlsplit(url)
    host = parts.hostname or ""
    method = (_text_arg(args, "method") or "GET").upper()
    target = f"{method} {host}{parts.path or '/'}"
    policy = network_policy()
    if policy.allow_all:
        rule = "network_allow_all admits any public host"
    else:
        entry = next(
            (
                domain
                for domain in policy.allowed_domains
                if host == domain or host.endswith(f".{domain}")
            ),
            "",
        )
        rule = (
            f"allowed by network_allow_list entry {entry}"
            if entry
            else "not on [security] network_allow_list: the request will be refused"
        )
    body = (Text(target, style="bold"), Text(rule, style=p.muted))
    return ApprovalContent("Access the network", target, body)


# ── generic ──────────────────────────────────────────────────────────────────


def _value_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _arguments(args: Mapping[str, object], *, bounded: bool) -> Table:
    p = palette()
    table = Table.grid(padding=(0, 2))
    table.add_column(style=p.muted, no_wrap=True)
    table.add_column(overflow="fold")
    for key, value in args.items():
        lines = _value_text(value).splitlines() or [""]
        shown, hidden = _bounded(lines) if bounded else (lines, 0)
        cell = Text("\n".join(shown))
        if hidden:
            cell.append("\n")
            cell.append_text(_more(hidden))
        table.add_row(escape(str(key)), cell)
    return table


def _generic(tool: str, args: Mapping[str, object]) -> ApprovalContent:
    total = sum(len(_value_text(value).splitlines()) for value in args.values())
    body: list[RenderableType] = [_arguments(args, bounded=True)] if args else []
    if not args:
        body.append(Text("no arguments", style=palette().muted))
    details = (_arguments(args, bounded=False),) if total > PREVIEW_LINES else None
    summary = ""
    for key, value in args.items():
        first_line = (_value_text(value).splitlines() or [""])[0]
        summary = f"{key} {first_line}".strip()
        break
    return ApprovalContent(f"Use {_humanize(tool)}", summary, tuple(body), details)


# ── workspace access ─────────────────────────────────────────────────────────


def _scope(req: ApprovalRequest) -> ApprovalContent:
    from agenthicc.tools.workspace_access import WorkspacePathStatus

    p = palette()
    items = req.workspace_access
    body: list[RenderableType] = []
    for access in items:
        row = Text(f"{access.operation}: ", style=p.muted)
        row.append(access.display)
        body.append(row)
    root = items[0].workspace_root
    if root is not None:
        row = Text("workspace: ", style=p.muted)
        row.append(str(root))
        body.append(row)
    mode = Text("mode: ", style=p.muted)
    mode.append(items[0].mode)
    body.append(mode)
    count = len(items)
    title = (
        "Edit executable extensions"
        if any(access.status == WorkspacePathStatus.PROTECTED_CODE.value for access in items)
        else "Access outside the workspace"
    )
    summary = items[0].display if count == 1 else f"{count} targets"
    return ApprovalContent(title, summary, tuple(body))


def approval_content(req: ApprovalRequest, scope: WorkspaceScope | None) -> ApprovalContent:
    """The title, summary, body and details for a tool or workspace approval."""
    if req.workspace_access:
        # Never the raw arguments: writes carry contents, commands credentials.
        return _scope(req)
    tool = req.tool_name
    args = req.tool_input or {}
    if tool in _EXECUTE_TOOLS:
        return _execute(tool, args)
    if tool in _WRITE_TOOLS:
        return _write(tool, args, scope)
    if tool in ("delete_file", "move_file", "copy_file", "make_directory"):
        return _paths(tool, args, scope)
    capabilities = capability_values(req.capabilities)
    if "network" in capabilities:
        network = _network(tool, args)
        if network is not None:
            return network
    return _generic(tool, args)


def request_announcement(req: ApprovalRequest) -> str:
    """The screen-reader sentence for the dialog *req* opens (AGX-13)."""
    from agenthicc.tui.workspace.dialog import announcement
    from agenthicc.tui.workspace.overlays.approval import approval_options
    from agenthicc.tui.workspace.overlays.plan_approval import PLAN_TITLE, plan_options
    from agenthicc.tui.workspace.overlays.questions import QUESTIONS_TITLE, question_summary

    if req.kind == "plan_review":
        return announcement(
            PLAN_TITLE, "the plan is shown below", req.reason, len(plan_options(req))
        )
    if req.kind == "questions":
        summary, options = question_summary(req)
        return announcement(QUESTIONS_TITLE, summary, req.reason, options)
    content = approval_content(req, None)
    return announcement(content.title, content.summary, req.reason, len(approval_options(req)))
