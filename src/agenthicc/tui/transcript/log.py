"""One pass over an append-only log: an offset index, search and windows (TUX-09).

The view never loads a whole log. :class:`LogIndex` reads it once, in 1 MiB
chunks, and keeps four small arrays per *item* (a line that shows something):
where it starts and ends, its kind and a flag. That is enough to number the
user turns, jump to the next failure, bisect a search hit to its item and find
the byte range a page of events is loaded from. The index only ever grows, and
:meth:`LogIndex.scan` picks up where the last one stopped, so a log that is
still being written is followed cheaply.

Reading and searching are meant for a worker thread. Both check a cancel event
between chunks, and every chunk is a handful of C-level ``bytes`` operations, so
the event loop is never held for more than a millisecond or two, even on a log
of 100 MB.

Two formats exist: the session's JSON-lines event log, and a plain text file
(the diagnostics log of ``/logs``).
"""

from __future__ import annotations

import bisect
import json
import re
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agenthicc.tui.transcript.text import matches, plain_text
from agenthicc.tui.workspace.appender import has_renderer

__all__ = [
    "EVENT_FORMAT",
    "FOLLOW_BYTES",
    "TEXT_FORMAT",
    "LogFormat",
    "LogIndex",
    "SearchJob",
]

#: Bytes read at a time while scanning and searching.
CHUNK_BYTES = 1 << 20
#: A line is classified from this many leading bytes (``event_id`` and ``kind`` come first).
_HEAD_BYTES = 512
#: The largest span read to search one item; a record beyond it is not searched.
_MAX_ITEM_BYTES = 8 << 20
#: The most a followed log is read in one ``refresh`` on the event loop.
FOLLOW_BYTES = 4 * CHUNK_BYTES

_KIND = re.compile(rb'"kind"\s*:\s*"([^"\\]{1,64})"')
_FAILED = re.compile(rb'"success"\s*:\s*false|"error_code"\s*:\s*"')
_EXIT_CODE = re.compile(rb'"exit_code"\s*:\s*(-?\d+)')
_STATE = re.compile(rb'"state"\s*:\s*"([a-z_]+)"')
_STATUS = re.compile(rb'"status"\s*:\s*"([a-z_]+)"')
#: ``compaction_status`` values that print no row (the live row is their start).
_SILENT_STATUSES = frozenset({b"started", b"token_count_unavailable"})
_FAILED_STATUSES = frozenset(
    {
        b"skipped_summary_unavailable",
        b"skipped_concurrent_update",
        b"blocked_durability_error",
        b"blocked_irreducible_context",
    }
)
_QUIET_STATES = frozenset({b"exited", b"running", b"waiting"})

#: Kinds that always call for attention (``{`` and ``}`` stop on them).
_ATTENTION_KINDS = frozenset(
    {
        "error",
        "tool_recovery",
        "mcp_server_failure",
        "phase_control_rejected",
        "subagent_worker_failed",
        "subagent_transcript_degraded",
    }
)
_TEXT_ATTENTION = re.compile(rb"\b(ERROR|CRITICAL|WARNING|Traceback)\b")


def _tool_failed(line: bytes) -> bool:
    """Whether a ``tool_complete`` line records a failure (the cheap, raw reading)."""
    if _FAILED.search(line):
        return True
    code = _EXIT_CODE.search(line)
    if code is not None and code.group(1) != b"0":
        return True
    state = _STATE.search(line)
    return state is not None and state.group(1) not in _QUIET_STATES


@dataclass(frozen=True)
class LogFormat:
    """How one kind of log is split into items and read back."""

    #: A line's ``(kind, needs_attention)``, or ``None`` when it shows nothing.
    classify: Callable[[bytes], tuple[str, bool] | None]
    #: A line's searchable text, or ``None`` when it cannot be read.
    text_of: Callable[[bytes], str | None]
    #: Whether a last line with no newline is complete as it stands.
    complete: Callable[[bytes], bool]


