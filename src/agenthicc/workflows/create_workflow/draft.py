"""Run-owned draft manifests and atomic workflow publication.

Generated workflows are written to a hidden, run-specific draft tree. The
normal workflow loader never discovers that tree. Publication replaces the
whole package only after the manifest has been rechecked, so stale sibling
files from a previous repair cannot survive in the published package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "DRAFT_ROOT",
    "DraftError",
    "DraftFile",
    "DraftManifest",
    "PublicationRecord",
    "build_draft_path",
    "publish_draft",
    "reset_draft",
    "scan_draft",
    "stage_legacy_package",
]

DRAFT_ROOT: Final[str] = ".agenthicc/workflows/.drafts"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_FILES = 128
_MAX_FILE_BYTES = 1_000_000
_MAX_TOTAL_BYTES = 4_000_000


class DraftError(ValueError):
    """Raised when a generated draft is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class DraftFile:
    """One regular file in a generated draft."""

    path: str
    byte_length: int
    line_count: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "byte_length": self.byte_length,
            "line_count": self.line_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DraftManifest:
    """Exact, deterministic inventory of one run-owned workflow package."""

    workflow_name: str
    run_id: str
    draft_path: str
    files: tuple[DraftFile, ...]
    total_bytes: int
    fingerprint: str

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "version": "agenthicc.workflow-draft.v1",
            "workflow_name": self.workflow_name,
            "run_id": self.run_id,
            "draft_path": self.draft_path,
            "files": [item.to_dict() for item in self.files],
            "total_bytes": self.total_bytes,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    def render(self) -> str:
        """Return a bounded report suitable for a validation prompt."""
        lines = [
            "[DRAFT MANIFEST]",
            f"workflow_name: {self.workflow_name}",
            f"run_id: {self.run_id}",
            f"draft_path: {self.draft_path}",
            f"files: {len(self.files)}",
            f"total_bytes: {self.total_bytes}",
            f"fingerprint: {self.fingerprint}",
        ]
        lines.extend(
            f"- {item.path} bytes={item.byte_length} lines={item.line_count} sha256={item.sha256}"
            for item in self.files
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """Evidence emitted after a draft package was atomically published."""

    workflow_name: str
    run_id: str
    draft_path: str
    published_path: str
    draft_fingerprint: str
    published_fingerprint: str
    backup_path: str = ""
    published_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "published",
            "workflow_name": self.workflow_name,
            "run_id": self.run_id,
            "draft_path": self.draft_path,
            "published_path": self.published_path,
            "draft_fingerprint": self.draft_fingerprint,
            "published_fingerprint": self.published_fingerprint,
            "backup_path": self.backup_path,
            "published_at": self.published_at,
        }


def _safe_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise DraftError(f"{label} must be a lower_snake_case identifier")
    return value


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise DraftError("run_id contains unsafe path characters")
    return value


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _reject_symlink_components(path: Path, root: Path) -> None:
    """Reject symlinked parents between an authorized root and *path*."""
    resolved_root = root.resolve()
    current = path
    while True:
        _reject_symlink(current)
        if current == resolved_root:
            return
        if current.parent == current:
            raise DraftError(f"path {path} is not contained by workspace {root}")
        current = current.parent


def _reject_symlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise DraftError(f"symlink is not allowed in workflow draft: {path}")
    except OSError as exc:
        raise DraftError(f"could not inspect draft path {path}: {exc}") from exc


def build_draft_path(root: Path, run_id: str, workflow_name: str) -> Path:
    """Return the isolated draft directory for a run and workflow name."""
    _safe_run_id(run_id)
    _safe_name(workflow_name, "workflow_name")
    workspace = root.resolve()
    draft = workspace / DRAFT_ROOT / run_id / workflow_name
    if not _contained(draft, workspace):
        raise DraftError("draft path escapes the authorized workspace")
    _reject_symlink_components(draft, workspace)
    return draft


