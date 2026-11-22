"""Model-facing file tools confined to a runner-owned authoring root."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Lock
from typing import Literal

from lauren_ai._tools import tool

from agenthicc.runners.process_lease import directory_fsync
from agenthicc.tools.capabilities import ToolCapability, tool_read, tool_write
from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.tools.workspace_access import current_workspace_access

__all__ = ["make_scoped_reader_tools", "make_scoped_writer_tools"]

_RESERVED_NAMES = frozenset(
    {".agenthicc-pending.json", ".agenthicc-manifest.json", "manifest.json"}
)
_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)


class _ScopedFileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise _ScopedFileError("invalid_path", "Use a non-empty relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or PureWindowsPath(value).drive or value.startswith("\\"):
        raise _ScopedFileError("absolute_path", "Use a path relative to the authoring root")
    if ".." in relative.parts:
        raise _ScopedFileError("path_traversal", "Parent path components are forbidden")
    if (
        not relative.parts
        or relative.as_posix() != value
        or any(char in value for char in '\\:<>"|?*')
        or any(part.endswith((".", " ")) for part in relative.parts)
    ):
        raise _ScopedFileError("invalid_path", "Use normalized relative POSIX separators")
    if any(part.casefold() in _RESERVED_NAMES for part in relative.parts):
        raise _ScopedFileError("runner_metadata", "Runner-owned metadata cannot be addressed")
    return relative.parts


def _error(exc: OSError | ValueError) -> dict[str, object]:
    code = exc.code if isinstance(exc, _ScopedFileError) else "file_error"
    return {"ok": False, "code": code, "error": str(exc)[:1000]}


class _ScopedFiles:
    def __init__(
        self,
        root: Path,
        *,
        suffixes: frozenset[str],
        max_file_bytes: int,
        max_files: int,
        verify_owner: Callable[[], object] | None,
    ) -> None:
        self.root = root.absolute()
        self.view = WorkspaceView(self.root)
        self.suffixes = suffixes
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.verify_owner = verify_owner
        self.write_lock = Lock()

    def parts(self, value: str, *, directory: bool = False) -> tuple[str, ...]:
        parts = _relative_parts(value)
        if not directory and PurePosixPath(parts[-1]).suffix not in self.suffixes:
            raise _ScopedFileError(
                "invalid_suffix", "This file suffix is outside the authoring scope"
            )
        return parts

    async def authorize(
        self,
        parts: tuple[str, ...],
        operation: Literal["read", "write"],
        tool_name: str,
    ) -> None:
        policy = current_workspace_access()
        if policy is not None:
            path = self.root.joinpath(*parts)
            capability = ToolCapability.WRITE if operation == "write" else ToolCapability.READ
            await policy.authorize(
                path,
                operation=operation,
                tool_name=tool_name,
                capabilities=frozenset({capability.value}),
                tool_input={"path": str(path)},
            )

    def check_root(self) -> None:
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise _ScopedFileError("symlink", "The authoring root must be a canonical directory")
        global_root = Path.home().resolve() / ".agenthicc"
        if self.root.is_relative_to(global_root) or self.root.is_relative_to(global_root.resolve()):
            raise _ScopedFileError("outside_workspace", "User-global paths cannot be authored")
        if self.verify_owner is not None:
            self.verify_owner()

    def _directory(self, parts: tuple[str, ...], *, create: bool) -> Path:
        self.check_root()
        current = self.root
        for part in parts:
            current /= part
            if create:
                current.mkdir(exist_ok=True)
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise _ScopedFileError("symlink", f"Directory component is a symlink: {part}")
            if not stat.S_ISDIR(mode):
                raise _ScopedFileError("invalid_path", f"Path component is a file: {part}")
        if self.view.resolve(current) != current:
            raise _ScopedFileError("symlink", "Directory changed while checking the authoring path")
        return current

    def _target(
        self, parts: tuple[str, ...], *, create: bool
    ) -> tuple[Path, os.stat_result | None]:
        path = self._directory(parts[:-1], create=create) / parts[-1]
        try:
            info = path.lstat()
        except FileNotFoundError:
            return path, None
        if stat.S_ISLNK(info.st_mode):
            raise _ScopedFileError("symlink", f"File is a symlink: {parts[-1]}")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _ScopedFileError("invalid_path", "File must be a regular, unlinked candidate")
        return path, info

    def _size(self, size: int) -> None:
        if size > self.max_file_bytes:
            raise _ScopedFileError("file_too_large", f"File exceeds {self.max_file_bytes} bytes")

    def _read_bytes(self, path: Path, expected: os.stat_result) -> bytes:
        self._size(expected.st_size)
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(descriptor)
            if not os.path.samestat(opened, expected) or opened.st_nlink != 1:
                raise _ScopedFileError("stale_path", "File changed before it could be read")
            data = stream.read(self.max_file_bytes + 1)
        self._size(len(data))
        return data

    def read(self, parts: tuple[str, ...]) -> str:
        path, info = self._target(parts, create=False)
        if info is None:
            raise _ScopedFileError("missing_file", "The candidate file does not exist")
        return self._read_bytes(path, info).decode("utf-8")

    def _check_count(self) -> None:
        count = 0
        for _, _, names in os.walk(self.root, followlinks=False):
            count += sum(name.casefold() not in _RESERVED_NAMES for name in names)
            if count >= self.max_files:
                raise _ScopedFileError(
                    "file_limit", f"Authoring root allows {self.max_files} files"
                )

    def write(self, parts: tuple[str, ...], content: str, *, append: bool) -> int:
        data = content.encode("utf-8")
        written = len(data)
        self._size(written)
        with self.write_lock:
            path, info = self._target(parts, create=not append)
            parent_before = path.parent.lstat()
            if append:
                if info is None:
                    raise _ScopedFileError("missing_file", "The candidate file does not exist")
                data = self._read_bytes(path, info) + data
                self._size(len(data))
            elif info is None:
                self._check_count()
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                _, latest = self._target(parts, create=False)
                if (
                    not os.path.samestat(parent_before, path.parent.lstat())
                    or (latest is None) != (info is None)
                    or (
                        latest is not None
                        and info is not None
                        and (
                            not os.path.samestat(latest, info)
                            or latest.st_mtime_ns != info.st_mtime_ns
                            or latest.st_size != info.st_size
                        )
                    )
                ):
                    raise _ScopedFileError("stale_path", "Candidate changed while writing")
                os.replace(temporary_path, path)
                directory_fsync(path.parent)
            finally:
                temporary_path.unlink(missing_ok=True)
        return written

    def make_directory(self, parts: tuple[str, ...]) -> str:
        with self.write_lock:
            return self._directory(parts, create=True).relative_to(self.root).as_posix()


def _read_tool(scope: _ScopedFiles) -> Callable[..., object]:
    @tool()
    @tool_read
    async def read_file(path: str) -> dict[str, object]:
        """Read a UTF-8 file by its relative path inside this authoring root."""
        try:
            parts = scope.parts(path)
            await scope.authorize(parts, "read", "read_file")
            content = await asyncio.to_thread(scope.read, parts)
            return {"ok": True, "content": content}
        except (OSError, ValueError) as exc:
            return _error(exc)

    return read_file


def make_scoped_reader_tools(
    root: Path,
    *,
    suffixes: frozenset[str],
    max_file_bytes: int,
    verify_owner: Callable[[], object] | None = None,
) -> list[Callable[..., object]]:
    """Build authoring tools with read access to approved files in one root."""
    return [
        _read_tool(
            _ScopedFiles(
                root,
                suffixes=suffixes,
                max_file_bytes=max_file_bytes,
                max_files=0,
                verify_owner=verify_owner,
            )
        )
    ]


def make_scoped_writer_tools(
    root: Path,
    *,
    suffixes: frozenset[str],
    max_file_bytes: int,
    max_files: int,
    verify_owner: Callable[[], object] | None = None,
) -> list[Callable[..., object]]:
    """Build bounded read and write tools inside the runner's authoring root."""
    scope = _ScopedFiles(
        root,
        suffixes=suffixes,
        max_file_bytes=max_file_bytes,
        max_files=max_files,
        verify_owner=verify_owner,
    )

    @tool()
    @tool_write
    async def write_file(path: str, content: str) -> dict[str, object]:
        """Atomically write UTF-8 content to a file inside this authoring root."""
        try:
            parts = scope.parts(path)
            await scope.authorize(parts, "write", "write_file")
            written = await asyncio.to_thread(scope.write, parts, content, append=False)
            return {"ok": True, "bytes_written": written}
        except (OSError, ValueError) as exc:
            return _error(exc)

    @tool()
    @tool_write
    async def append_file(path: str, content: str) -> dict[str, object]:
        """Append UTF-8 content to an existing file inside this authoring root."""
        try:
            parts = scope.parts(path)
            await scope.authorize(parts, "write", "append_file")
            written = await asyncio.to_thread(scope.write, parts, content, append=True)
            return {"ok": True, "bytes_written": written}
        except (OSError, ValueError) as exc:
            return _error(exc)

    @tool()
    @tool_write
    async def make_directory(path: str) -> dict[str, object]:
        """Create a relative subdirectory inside this authoring root."""
        try:
            parts = scope.parts(path, directory=True)
            await scope.authorize(parts, "write", "make_directory")
            created = await asyncio.to_thread(scope.make_directory, parts)
            return {"ok": True, "path": created}
        except (OSError, ValueError) as exc:
            return _error(exc)

    return [make_directory, _read_tool(scope), write_file, append_file]