def _classify_event(line: bytes) -> tuple[str, bool] | None:
    found = _KIND.search(line, 0, _HEAD_BYTES)
    if found is None:
        return None
    kind = found.group(1).decode("ascii", "replace")
    if not has_renderer(kind):
        return None
    if kind == "tool_complete":
        return kind, _tool_failed(line)
    if kind == "compaction_status":
        # Only the result rows show (TUX-09 reads the appender's own rule).
        status = _STATUS.search(line)
        if status is None or status.group(1) in _SILENT_STATUSES:
            return None
        return kind, status.group(1) in _FAILED_STATUSES
    return kind, kind in _ATTENTION_KINDS


def _event_text(line: bytes) -> str | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    kind, payload = record.get("kind"), record.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict):
        return None
    return plain_text(kind, payload)


def _json_complete(line: bytes) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def _classify_text(line: bytes) -> tuple[str, bool] | None:
    return ("line", _TEXT_ATTENTION.search(line) is not None) if line.strip() else None


def _text_line(line: bytes) -> str | None:
    return line.decode("utf-8", "replace")


EVENT_FORMAT = LogFormat(_classify_event, _event_text, _json_complete)
TEXT_FORMAT = LogFormat(_classify_text, _text_line, lambda line: False)


def _prefilter(needle: str) -> bytes | None:
    """Raw bytes every line holding *needle* must contain, or ``None`` for no shortcut.

    The event log is JSON with non-ASCII characters, quotes and backslashes
    escaped, so only a printable ASCII needle without those appears in a line
    as written.
    """
    plain = needle.isascii() and needle.isprintable() and '"' not in needle and "\\" not in needle
    return needle.encode("ascii") if needle and plain else None


class SearchJob:
    """A search over a log: its hits fill in on a worker thread."""

    def __init__(self, query: str) -> None:
        self.query = query
        #: Item indices that match, in order; complete once ``done``.
        self.hits: list[int] = []
        self.done = False
        self.cancel = threading.Event()


