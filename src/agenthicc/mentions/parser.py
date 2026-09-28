from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tools.workspace_access import ResolvedWorkspacePath, WorkspaceScope

__all__ = ["MentionKind", "Mention", "parse_mentions", "strip_mentions"]


class MentionKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    GLOB = "glob"
    URL = "url"
    UNRESOLVED = "unresolved"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass
class Mention:
    """A single @mention token extracted from user input."""

    raw: str  # the original token including @, e.g. "@src/auth.py"
    path: str  # the path/URL part, e.g. "src/auth.py"
    kind: MentionKind
    resolved: Path | None  # absolute Path for file/directory/unresolved; None for url/glob
    start: int  # character offset of "@" in the original string
    end: int  # character offset after the last char of the token
    scope_status: str = "unknown"
    root_id: str | None = None


# Mention tokenization is intentionally more conservative than a generic
# ``@``-word regex.  The parser is used on every submitted user message, so
# an unresolved bare identifier such as ``@bookTicker`` must not become a
# filesystem lookup merely because it follows an at sign.  The scanner below
# accepts a bare token only when it resolves to an existing target; unresolved
# candidates need explicit path evidence (a separator, prefix, extension,
# glob, or URL scheme).
_URL_PREFIXES = ("http://", "https://")
_GLOB_CHARS = frozenset("*?[")
_TRAILING_SENTENCE_PUNCTUATION = frozenset("?!.,:")
_OPENING_BOUNDARIES = frozenset("([{<")
_TOKEN_DELIMITERS = frozenset(" \t\r\n@,;)]}'\"`")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_valid_mention_boundary(text: str, index: int) -> bool:
    """Return whether ``text[index]`` can begin a mention.

    The interactive picker and the submitted-message parser share this rule:
    mentions begin at the start of input, after whitespace, or after a prose
    opening delimiter.  Restricting the left boundary prevents addresses such
    as ``contact@example.com`` and word-adjacent identifiers from becoming
    implicit file reads.
    """

    if index <= 0:
        return True
    previous = text[index - 1]
    return previous.isspace() or previous in _OPENING_BOUNDARIES


def _is_valid_mention_position(text: str, index: int) -> bool:
    """Return whether a mention may begin at ``index`` outside Markdown code."""

    if not _is_valid_mention_boundary(text, index):
        return False
    return not any(start <= index < end for start, end in _markdown_code_ranges(text))


def _fence_at(text: str, index: int) -> tuple[str, int] | None:
    """Return a Markdown fence's character and length at a line start."""

    if index > 0 and text[index - 1] != "\n":
        return None
    cursor = index
    while cursor < len(text) and text[cursor] in " \t" and cursor - index < 4:
        cursor += 1
    if cursor - index > 3 or cursor >= len(text) or text[cursor] not in "`~":
        return None
    char = text[cursor]
    end = cursor
    while end < len(text) and text[end] == char:
        end += 1
    if end - cursor < 3:
        return None
    return char, end - cursor


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline


def _fence_closes(text: str, index: int, char: str, minimum_length: int) -> int | None:
    """Find the end of a closing Markdown fence at or after ``index``."""

    cursor = index
    while cursor < len(text):
        end = _line_end(text, cursor)
        line = text[cursor:end]
        stripped = line.lstrip(" \t")
        indentation = len(line) - len(stripped)
        if indentation <= 3 and stripped.startswith(char):
            run_end = 0
            while run_end < len(stripped) and stripped[run_end] == char:
                run_end += 1
            if run_end >= minimum_length and not stripped[run_end:].strip():
                return end + 1 if end < len(text) else end
        cursor = end + 1 if end < len(text) else len(text)
    return None


def _markdown_code_ranges(text: str) -> list[tuple[int, int]]:
    """Return ranges that must be treated as literal Markdown code.

    This is deliberately a small, defensive scanner rather than a Markdown
    parser.  Fenced blocks and inline backtick spans are enough to prevent
    source/protocol examples from triggering mentions.  Unmatched delimiters
    protect the remainder of the message, which is the safe failure mode for
    user-provided input.
    """

    ranges: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        fence = _fence_at(text, index)
        if fence is not None:
            char, fence_length = fence
            opening_end = _line_end(text, index)
            closing_end = _fence_closes(
                text,
                opening_end + 1 if opening_end < length else length,
                char,
                fence_length,
            )
            ranges.append((index, closing_end if closing_end is not None else length))
            index = closing_end if closing_end is not None else length
            continue

        if text[index] == "`":
            run_end = index
            while run_end < length and text[run_end] == "`":
                run_end += 1
            run_length = run_end - index
            delimiter = "`" * run_length
            closing = text.find(delimiter, run_end)
            if closing < 0:
                ranges.append((index, length))
                break
            ranges.append((index, closing + run_length))
            index = closing + run_length
            continue

        index += 1
    return ranges


