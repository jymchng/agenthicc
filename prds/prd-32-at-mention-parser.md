---
title: "PRD-32: @Mention Parser — Syntax, Extraction, and Path Resolution"
status: draft
version: 0.1.0
created: 2026-06-12
---

# PRD-32: @Mention Parser

## Context

Typing `@src/auth.py` in the input bar currently triggers an autocomplete
dropdown (via `AtMentionCompleter`) but the submitted text reaches the LLM
as the plain string `"Review @src/auth.py"`.  The LLM must call `read_file`
by itself — there is no automatic injection.  This PRD specifies the
**parser** that extracts `@mention` tokens from submitted text so downstream
processors (PRD-33) can inject file content before the message reaches the LLM.

---

## Goals

| ID | Goal |
|----|------|
| G1 | Parse all `@path` tokens from a message string, preserving token positions |
| G2 | Resolve each path against the session working directory |
| G3 | Classify each token: `file`, `directory`, `glob`, `url`, or `unresolved` |
| G4 | Leave the rest of the message text unchanged (only token metadata is extracted) |
| G5 | A mention whose path does not exist is classified `unresolved` (not an error) |
| G6 | URL mentions (`@https://…`) are classified `url` and excluded from file resolution |
| G7 | Glob patterns (`@src/**/*.py`) are classified `glob`; expansion is delegated to PRD-33 |
| G8 | The parser is a pure function — no I/O, no side effects, fully testable |

## Non-Goals
- Reading file content (PRD-33)
- Truncation / token budgeting (PRD-33)
- UI completion (already in `input_bar.py`)

---

## Syntax Rules

```
mention    ::= "@" path
path       ::= url | glob | fs_path
url        ::= ("http://" | "https://") rest
glob       ::= fs_path containing "*" or "?"
fs_path    ::= relative or absolute filesystem path

Delimiters (end a mention):
  whitespace, newline, comma, semicolon, closing bracket/paren/quote
```

### Examples

| Input token | Type | Resolved path |
|---|---|---|
| `@README.md` | `file` | `{cwd}/README.md` |
| `@src/auth.py` | `file` | `{cwd}/src/auth.py` |
| `@src/` | `directory` | `{cwd}/src/` |
| `@src/**/*.py` | `glob` | pattern kept as-is |
| `@https://example.com` | `url` | kept as-is |
| `@nonexistent.txt` | `unresolved` | `{cwd}/nonexistent.txt` |

---

## Data Structures

```python
# src/agenthicc/mentions/parser.py

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

__all__ = ["MentionKind", "Mention", "parse_mentions"]


class MentionKind(str, Enum):
    FILE        = "file"
    DIRECTORY   = "directory"
    GLOB        = "glob"
    URL         = "url"
    UNRESOLVED  = "unresolved"


@dataclass
class Mention:
    """A single @mention token extracted from user input."""
    raw: str              # the original token including @, e.g. "@src/auth.py"
    path: str             # the path/URL part, e.g. "src/auth.py"
    kind: MentionKind
    resolved: Path | None # absolute Path for file/directory/unresolved; None for url/glob
    start: int            # character offset of "@" in the original string
    end: int              # character offset after the last char of the token
```

---

## Parser Implementation

