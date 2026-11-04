"""Lossless, exact and atomic file edits (TOOLR-01).

Every edit tool reads the file as bytes, decodes it strictly and without
newline translation, changes only the span it was asked to change, and writes
the result back with the same encoding, byte-order mark and line endings. A
file that does not decode is refused, never rewritten with replacement
characters.

Writes go through :func:`atomic_write`: a sibling temporary file is written,
flushed, fsynced, given the original file's mode and renamed over the target,
so a crash or a full disk never leaves a half-written file.

:class:`FileVersions` gives optimistic concurrency. Reads record each file's
version (``<size>:<mtime_ns>``); an edit compares the file on disk with the
version the caller names, or with the last version this process read, and
refuses a file that changed in between (``stale_file``) unless forced.
"""

from __future__ import annotations

import codecs
import difflib
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "DIFF_PREVIEW_CHARS",
    "DecodedFile",
    "EditError",
    "FILE_VERSIONS",
    "FileVersions",
    "atomic_write",
    "changed_line_ranges",
    "decode_for_edit",
    "diff_preview",
    "file_version",
]

#: An edit result's unified diff is cut to this many characters.
DIFF_PREVIEW_CHARS: Final = 4_000

Newline = Literal["\n", "\r\n", "mixed", "none"]


def _process_umask() -> int:
    # Read once at import, before any thread exists: os.umask is process-wide.
    current = os.umask(0)
    os.umask(current)
    return current


_UMASK: Final = _process_umask()


class EditError(Exception):
    """An edit refused before anything was written; carries its error code."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def result(self) -> dict[str, object]:
        """The tool-result envelope for this refusal."""
        return {"ok": False, "error_code": self.code, "error": str(self), **self.details}


@dataclass(frozen=True, slots=True)
class DecodedFile:
    """A file's text plus everything needed to write it back byte-identically."""

    text: str
    encoding: str
    bom: bytes
    newline: Newline

    @property
    def line_ending(self) -> str:
        """The ending new lines get: CRLF for a CRLF file, LF otherwise."""
        return "\r\n" if self.newline == "\r\n" else "\n"

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.encoding)


def _is_utf8(encoding: str) -> bool:
    return codecs.lookup(encoding).name == "utf-8"


def decode_for_edit(data: bytes, encoding: str = "utf-8") -> DecodedFile:
    """Decode *data* strictly, keeping its BOM and newlines as they are."""
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise EditError("invalid_argument", f"unknown encoding: {encoding}") from exc
    bom = codecs.BOM_UTF8 if _is_utf8(encoding) and data.startswith(codecs.BOM_UTF8) else b""
    try:
        text = data[len(bom) :].decode(encoding)
    except UnicodeDecodeError as exc:
        offset = exc.start + len(bom)
        raise EditError(
            "undecodable",
            f"byte {offset} is not valid {encoding}; refusing to rewrite the file "
            "(pass the file's real encoding)",
            offset=offset,
        ) from exc
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline: Newline
    if crlf == 0 and lf == 0:
        newline = "none"
    elif lf == 0:
        newline = "\r\n"
    elif crlf == 0:
        newline = "\n"
    else:
        newline = "mixed"
    return DecodedFile(text=text, encoding=encoding, bom=bom, newline=newline)


def file_version(st: os.stat_result) -> str:
    """The version of a file as an edit compares it: ``<size>:<mtime_ns>``."""
    return f"{st.st_size}:{st.st_mtime_ns}"


class FileVersions:
    """The last version of each file this process read or wrote (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, str] = {}

    def record(self, path: Path, version: str) -> None:
        with self._lock:
            self._versions[str(path)] = version

    def forget(self, path: Path) -> None:
        with self._lock:
            self._versions.pop(str(path), None)

    def last(self, path: Path) -> str | None:
        with self._lock:
            return self._versions.get(str(path))

    def check(self, path: Path, *, expected_version: str = "", force: bool = False) -> None:
        """Refuse an edit of *path* when it changed since it was last seen.

        *expected_version* is the caller's explicit claim; without one the
        last version this process read or wrote is used. A file never seen,
        or a missing file, is not stale. *force* skips the implicit check
        only: an explicit version that does not match is always refused.
        """
        try:
            current = file_version(os.stat(path))
        except FileNotFoundError:
            if expected_version:
                raise EditError(
                    "stale_file", f"{path.name} no longer exists", current_version=""
                ) from None
            return
        if expected_version:
            if expected_version != current:
                raise EditError(
                    "stale_file",
                    f"{path.name} changed since version {expected_version}; read it again",
                    current_version=current,
                )
            return
        seen = self.last(path)
        if not force and seen is not None and seen != current:
            raise EditError(
                "stale_file",
                f"{path.name} changed on disk since it was last read; read it again "
                "or pass force=true to overwrite",
                current_version=current,
            )


#: The process-wide registry every file tool shares.
FILE_VERSIONS: Final = FileVersions()


def atomic_write(path: Path, data: bytes, *, create_parents: bool = True) -> str:
    """Replace *path* with *data* atomically, keeping its mode; return its version.

    A new file gets the default mode the process umask allows.
    """
    parent = path.parent
    if create_parents:
        parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_UMASK
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    version = file_version(os.stat(path))
    FILE_VERSIONS.record(path, version)
    _refresh_cache(path, data)
    return version


def _refresh_cache(path: Path, data: bytes) -> None:
    """Keep the read cache exact for a file this process just wrote (TOOLR-09)."""
    from agenthicc.tools.fs.file_cache import get_file_cache

    cache = get_file_cache()
    if cache is None:
        return
    try:
        # The cache holds what read_file returns: universal newlines applied.
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        cache.store(str(path), text)
    except UnicodeDecodeError:
        cache.forget(str(path))


def diff_preview(before: str, after: str, name: str) -> tuple[str, bool]:
    """A unified diff of an edit, cut to :data:`DIFF_PREVIEW_CHARS`."""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    )
    if len(diff) <= DIFF_PREVIEW_CHARS:
        return diff, False
    return diff[:DIFF_PREVIEW_CHARS] + "\n…[diff truncated]\n", True


def changed_line_ranges(before: str, after: str) -> list[list[int]]:
    """1-based ``[first, last]`` line ranges of *after* that an edit touched.

    A pure deletion is reported as the empty range at the line it left,
    ``[n, n - 1]``.
    """
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    return [
        [start + 1, end] for tag, _a0, _a1, start, end in matcher.get_opcodes() if tag != "equal"
    ]