def _protected_range_at(
    index: int,
    ranges: list[tuple[int, int]],
    range_index: int,
) -> tuple[bool, int, int]:
    """Return ``(protected, next_index, next_range_index)`` for a position."""

    while range_index < len(ranges) and index >= ranges[range_index][1]:
        range_index += 1
    if range_index < len(ranges):
        start, end = ranges[range_index]
        if start <= index < end:
            return True, end, range_index
    return False, index, range_index


def _scan_token_end(text: str, start: int) -> int:
    """Scan a candidate token, retaining balanced glob character classes."""

    index = start + 1
    bracket_depth = 0
    while index < len(text):
        char = text[index]
        if char == "[":
            bracket_depth += 1
        elif char == "]":
            if bracket_depth:
                bracket_depth -= 1
            else:
                break
        elif bracket_depth == 0 and char in _TOKEN_DELIMITERS:
            break
        index += 1
    return index


def _resolve_unscoped(path_str: str, base: Path) -> Path | None:
    """Resolve a path without allowing malformed input to escape the parser."""

    try:
        path = Path(path_str).expanduser()
        return (path if path.is_absolute() else base / path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _scope_status(value: "ResolvedWorkspacePath") -> str:
    return value.status.value


def _scope_resolve(
    path_str: str,
    workspace_scope: "WorkspaceScope",
) -> "ResolvedWorkspacePath | None":
    try:
        return workspace_scope.resolve(path_str, operation="read", probe_exists=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _path_exists(
    path_str: str,
    base: Path,
    workspace_scope: "WorkspaceScope | None",
) -> bool:
    """Check only bounded target metadata needed for bare-name recognition."""

    if workspace_scope is not None:
        scoped = _scope_resolve(path_str, workspace_scope)
        if scoped is None or _scope_status(scoped) == "outside_workspace":
            return False
        return scoped.exists is True

    resolved = _resolve_unscoped(path_str, base)
    if resolved is None:
        return False
    try:
        return resolved.is_file() or resolved.is_dir()
    except (OSError, RuntimeError, ValueError):
        return False


def _strip_existing_path_punctuation(
    path_str: str,
    base: Path,
    workspace_scope: "WorkspaceScope | None" = None,
) -> str:
    """Remove sentence punctuation when the resulting path exists.

    A question such as ``"what is @README.md?"`` should mention
    ``README.md`` rather than turn the terminal ``?`` into a glob wildcard.
    Only existing paths are normalised here so legitimate glob patterns and
    filenames containing punctuation keep their original meaning.
    """

    if _path_exists(path_str, base, workspace_scope):
        return path_str

    candidate = path_str
    while candidate and candidate[-1] in _TRAILING_SENTENCE_PUNCTUATION:
        trimmed = candidate[:-1]
        if not trimmed:
            break
        if _path_exists(trimmed, base, workspace_scope):
            return trimmed
        candidate = trimmed
    return path_str


def _has_path_evidence(path_str: str) -> bool:
    """Return whether an unresolved token explicitly looks like a target."""

    if any(path_str.startswith(prefix) for prefix in _URL_PREFIXES):
        return True
    if any(char in path_str for char in _GLOB_CHARS):
        return True
    if "/" in path_str or "\\" in path_str:
        return True
    if path_str.startswith((".", "~")) or path_str.startswith("/"):
        return True
    if _DRIVE_PATH_RE.match(path_str) or path_str.startswith("\\\\"):
        return True
    # A dot in an otherwise bare token is retained as backwards-compatible
    # evidence for unresolved filenames such as ``@missing.txt``.
    return "." in path_str


def _classify_path(
    path_str: str,
    base: Path,
    workspace_scope: "WorkspaceScope | None",
) -> tuple[MentionKind, Path | None, str, str | None] | None:
    """Classify an accepted non-URL/non-glob path candidate safely."""

    resolved: Path | None
    if workspace_scope is not None:
        scoped = _scope_resolve(path_str, workspace_scope)
        if scoped is None:
            return None
        resolved = scoped.absolute
        if resolved is None:
            return None
        status = _scope_status(scoped)
        root_id = scoped.root_id
        if status == "outside_workspace":
            return MentionKind.OUT_OF_SCOPE, resolved, status, root_id
    else:
        resolved = _resolve_unscoped(path_str, base)
        if resolved is None:
            return None
        status = "unknown"
        root_id = None

    try:
        if resolved.is_file():
            kind = MentionKind.FILE
        elif resolved.is_dir():
            kind = MentionKind.DIRECTORY
        else:
            kind = MentionKind.UNRESOLVED
    except (OSError, RuntimeError, ValueError):
        kind = MentionKind.UNRESOLVED
    return kind, resolved, status, root_id


def parse_mentions(
    text: str,
    cwd: Path | None = None,
    workspace_scope: "WorkspaceScope | None" = None,
) -> list[Mention]:
    """Extract and classify supported @mention tokens from *text*.

    A bare token is accepted only when it resolves to an existing file or
    directory.  Missing targets must use an explicit path shape (for example
    ``@./missing.py`` or ``@docs/missing``), which prevents technical prose
    such as ``@bookTicker`` from entering mention injection.

    Args:
        text: Raw user message.
        cwd: Working directory for path resolution (default: ``Path.cwd()``).
        workspace_scope: Optional scope used for canonical classification and
            policy-consistent outside-workspace status.

    Returns:
        Ordered list of Mention objects.  Markdown code spans and fenced code
        blocks are treated as literal text.
    """

    base = (cwd or Path.cwd()).resolve()
    mentions: list[Mention] = []
    protected_ranges = _markdown_code_ranges(text)
    protected_index = 0
    index = 0

    while index < len(text):
        protected, next_index, protected_index = _protected_range_at(
            index,
            protected_ranges,
            protected_index,
        )
        if protected:
            index = next_index
            continue
        if text[index] != "@" or not _is_valid_mention_boundary(text, index):
            index += 1
            continue

        token_end = _scan_token_end(text, index)
        path_str = text[index + 1 : token_end]
        if not path_str:
            index += 1
            continue

        # A terminal question mark is usually prose punctuation, but is also
        # a valid glob wildcard.  Prefer punctuation when the path without it
        # resolves to a real file or directory.
        if not any(path_str.startswith(prefix) for prefix in _URL_PREFIXES):
            path_str = _strip_existing_path_punctuation(path_str, base, workspace_scope)

        # Explicit URL and glob forms do not require a filesystem target.
        if any(path_str.startswith(prefix) for prefix in _URL_PREFIXES):
            kind = MentionKind.URL
            resolved = None
            scope_status = "unknown"
            root_id = None
        elif any(char in path_str for char in _GLOB_CHARS):
            scope_status = "unknown"
            resolved = None
            root_id = None
            if workspace_scope is not None:
                try:
                    scoped = workspace_scope.resolve_pattern(
                        path_str,
                        operation="search",
                        probe_exists=False,
                    )
                except (OSError, RuntimeError, ValueError):
                    scoped = None
                if scoped is None:
                    index = token_end
                    continue
                scope_status = _scope_status(scoped)
                resolved = scoped.absolute
                root_id = scoped.root_id
            kind = MentionKind.GLOB
        else:
            # Bare names are accepted only when they identify an existing
            # target.  Explicit path-shaped candidates retain unresolved
            # diagnostics for typos and missing files.
            if not _has_path_evidence(path_str) and not _path_exists(
                path_str,
                base,
                workspace_scope,
            ):
                index = token_end
                continue
            classified = _classify_path(path_str, base, workspace_scope)
            if classified is None:
                index = token_end
                continue
            kind, resolved, scope_status, root_id = classified

        end = index + 1 + len(path_str)
        mentions.append(
            Mention(
                raw=text[index:end],
                path=path_str,
                kind=kind,
                resolved=resolved,
                start=index,
                end=end,
                scope_status=scope_status,
                root_id=root_id,
            )
        )
        # Continue at the scanner's token boundary.  If punctuation was
        # trimmed, the punctuation is intentionally revisited as ordinary
        # text, never as part of the accepted mention.
        index = token_end

    return mentions


def strip_mentions(text: str, mentions: list[Mention]) -> str:
    """Return *text* with all mention tokens replaced by just the path.

    e.g. ``"Review @src/auth.py please"`` -> ``"Review src/auth.py please"``
    Useful for the agent context where the ``@`` prefix is noise.
    """

    result = text
    # Replace right-to-left so offsets stay valid
    for mention in sorted(mentions, key=lambda item: item.start, reverse=True):
        result = result[: mention.start] + mention.path + result[mention.end :]
    return result