class LogIndex:
    """Where the items of a log are, built in one pass and extended as it grows."""

    def __init__(self, path: Path, fmt: LogFormat) -> None:
        self.path = path
        self.fmt = fmt
        self.starts: array[int] = array("Q")
        self.ends: array[int] = array("Q")
        self.kind_ids = bytearray()
        self.flags = bytearray()
        self.kind_names: list[str] = []
        self._kind_index: dict[str, int] = {}
        #: Item indices of user messages and of items that call for attention.
        self.turn_items: array[int] = array("I")
        self.attention_items: array[int] = array("I")
        #: Items fully published; readers use this, never ``len(starts)``.
        self.count = 0
        self._scanned = 0
        self._lock = threading.Lock()

    # ── reading the log ──────────────────────────────────────────────────────

    @property
    def scanned(self) -> int:
        """The offset up to which the log has been indexed."""
        return self._scanned

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def scan(
        self,
        cancel: threading.Event | None = None,
        *,
        limit: int | None = None,
        pause_s: float = 0.0,
    ) -> int:
        """Index the bytes appended since the last scan; return the items added.

        With *limit* at most that many bytes are read (the rest waits for the
        next call). A worker thread passes *pause_s*, a short sleep after each
        chunk that hands the interpreter to the event loop. A line without its newline is left for later, unless it is
        a complete record, which a hand-written log may end with.
        """
        with self._lock:
            size = self.size()
            if size < self._scanned:
                self._reset()
            before = len(self.starts)
            budget = size if limit is None else min(size, self._scanned + limit)
            try:
                with self.path.open("rb") as handle:
                    self._read(handle, size, budget, cancel, pause_s)
            except OSError:
                pass
            self.count = len(self.starts)
            return self.count - before

    def _reset(self) -> None:
        self.starts = array("Q")
        self.ends = array("Q")
        self.kind_ids = bytearray()
        self.flags = bytearray()
        self.turn_items = array("I")
        self.attention_items = array("I")
        self.count = 0
        self._scanned = 0

    def _read(
        self,
        handle: BinaryIO,
        size: int,
        budget: int,
        cancel: threading.Event | None,
        pause_s: float,
    ) -> None:
        offset = self._scanned
        handle.seek(offset)
        carry = b""
        carry_start = offset
        while offset < budget:
            if cancel is not None and cancel.is_set():
                return
            chunk = handle.read(min(CHUNK_BYTES, budget - offset))
            if not chunk:
                break
            offset += len(chunk)
            data = carry + chunk
            newline = data.rfind(b"\n")
            if newline < 0:
                carry = data
                continue
            self._index_block(data[: newline + 1], carry_start)
            carry = data[newline + 1 :]
            carry_start += newline + 1
            self._scanned = carry_start
            self.count = len(self.starts)
            if pause_s:
                time.sleep(pause_s)
        if carry.strip() and offset >= size and self.fmt.complete(carry.rstrip(b"\r")):
            self._add(carry_start, carry_start + len(carry), carry.rstrip(b"\r"))
            self._scanned = carry_start + len(carry)

    def _index_block(self, block: bytes, base: int) -> None:
        position = base
        for line in block.split(b"\n")[:-1]:
            end = position + len(line) + 1
            if line.strip():
                self._add(position, end, line.rstrip(b"\r"))
            position = end

    def _add(self, start: int, end: int, line: bytes) -> None:
        found = self.fmt.classify(line)
        if found is None:
            return
        kind, attention = found
        kind_id = self._kind_index.get(kind)
        if kind_id is None:
            kind_id = len(self.kind_names)
            self._kind_index[kind] = kind_id
            self.kind_names.append(kind)
        item = len(self.starts)
        self.starts.append(start)
        self.ends.append(end)
        self.kind_ids.append(kind_id & 0xFF)
        self.flags.append(1 if attention else 0)
        if kind == "user_message":
            self.turn_items.append(item)
        if attention:
            self.attention_items.append(item)

    # ── lookups ──────────────────────────────────────────────────────────────

    def kind(self, item: int) -> str:
        return self.kind_names[self.kind_ids[item]]

    def turn_number(self, item: int) -> int:
        """How many user messages there are up to and including *item*."""
        return bisect.bisect_right(self.turn_items, item, 0, len(self.turn_items))

    def item_at(self, offset: int) -> int:
        """The item whose line holds the byte at *offset*."""
        return max(0, bisect.bisect_right(self.starts, offset, 0, self.count) - 1)

    # ── search ───────────────────────────────────────────────────────────────

    def search(
        self, query: str, cancel: threading.Event | None = None, *, pause_s: float = 0.0
    ) -> list[int]:
        """The items whose text contains *query*, in order (smart case, substring).

        Chunks of about 1 MiB are lower-cased and scanned for the raw bytes of
        the query; only the lines that hold them are decoded and checked, so a
        log with few hits costs a few C-level passes.
        """
        count = self.count
        folded = query == query.lower()
        needle = query.lower() if folded else query
        raw = _prefilter(needle)
        hits: list[int] = []
        item = 0
        try:
            handle = self.path.open("rb")
        except OSError:
            return hits
        with handle:
            while item < count:
                if cancel is not None and cancel.is_set():
                    break
                last = item + 1
                first_offset = self.starts[item]
                while last < count and self.ends[last] - first_offset <= CHUNK_BYTES:
                    last += 1
                span = self.ends[last - 1] - first_offset
                if span > _MAX_ITEM_BYTES:
                    item = last
                    continue
                handle.seek(first_offset)
                data = handle.read(span)
                if raw is None:
                    candidates: list[int] = list(range(item, last))
                else:
                    probe = data.lower() if folded else data
                    candidates = []
                    at = probe.find(raw)
                    while at >= 0:
                        position = first_offset + at
                        owner = bisect.bisect_right(self.starts, position, item, last) - 1
                        if position < self.ends[owner]:  # not in a line that shows nothing
                            candidates.append(owner)
                        if owner + 1 >= last:
                            break
                        at = probe.find(raw, self.starts[owner + 1] - first_offset)
                for owner in candidates:
                    line = data[self.starts[owner] - first_offset : self.ends[owner] - first_offset]
                    text = self.fmt.text_of(line.rstrip(b"\r\n"))
                    if text is not None and matches(query, text):
                        hits.append(owner)
                item = last
                if pause_s:
                    time.sleep(pause_s)
        return hits