```python
# src/agenthicc/mentions/parser.py  (continued)

# Regex: @  followed by non-whitespace / non-delimiter chars.
# Stops at whitespace, ,;)]'"  (common natural-language delimiters).
_MENTION_RE = re.compile(r"@([^\s,;)\]'\"]+)")

_URL_PREFIXES = ("http://", "https://")
_GLOB_CHARS   = frozenset("*?[")


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
            mentions.append(Mention(
                raw=raw, path=path_str, kind=MentionKind.URL,
                resolved=None, start=start, end=end,
            ))
            continue

        # Glob
        if any(c in path_str for c in _GLOB_CHARS):
            mentions.append(Mention(
                raw=raw, path=path_str, kind=MentionKind.GLOB,
                resolved=None, start=start, end=end,
            ))
            continue

        # File system path — resolve relative to cwd
        resolved = (base / path_str).resolve()
        if resolved.is_file():
            kind = MentionKind.FILE
        elif resolved.is_dir() or path_str.endswith("/"):
            kind = MentionKind.DIRECTORY
        else:
            kind = MentionKind.UNRESOLVED

        mentions.append(Mention(
            raw=raw, path=path_str, kind=kind,
            resolved=resolved, start=start, end=end,
        ))

    return mentions


def strip_mentions(text: str, mentions: list[Mention]) -> str:
    """Return *text* with all mention tokens replaced by just the path.

    e.g. "Review @src/auth.py please" → "Review src/auth.py please"
    Useful for the agent context where the @ prefix is noise.
    """
    result = text
    # Replace right-to-left so offsets stay valid
    for m in sorted(mentions, key=lambda x: x.start, reverse=True):
        result = result[: m.start] + m.path + result[m.end :]
    return result
```

---

## Package Init

```python
# src/agenthicc/mentions/__init__.py
"""@mention parsing and file content injection (PRD-32, PRD-33)."""
from .parser import MentionKind, Mention, parse_mentions, strip_mentions

__all__ = ["MentionKind", "Mention", "parse_mentions", "strip_mentions"]
```

---

## Tests

```python
# tests/unit/test_mention_parser.py

import pytest
from pathlib import Path
from agenthicc.mentions.parser import MentionKind, parse_mentions, strip_mentions

pytestmark = pytest.mark.unit


def test_parse_file_mention(tmp_path):
    (tmp_path / "README.md").write_text("hello")
    mentions = parse_mentions("Read @README.md please", cwd=tmp_path)
    assert len(mentions) == 1
    m = mentions[0]
    assert m.path == "README.md"
    assert m.kind == MentionKind.FILE
    assert m.resolved == (tmp_path / "README.md").resolve()


def test_parse_directory_mention(tmp_path):
    (tmp_path / "src").mkdir()
    mentions = parse_mentions("Look at @src/", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.DIRECTORY


def test_parse_url_mention(tmp_path):
    mentions = parse_mentions("See @https://example.com/doc", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.URL
    assert mentions[0].resolved is None


def test_parse_glob_mention(tmp_path):
    mentions = parse_mentions("Load @src/**/*.py", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.GLOB


def test_parse_unresolved_mention(tmp_path):
    mentions = parse_mentions("Check @does_not_exist.txt", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.UNRESOLVED


def test_multiple_mentions(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    mentions = parse_mentions("Compare @a.py and @b.py", cwd=tmp_path)
    assert len(mentions) == 2
    assert {m.path for m in mentions} == {"a.py", "b.py"}


def test_no_mentions(tmp_path):
    assert parse_mentions("Hello world", cwd=tmp_path) == []


def test_mention_stops_at_comma(tmp_path):
    (tmp_path / "file.py").write_text("")
    mentions = parse_mentions("Read @file.py,please", cwd=tmp_path)
    assert mentions[0].path == "file.py"


def test_mention_stops_at_whitespace(tmp_path):
    mentions = parse_mentions("@foo bar", cwd=tmp_path)
    assert mentions[0].path == "foo"


def test_strip_mentions_removes_at_prefix(tmp_path):
    (tmp_path / "auth.py").write_text("")
    mentions = parse_mentions("Review @auth.py for issues", cwd=tmp_path)
    stripped = strip_mentions("Review @auth.py for issues", mentions)
    assert stripped == "Review auth.py for issues"


def test_start_end_positions(tmp_path):
    (tmp_path / "f.py").write_text("")
    mentions = parse_mentions("x @f.py y", cwd=tmp_path)
    text = "x @f.py y"
    m = mentions[0]
    assert text[m.start:m.end] == "@f.py"
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mention_parser.py -v
```