def _file_entry(path: Path, relative: str) -> DraftFile:
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise DraftError(f"draft file {relative} could not be inspected: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise DraftError(f"draft entry is not a regular file: {path}")
    if before.st_size > _MAX_FILE_BYTES:
        raise DraftError(f"draft file {relative} exceeds {_MAX_FILE_BYTES} bytes")

    data_chunks: list[bytes] = []
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DraftError(f"draft file {relative} could not be read: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise DraftError(f"draft file {relative} changed while it was opened")
        while True:
            chunk = os.read(fd, min(65_536, _MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            data_chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise DraftError(f"draft file {relative} exceeds {_MAX_FILE_BYTES} bytes")
        after = os.fstat(fd)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
        ):
            raise DraftError(f"draft file {relative} changed while it was read")
    except OSError as exc:
        raise DraftError(f"draft file {relative} could not be read: {exc}") from exc
    finally:
        os.close(fd)
    data = b"".join(data_chunks)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DraftError(f"draft file {relative} is not valid UTF-8") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_data = normalized.encode("utf-8")
    return DraftFile(
        path=relative,
        byte_length=len(normalized_data),
        line_count=normalized.count("\n") + (1 if normalized else 0),
        sha256=hashlib.sha256(normalized_data).hexdigest(),
    )


def _manifest_fingerprint(
    workflow_name: str,
    run_id: str,
    files: tuple[DraftFile, ...],
) -> str:
    payload = {
        "version": "agenthicc.workflow-draft.v1",
        "workflow_name": workflow_name,
        "run_id": run_id,
        "files": [item.to_dict() for item in files],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _tree_files(path: Path) -> tuple[DraftFile, ...]:
    """Return a bounded manifest for an arbitrary already-contained tree."""
    files: list[DraftFile] = []
    total = 0
    for current, directories, names in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [directory for directory in directories if directory != "__pycache__"]
        for directory in directories:
            _reject_symlink(current_path / directory)
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")):
                continue
            file_path = current_path / name
            _reject_symlink(file_path)
            if not file_path.is_file():
                raise DraftError(f"draft entry is not a regular file: {file_path}")
            relative = file_path.relative_to(path).as_posix()
            if relative.startswith("../") or relative == "..":
                raise DraftError(f"draft entry escapes package: {relative}")
            files.append(_file_entry(file_path, relative))
            total += files[-1].byte_length
            if len(files) > _MAX_FILES:
                raise DraftError(f"draft contains more than {_MAX_FILES} files")
            if total > _MAX_TOTAL_BYTES:
                raise DraftError(f"draft exceeds {_MAX_TOTAL_BYTES} total bytes")
    return tuple(sorted(files, key=lambda item: item.path))


def scan_draft(
    path: Path,
    *,
    root: Path,
    run_id: str,
    workflow_name: str,
) -> DraftManifest:
    """Scan and validate the exact contents of a run-owned draft."""
    _safe_run_id(run_id)
    _safe_name(workflow_name, "workflow_name")
    workspace = root.resolve()
    draft = path if path.is_absolute() else workspace / path
    _reject_symlink_components(draft, workspace)
    draft = draft.resolve()
    expected = build_draft_path(workspace, run_id, workflow_name).resolve()
    if draft != expected:
        raise DraftError(f"draft path must be exactly {expected}")
    if not draft.is_dir():
        raise DraftError(f"draft directory does not exist: {draft}")
    ordered = _tree_files(draft)
    if not any(item.path == "runner.py" for item in ordered):
        raise DraftError("draft must contain runner.py")
    fingerprint = _manifest_fingerprint(workflow_name, run_id, ordered)
    return DraftManifest(
        workflow_name=workflow_name,
        run_id=run_id,
        draft_path=str(draft),
        files=ordered,
        total_bytes=sum(item.byte_length for item in ordered),
        fingerprint=fingerprint,
    )


def reset_draft(
    path: Path,
    *,
    root: Path,
    run_id: str,
    workflow_name: str,
) -> Path:
    """Remove only the contents of one run-owned draft before a repair.

    A validation rejection starts a new generation attempt, not a new run.
    Clearing the exact draft prevents helper modules from a failed attempt
    silently surviving into the next manifest. The directory itself is kept so
    a resumed/chunked writer retains the same authorized target. Symlinks and
    non-regular entries are rejected before anything is removed.
    """
    _safe_run_id(run_id)
    _safe_name(workflow_name, "workflow_name")
    workspace = root.resolve()
    draft = path if path.is_absolute() else workspace / path
    draft = Path(os.path.abspath(draft))
    expected = build_draft_path(workspace, run_id, workflow_name)
    _reject_symlink_components(draft, workspace)
    if draft != expected:
        raise DraftError(f"draft path must be exactly {expected}")
    if not draft.exists():
        return draft
    if not draft.is_dir():
        raise DraftError(f"draft path is not a directory: {draft}")

    # Audit the complete tree before deleting a child directory. This avoids
    # delegating an unexpected symlink or special file to recursive removal.
    for current, directories, names in os.walk(draft, topdown=True, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            child = current_path / directory
            _reject_symlink(child)
        for name in names:
            child = current_path / name
            _reject_symlink(child)
            if not child.is_file():
                raise DraftError(f"draft entry is not a regular file: {child}")
    for child in sorted(draft.iterdir(), key=lambda item: item.name, reverse=True):
        _remove_exact(child)
    return draft


def _copy_tree_without_links(source: Path, destination: Path) -> None:
    """Copy a validated draft into a private publication staging directory."""
    copied_files = 0
    copied_bytes = 0
    for current, directories, names in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [directory for directory in directories if directory != "__pycache__"]
        relative = current_path.relative_to(source)
        target = destination / relative
        target.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            _reject_symlink(current_path / directory)
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")):
                continue
            source_file = current_path / name
            _reject_symlink(source_file)
            if not source_file.is_file():
                raise DraftError(f"cannot publish non-regular file {source_file}")
            relative_path = source_file.relative_to(source).as_posix()
            entry = _file_entry(source_file, relative_path)
            copied_files += 1
            copied_bytes += entry.byte_length
            if copied_files > _MAX_FILES:
                raise DraftError(f"draft contains more than {_MAX_FILES} files")
            if copied_bytes > _MAX_TOTAL_BYTES:
                raise DraftError(f"draft exceeds {_MAX_TOTAL_BYTES} total bytes")
            shutil.copyfile(source_file, target / name)


def _remove_exact(path: Path) -> None:
    """Remove one already-resolved publication path after symlink checking."""
    _reject_symlink(path)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def stage_legacy_package(
    source: Path,
    *,
    destination: Path,
    root: Path,
) -> Path:
    """Copy a legacy direct-published file/package into an isolated draft.

    This is a migration adapter for older callers that still report
    .agenthicc/workflows/name.py. New generation prompts always target the
    run-owned draft path.
    """
    workspace = root.resolve()
    # ``resolve()`` follows a symlink and would erase the evidence needed by
    # the symlink checks below.  Normalize ``..`` lexically first; containment
    # and component checks then inspect the original filesystem path.
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    if not _contained(source, workspace):
        raise DraftError("legacy workflow path is outside the workspace")
    if not _contained(destination, workspace) or not destination.name:
        raise DraftError("legacy draft destination is outside the workspace")
    _reject_symlink_components(source, workspace)
    _reject_symlink_components(destination, workspace)
    if not source.exists():
        raise DraftError(f"legacy workflow path does not exist: {source}")
    if not source.is_file() and not source.is_dir():
        raise DraftError("legacy workflow path is not a regular file or directory")
    if source.is_dir() and not (source / "runner.py").is_file():
        raise DraftError("legacy workflow directory must contain runner.py")

    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(destination.parent, workspace)
    temporary_parent = Path(tempfile.mkdtemp(prefix=".draft-stage-", dir=destination.parent))
    temporary = temporary_parent / "package"
    try:
        temporary.mkdir()
        if source.is_file():
            entry = _file_entry(source, "runner.py")
            shutil.copyfile(source, temporary / "runner.py")
            if entry.byte_length > _MAX_TOTAL_BYTES:
                raise DraftError(f"draft exceeds {_MAX_TOTAL_BYTES} total bytes")
        else:
            _copy_tree_without_links(source, temporary)
        if destination.exists():
            _remove_exact(destination)
        os.replace(temporary, destination)
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)
    return destination


def _sync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Directory fsync is not portable; the atomic rename remains the
        # visibility boundary on platforms where it is unavailable.
        return


def publish_draft(
    manifest: DraftManifest,
    *,
    root: Path,
    published_name: str | None = None,
) -> PublicationRecord:
    """Atomically replace one published workflow package with a draft.

    The old package is moved to a hidden backup before the staged draft is
    renamed into place. If the second rename fails, the old package is restored.
    The backup is retained so an operator can recover an earlier publication.
    """
    workspace = root.resolve()
    workflow_name = _safe_name(published_name or manifest.workflow_name, "workflow_name")
    draft_input = Path(manifest.draft_path).expanduser()
    if not draft_input.is_absolute():
        draft_input = workspace / draft_input
    draft_input = Path(os.path.abspath(draft_input))
    _reject_symlink_components(draft_input, workspace)
    draft = draft_input.resolve()
    checked = scan_draft(
        draft,
        root=workspace,
        run_id=manifest.run_id,
        workflow_name=workflow_name,
    )
    if checked.fingerprint != manifest.fingerprint:
        raise DraftError("draft changed after the manifest was recorded")

    parent = workspace / ".agenthicc/workflows"
    _reject_symlink_components(parent, workspace)
    parent.mkdir(parents=True, exist_ok=True)
    # Recheck after creating missing components.  This closes the normal
    # symlink-substitution window between the preflight inspection and the
    # first publication rename.
    _reject_symlink_components(parent, workspace)
    destination = parent / workflow_name
    _reject_symlink_components(destination, workspace)
    legacy_destination = parent / f"{workflow_name}.py"
    _reject_symlink_components(legacy_destination, workspace)
    if destination.exists() and not destination.is_dir():
        raise DraftError(f"published workflow path is not a directory: {destination}")
    temporary_parent = Path(tempfile.mkdtemp(prefix=f".{workflow_name}.publish-", dir=parent))
    temporary = temporary_parent / "package"
    backup = parent / ".backups" / manifest.run_id / workflow_name
    _reject_symlink_components(parent / ".backups", workspace)
    backup_path = ""
    moved_old_package = False
    moved_old_legacy = False
    old_package = destination.exists()
    old_legacy = legacy_destination.exists()
    try:
        _copy_tree_without_links(draft, temporary)
        copied_files = _tree_files(temporary)
        if copied_files != checked.files:
            raise DraftError("publication staging changed the draft manifest")
        _sync_directory(temporary)
        if old_package or old_legacy:
            backup.parent.mkdir(parents=True, exist_ok=True)
            _reject_symlink_components(backup.parent, workspace)
            _reject_symlink_components(backup, workspace)
            if backup.exists() or backup.is_symlink():
                raise DraftError(f"publication backup already exists: {backup}")
            if old_package and old_legacy:
                backup.mkdir()
                os.replace(destination, backup / "package")
                moved_old_package = True
                os.replace(legacy_destination, backup / "legacy.py")
                moved_old_legacy = True
            elif old_package:
                os.replace(destination, backup)
                moved_old_package = True
            else:
                backup.mkdir()
                os.replace(legacy_destination, backup / "legacy.py")
                moved_old_legacy = True
            backup_path = str(backup)
        os.replace(temporary, destination)
        _sync_directory(parent)
    except Exception:
        if destination.exists() and not old_package:
            _remove_exact(destination)
        elif destination.exists() and (moved_old_package or moved_old_legacy):
            _remove_exact(destination)
        if moved_old_package and not destination.exists():
            if old_package and old_legacy:
                os.replace(backup / "package", destination)
            else:
                os.replace(backup, destination)
        if moved_old_legacy and not legacy_destination.exists():
            os.replace(backup / "legacy.py", legacy_destination)
        raise
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)

    return PublicationRecord(
        workflow_name=workflow_name,
        run_id=manifest.run_id,
        draft_path=str(draft),
        published_path=str(destination),
        draft_fingerprint=checked.fingerprint,
        published_fingerprint=checked.fingerprint,
        backup_path=backup_path,
        published_at=time.time(),
    )
