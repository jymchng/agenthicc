from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = ["MentionKind", "Mention", "parse_mentions", "strip_mentions"]


class MentionKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    GLOB = "glob"
    URL = "url"
    UNRESOLVED = "unresolved"


@dataclass
class Mention:
    """A single @mention token extracted from user input."""

    raw: str  # the original token including @, e.g. "@src/auth.py"
    path: str  # the path/URL part, e.g. "src/auth.py"
    kind: MentionKind
    resolved: Path | None  # absolute Path for file/directory/unresolved; None for url/glob
    start: int  # character offset of "@" in the original string
    end: int  # character offset after the last char of the token


# Regex: @ followed by non-whitespace / non-delimiter chars.
# Stops at whitespace, ,;)]'"  (common natural-language delimiters).
_MENTION_RE = re.compile(r"@([^\s,;)\]'\"]+)")

_URL_PREFIXES = ("http://", "https://")
_GLOB_CHARS = frozenset("*?[")


def parse_mentions(
    text: str,
    cwd: Path | None = None,
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
        start, end = m.start(), m.end()
        raw = m.group(0)

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
            mentions.append(
                Mention(
                    raw=raw,
                    path=path_str,
                    kind=MentionKind.GLOB,
                    resolved=None,
                    start=start,
                    end=end,
                )
            )
            continue

        # File system path — resolve relative to cwd
        resolved = (base / path_str).resolve()
        if resolved.is_file():
            kind = MentionKind.FILE
        elif resolved.is_dir():
            kind = MentionKind.DIRECTORY
        # Non-existent trailing-slash paths fall through to UNRESOLVED so that
        # resolve_mention returns a soft-error block instead of raising.
        else:
            kind = MentionKind.UNRESOLVED

        mentions.append(
            Mention(
                raw=raw,
                path=path_str,
                kind=kind,
                resolved=resolved,
                start=start,
                end=end,
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
