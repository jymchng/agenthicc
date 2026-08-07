from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tools.workspace_access import WorkspaceScope

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


# Regex: a single @ followed by a non-delimited token.
#
# ``@`` is deliberately both a left-boundary guard and a token delimiter:
# ``@@./agenthicc`` must remain ordinary text rather than becoming the invalid
# mention ``@./agenthicc``.  The remaining delimiters preserve the existing
# prose behavior for commas, brackets, quotes, and sentence punctuation.
# Backslashes are not delimiters because they are path separators on Windows.
_MENTION_RE = re.compile(r"(?<!@)@([^\s@,;)\]'\"]+)")

_URL_PREFIXES = ("http://", "https://")
_GLOB_CHARS = frozenset("*?[")
_TRAILING_SENTENCE_PUNCTUATION = frozenset("?!.,:")


def _strip_existing_path_punctuation(path_str: str, base: Path) -> str:
    """Remove sentence punctuation when the resulting path exists.

    A question such as ``"what is @README.md?"`` should mention
    ``README.md`` rather than turn the terminal ``?`` into a glob wildcard.
    Only existing paths are normalised here so legitimate glob patterns and
    filenames containing punctuation keep their original meaning.
    """
    resolved_original = (base / path_str).resolve()
    if resolved_original.is_file() or resolved_original.is_dir():
        return path_str

    candidate = path_str
    while candidate and candidate[-1] in _TRAILING_SENTENCE_PUNCTUATION:
        trimmed = candidate[:-1]
        if not trimmed:
            break
        resolved = (base / trimmed).resolve()
        if resolved.is_file() or resolved.is_dir():
            return trimmed
        candidate = trimmed
    return path_str


def parse_mentions(
    text: str,
    cwd: Path | None = None,
    workspace_scope: "WorkspaceScope | None" = None,
) -> list[Mention]:
    """Extract and classify all @mention tokens from *text*.

    Args:
        text: Raw user message.
        cwd:  Working directory for path resolution (default: Path.cwd()).

    Returns:
        Ordered list of Mention objects.  Overlapping matches are impossible
        given the regex; ordering matches left-to-right occurrence in *text*.
    """
    base = (cwd or Path.cwd()).resolve()
    mentions: list[Mention] = []

    for m in _MENTION_RE.finditer(text):
        path_str = m.group(1)
        start = m.start()

        # A terminal question mark is usually prose punctuation, but is also
        # a valid glob wildcard.  Prefer punctuation when the path without it
        # resolves to a real file or directory.
        if not any(path_str.startswith(p) for p in _URL_PREFIXES) and workspace_scope is None:
            path_str = _strip_existing_path_punctuation(path_str, base)

        end = start + 1 + len(path_str)
        raw = text[start:end]

        # URL
        if any(path_str.startswith(p) for p in _URL_PREFIXES):
            mentions.append(
                Mention(
                    raw=raw,
                    path=path_str,
                    kind=MentionKind.URL,
                    resolved=None,
                    start=start,
                    end=end,
                )
            )
            continue

        # Glob
        if any(c in path_str for c in _GLOB_CHARS):
            scope_status = "unknown"
            resolved_glob: Path | None = None
            root_id: str | None = None
            if workspace_scope is not None:
                resolved_scope = workspace_scope.resolve_pattern(
                    path_str,
                    operation="search",
                    probe_exists=False,
                )
                scope_status = resolved_scope.status.value
                resolved_glob = resolved_scope.absolute
                root_id = resolved_scope.root_id
            mentions.append(
                Mention(
                    raw=raw,
                    path=path_str,
                    kind=MentionKind.GLOB,
                    resolved=resolved_glob,
                    start=start,
                    end=end,
                    scope_status=scope_status,
                    root_id=root_id,
                )
            )
            continue

        # File system path — resolve relative to cwd.  When a session scope is
        # present, classify the canonical target before checking file type so
        # an outside target cannot be silently treated as a normal mention.
        scope_status = "unknown"
        root_id: str | None = None
        if workspace_scope is not None:
            scoped = workspace_scope.resolve(
                path_str,
                operation="read",
                probe_exists=False,
            )
            resolved = scoped.absolute
            scope_status = scoped.status.value
            root_id = scoped.root_id
        else:
            resolved = (base / path_str).resolve()
        if scope_status == "outside_workspace":
            kind = MentionKind.OUT_OF_SCOPE
        elif resolved.is_file():
            kind = MentionKind.FILE
        elif resolved.is_dir():
            kind = MentionKind.DIRECTORY
        # Non-existent trailing-slash paths fall through to UNRESOLVED so that
        # resolve_mention returns a soft-error block instead of raising.
        else:
            kind = (
                MentionKind.OUT_OF_SCOPE
                if scope_status == "outside_workspace"
                else MentionKind.UNRESOLVED
            )

        mentions.append(
            Mention(
                raw=raw,
                path=path_str,
                kind=kind,
                resolved=resolved,
                start=start,
                end=end,
                scope_status=scope_status,
                root_id=root_id,
            )
        )

    return mentions


def strip_mentions(text: str, mentions: list[Mention]) -> str:
    """Return *text* with all mention tokens replaced by just the path.

    e.g. "Review @src/auth.py please" -> "Review src/auth.py please"
    Useful for the agent context where the @ prefix is noise.
    """
    result = text
    # Replace right-to-left so offsets stay valid
    for m in sorted(mentions, key=lambda x: x.start, reverse=True):
        result = result[: m.start] + m.path + result[m.end :]
    return result
