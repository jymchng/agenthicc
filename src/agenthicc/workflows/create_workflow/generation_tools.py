"""Path-scoped filesystem tools for canonical workflow generation."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

from agenthicc.workflows.create_workflow.authoring import (
    AuthoringLease,
    AuthoringError,
    PENDING_MARKER_NAME,
    _optional_os_flag,
    verify_authoring,
)

__all__ = ["make_generation_writer_tools", "make_package_read_tools"]

_MAX_FILE_BYTES = 1_000_000


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise AuthoringError("package path must be a non-empty relative path")
    if "\\" in value or PureWindowsPath(value).drive or PureWindowsPath(value).is_absolute():
        raise AuthoringError("package path must use relative POSIX separators")
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value:
        raise AuthoringError("package path must be a normalized relative path")
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise AuthoringError("package path cannot contain empty, '.' or '..' components")
    if parts == (PENDING_MARKER_NAME,):
        raise AuthoringError("the runner-owned pending marker cannot be modified")
    return parts


def _check_path_components(lease: AuthoringLease, parts: tuple[str, ...], *, create: bool) -> Path:
    verify_authoring(lease)
    root = Path(lease.target_path)
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            if not create:
                raise AuthoringError(
                    f"package directory does not exist: {current.relative_to(root)}"
                ) from None
            try:
                current.mkdir()
            except FileExistsError:
                pass
            current_stat = current.lstat()
        if not stat.S_ISDIR(current_stat.st_mode) or stat.S_ISLNK(current_stat.st_mode):
            raise AuthoringError(f"package path contains a non-directory or symlink: {part}")
    target = current / parts[-1]
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(target_stat.st_mode):
        raise AuthoringError(f"package path is a symlink: {value_path(parts)}")
    return target


def value_path(parts: tuple[str, ...]) -> str:
    return PurePosixPath(*parts).as_posix()


def _check_existing_file(path: Path) -> os.stat_result | None:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(result.st_mode) or stat.S_ISLNK(result.st_mode):
        raise AuthoringError(f"package file is not a regular file: {path.name}")
    if result.st_size > _MAX_FILE_BYTES:
        raise AuthoringError(f"package file exceeds {_MAX_FILE_BYTES} bytes")
    return result


async def _authorize(lease: AuthoringLease, path: Path, *, operation: str, tool_name: str) -> None:
    from agenthicc.tools.capabilities import ToolCapability
    from agenthicc.tools.workspace_access import current_workspace_access

    policy = current_workspace_access()
    if policy is None:
        return
    capability = ToolCapability.WRITE.value if operation == "write" else ToolCapability.READ.value
    await policy.authorize(
        path,
        operation=operation,
        tool_name=tool_name,
        capabilities=frozenset({capability}),
        tool_input={"path": str(path)},
    )
    if not path.is_relative_to(Path(lease.workspace_root)):
        raise AuthoringError("workflow package is outside the authorized workspace")


def _read_file(lease: AuthoringLease, parts: tuple[str, ...]) -> str:
    path = _check_path_components(lease, parts, create=False)
    descriptor = os.open(path, os.O_RDONLY | _optional_os_flag("O_NOFOLLOW"))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_FILE_BYTES:
            raise AuthoringError(f"package file exceeds {_MAX_FILE_BYTES} bytes or is not regular")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise AuthoringError(f"package file exceeds {_MAX_FILE_BYTES} bytes")
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthoringError("package file is not valid UTF-8") from exc


def _write_file(lease: AuthoringLease, parts: tuple[str, ...], content: str) -> int:
    data = content.encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise AuthoringError(f"write exceeds {_MAX_FILE_BYTES} bytes")
    path = _check_path_components(lease, parts, create=True)
    parent_before = path.parent.stat(follow_symlinks=False)
    _check_existing_file(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        _check_path_components(lease, parts, create=False)
        parent_after = path.parent.stat(follow_symlinks=False)
        if (parent_before.st_dev, parent_before.st_ino) != (
            parent_after.st_dev,
            parent_after.st_ino,
        ):
            raise AuthoringError("package directory changed while writing")
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | _optional_os_flag("O_DIRECTORY"))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return len(data)


def _append_file(lease: AuthoringLease, parts: tuple[str, ...], content: str) -> int:
    data = content.encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise AuthoringError(f"append exceeds {_MAX_FILE_BYTES} bytes")
    path = _check_path_components(lease, parts, create=False)
    existing = _check_existing_file(path)
    if existing is None:
        raise AuthoringError(f"package file does not exist: {value_path(parts)}")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | _optional_os_flag("O_NOFOLLOW"),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size + len(data) > _MAX_FILE_BYTES:
            raise AuthoringError(f"package file exceeds {_MAX_FILE_BYTES} bytes or is not regular")
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(data)


def _make_directory(lease: AuthoringLease, parts: tuple[str, ...]) -> str:
    verify_authoring(lease)
    current = Path(lease.target_path)
    for part in parts:
        current /= part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        mode = current.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise AuthoringError(f"package path contains a non-directory or symlink: {part}")
        verify_authoring(lease)
    return value_path(parts)


def _make_read_file_tool(lease: AuthoringLease) -> Callable[..., object]:
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_read

    @_tool()
    @tool_read
    async def read_file(path: str) -> dict[str, object]:
        """Read a UTF-8 file from this workflow package using a package-relative path."""
        try:
            parts = _relative_parts(path)
            absolute = Path(lease.target_path).joinpath(*parts)
            await _authorize(lease, absolute, operation="read", tool_name="read_file")
            content = await asyncio.to_thread(_read_file, lease, parts)
            return {"ok": True, "content": content}
        except (AuthoringError, OSError, PermissionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

    return read_file


def make_package_read_tools(lease: AuthoringLease) -> list[Callable[..., object]]:
    """Build read-only tools whose only addressable root is this package."""
    return [_make_read_file_tool(lease)]


def make_generation_writer_tools(lease: AuthoringLease) -> list[Callable[..., object]]:
    """Build read/write tools whose only addressable root is this lease's package."""
    from lauren_ai._tools import tool as _tool
    from agenthicc.tools.capabilities import tool_write

    read_file = _make_read_file_tool(lease)

    @_tool()
    @tool_write
    async def write_file(path: str, content: str) -> dict[str, object]:
        """Atomically write a UTF-8 file inside this workflow package."""
        try:
            parts = _relative_parts(path)
            absolute = Path(lease.target_path).joinpath(*parts)
            await _authorize(lease, absolute, operation="write", tool_name="write_file")
            written = await asyncio.to_thread(_write_file, lease, parts, content)
            return {"ok": True, "bytes_written": written}
        except (AuthoringError, OSError, PermissionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

    @_tool()
    @tool_write
    async def append_file(path: str, content: str) -> dict[str, object]:
        """Append UTF-8 content to a file inside this workflow package."""
        try:
            parts = _relative_parts(path)
            absolute = Path(lease.target_path).joinpath(*parts)
            await _authorize(lease, absolute, operation="write", tool_name="append_file")
            written = await asyncio.to_thread(_append_file, lease, parts, content)
            return {"ok": True, "bytes_written": written}
        except (AuthoringError, OSError, PermissionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

    @_tool()
    @tool_write
    async def make_directory(path: str) -> dict[str, object]:
        """Create a subdirectory inside this workflow package."""
        try:
            parts = _relative_parts(path)
            absolute = Path(lease.target_path).joinpath(*parts)
            await _authorize(lease, absolute, operation="write", tool_name="make_directory")
            created = await asyncio.to_thread(_make_directory, lease, parts)
            return {"ok": True, "path": created}
        except (AuthoringError, OSError, PermissionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:1000]}

    return [make_directory, read_file, write_file, append_file]
