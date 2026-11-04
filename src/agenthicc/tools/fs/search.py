"""Content search for ``grep_files``: one query, two backends, one result (TOOLR-10).

Both backends search exactly the files :func:`~agenthicc.tools.fs.discovery.discover`
returned, so the file set — ignore rules, caps and workspace authorization
(TUIR-01, TUIR-02) — is identical by construction. They differ only in who runs
the regular expression:

- :func:`search_with_ripgrep` hands the files to ``rg --json`` in batches when
  ``rg`` is on ``PATH``. A pattern ``rg`` cannot compile (a look-around or a
  back-reference) falls back to Python, so a valid Python pattern always works.
- :func:`search_with_python` runs :mod:`re` in a worker thread.

Lines are split on ``\\n`` with a trailing ``\\r`` removed, bytes that are not
UTF-8 are replaced, and a file containing a NUL byte is binary and skipped —
the same rules on both sides, so they return the same matches.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

__all__ = [
    "MAX_CONTEXT_LINES",
    "GrepOutput",
    "GrepQuery",
    "GrepResult",
    "SearchFile",
    "compile_query",
    "filter_files",
    "ripgrep_path",
    "search_with_python",
    "search_with_ripgrep",
]

#: The most context lines a match carries on each side.
MAX_CONTEXT_LINES: Final = 5
#: Files larger than this are not searched (matches ``read_file``'s cap).
MAX_SEARCH_FILE_BYTES: Final = 10 * 1024 * 1024
#: Files handed to one ``rg`` process; keeps argv well under ``ARG_MAX``.
_RG_BATCH: Final = 2_000
#: The longest an ``rg`` batch may run before it is killed.
_RG_TIMEOUT_S: Final = 30.0

GrepOutput = Literal["matches", "files", "count"]


@dataclass(frozen=True, slots=True)
class SearchFile:
    """One file to search: its path relative to the search root, and on disk."""

    relative: str
    path: Path


@dataclass(frozen=True, slots=True)
class GrepQuery:
    """What to search for and what to return."""

    pattern: str
    case_sensitive: bool
    fixed_string: bool = False
    context_lines: int = 0
    max_results: int = 50
    output: GrepOutput = "matches"

    @classmethod
    def create(
        cls,
        pattern: str,
        *,
        case_sensitive: bool | None = None,
        fixed_string: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
        output: GrepOutput = "matches",
    ) -> GrepQuery:
        """A query; ``case_sensitive=None`` is smart case: sensitive iff *pattern* has a capital."""
        sensitive = (
            any(char.isupper() for char in pattern) if case_sensitive is None else case_sensitive
        )
        return cls(
            pattern=pattern,
            case_sensitive=sensitive,
            fixed_string=fixed_string,
            context_lines=max(0, min(MAX_CONTEXT_LINES, context_lines)),
            max_results=max(1, max_results),
            output=output,
        )


@dataclass(slots=True)
class GrepResult:
    """Everything both backends report, in the order the files were given."""

    matches: list[dict[str, object]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    total_matches: int = 0
    backend: Literal["python", "ripgrep"] = "python"

    @property
    def files_matched(self) -> int:
        return len(self.counts)

    @property
    def truncated(self) -> bool:
        return self.total_matches > len(self.matches)


def compile_query(query: GrepQuery) -> re.Pattern[str]:
    """The compiled pattern; raises :class:`re.error` when it is invalid."""
    source = re.escape(query.pattern) if query.fixed_string else query.pattern
    return re.compile(source, 0 if query.case_sensitive else re.IGNORECASE)


def filter_files(
    files: Sequence[SearchFile], include: Sequence[str], exclude: Sequence[str]
) -> list[SearchFile]:
    """*files* whose relative path matches an *include* glob and no *exclude* glob.

    A glob without ``/`` matches the file name (``*.py``); one with ``/``
    matches from the right of the relative path (``tests/*.py``).
    """

    def matches(relative: str, patterns: Sequence[str]) -> bool:
        path = PurePosixPath(relative)
        return any(path.match(pattern.removeprefix("**/")) for pattern in patterns)

    return [
        item
        for item in files
        if (not include or matches(item.relative, include))
        and not (exclude and matches(item.relative, exclude))
    ]


def _lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    pieces = text.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()
    return [piece.rstrip("\r") for piece in pieces]


def _record(
    result: GrepResult,
    query: GrepQuery,
    relative: str,
    numbered: dict[int, str],
    hits: list[tuple[int, int, int]],
) -> None:
    """Add one file's *hits* (line, start, end) to *result*, in line order."""
    if not hits:
        return
    hits.sort()
    result.counts[relative] = len(hits)
    for number, start, end in hits:
        result.total_matches += 1
        if query.output != "matches" or len(result.matches) >= query.max_results:
            continue
        entry: dict[str, object] = {
            "file": relative,
            "line_number": number,
            "line": numbered[number],
            "match_start": start,
            "match_end": end,
        }
        if query.context_lines:
            span = query.context_lines
            entry["context_before"] = [
                numbered[line] for line in range(number - span, number) if line in numbered
            ]
            entry["context_after"] = [
                numbered[line] for line in range(number + 1, number + span + 1) if line in numbered
            ]
        result.matches.append(entry)


def search_with_python(files: Sequence[SearchFile], query: GrepQuery) -> GrepResult:
    """Search *files* with :mod:`re`. Blocking: call it in a worker thread."""
    compiled = compile_query(query)
    result = GrepResult(backend="python")
    for item in files:
        try:
            if item.path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            data = item.path.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        lines = _lines(data)
        hits: list[tuple[int, int, int]] = []
        for number, line in enumerate(lines, 1):
            found = compiled.search(line)
            if found is not None:
                hits.append((number, found.start(), found.end()))
        numbered = dict(enumerate(lines, 1))
        _record(result, query, item.relative, numbered, hits)
    return result


def ripgrep_path() -> str | None:
    """The ``rg`` executable, or ``None`` when it is not installed."""
    return shutil.which("rg")


def _rg_bytes(value: object) -> bytes:
    """The raw bytes of an ``rg --json`` text field (``text``, or base64 ``bytes``)."""
    if not isinstance(value, dict):
        return b""
    text = value.get("text")
    if isinstance(text, str):
        return text.encode("utf-8")
    raw = value.get("bytes")
    if isinstance(raw, str):
        return base64.b64decode(raw)
    return b""


def _rg_text(value: object) -> str:
    return _rg_bytes(value).decode("utf-8", errors="replace")


class _PatternUnsupported(Exception):
    """``rg`` rejected a pattern Python accepts."""


_FileHits = tuple[dict[int, str], list[tuple[int, int, int]], bool]


def _rg_batch(
    rg: str,
    root: Path,
    batch: Sequence[SearchFile],
    query: GrepQuery,
    *,
    keep_lines: bool,
    deadline: float,
) -> dict[str, _FileHits]:
    """One blocking ``rg --json`` run: per relative path, its lines, hits and binary flag.

    Runs in a worker thread: the JSON stream is parsed off the event loop.
    With *keep_lines* false only hit positions are kept, which bounds memory
    once the result already holds ``max_results`` matches.
    """
    import subprocess
    import tempfile
    import time

    from agenthicc.tools.exec.child import child_env

    argv = [
        rg,
        "--json",
        "--no-config",
        "--no-ignore",
        "--hidden",
        "--no-follow",
        "--crlf",
        "--max-filesize",
        str(MAX_SEARCH_FILE_BYTES),
        "--case-sensitive" if query.case_sensitive else "--ignore-case",
    ]
    if query.fixed_string:
        argv.append("--fixed-strings")
    if query.context_lines and keep_lines:
        argv.extend(("--context", str(query.context_lines)))
    argv.extend(("--regexp", query.pattern, "--"))
    argv.extend(item.relative for item in batch)
    files: dict[str, _FileHits] = {}
    with (
        tempfile.TemporaryFile() as stderr,
        subprocess.Popen(
            argv,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=child_env(),
        ) as process,
    ):
        assert process.stdout is not None
        for raw in process.stdout:
            if time.monotonic() > deadline:
                process.kill()
                raise TimeoutError("rg exceeded its deadline")
            event = json.loads(raw)
            kind = event.get("type")
            data = event.get("data", {})
            if kind not in {"match", "context", "end"}:
                continue
            relative = _rg_text(data.get("path"))
            numbered, hits, _ = files.setdefault(relative, ({}, [], False))
            if kind == "end":
                files[relative] = (numbered, hits, data.get("binary_offset") is not None)
                continue
            number = int(data["line_number"])
            line_bytes = _rg_bytes(data.get("lines"))
            if keep_lines:
                numbered[number] = (
                    line_bytes.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                )
            if kind == "match":
                submatches = data.get("submatches") or [{"start": 0, "end": 0}]
                first = submatches[0]
                # rg reports byte offsets into the raw line; count characters
                # the way Python decodes it, replacement characters included.
                start = len(line_bytes[: int(first["start"])].decode("utf-8", errors="replace"))
                end = len(line_bytes[: int(first["end"])].decode("utf-8", errors="replace"))
                hits.append((number, start, end))
        returncode = process.wait()
        stderr.seek(0)
        message = stderr.read(4_096)
    if returncode == 2 and b"regex" in message.lower():
        raise _PatternUnsupported(message.decode("utf-8", errors="replace"))
    return files


def _search_with_ripgrep(
    rg: str, root: Path, files: Sequence[SearchFile], query: GrepQuery
) -> GrepResult | None:
    import time

    deadline = time.monotonic() + _RG_TIMEOUT_S
    result = GrepResult(backend="ripgrep")
    try:
        for start in range(0, len(files), _RG_BATCH):
            batch = files[start : start + _RG_BATCH]
            keep_lines = query.output == "matches" and len(result.matches) < query.max_results
            found = _rg_batch(rg, root, batch, query, keep_lines=keep_lines, deadline=deadline)
            for item in batch:
                entry = found.get(item.relative)
                if entry is None or entry[2]:
                    continue
                _record(result, query, item.relative, entry[0], entry[1])
    except (_PatternUnsupported, OSError, ValueError, KeyError, TimeoutError):
        return None
    return result


async def search_with_ripgrep(
    rg: str, root: Path, files: Sequence[SearchFile], query: GrepQuery
) -> GrepResult | None:
    """Search *files* (relative to *root*) with ``rg``; ``None`` to use Python instead.

    The whole run, including parsing ``rg``'s JSON stream, is in a worker
    thread. Batches go in the files' order, so the result's matches are the
    same ones, in the same order, as :func:`search_with_python` returns.
    """
    return await asyncio.to_thread(_search_with_ripgrep, rg, root, files, query)
