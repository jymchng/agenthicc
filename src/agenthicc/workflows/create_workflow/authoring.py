"""Run-owned canonical workflow-package authoring transactions.

A pending package is written at its final project path but remains invisible to
normal workflow discovery until deterministic validation, explicit approval,
exact-byte trust recording, and fingerprint revalidation have all succeeded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

__all__ = [
    "PENDING_MARKER_NAME",
    "AuthoringError",
    "AuthoringLease",
    "ExistingWorkflowTarget",
    "PackageFile",
    "PackageManifest",
    "begin_authoring",
    "canonical_workflow_path",
    "finalize_authoring",
    "inspect_existing_target",
    "prepare_authoring",
    "read_authoring_lease",
    "restore_authoring",
    "scan_package",
    "scan_committed_package",
    "verify_authoring",
]

_WORKFLOW_ROOT: Final[str] = ".agenthicc/workflows"
_LOCK_ROOT: Final[str] = ".agenthicc/workflows/.authoring-locks"
_BACKUP_ROOT: Final[str] = ".agenthicc/workflows/.authoring-backups"
PENDING_MARKER_NAME: Final[str] = ".agenthicc-pending.json"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LEASE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_MAX_FILES = 128
_MAX_FILE_BYTES = 1_000_000
_MAX_TOTAL_BYTES = 4_000_000
_MAX_METADATA_BYTES = 16_384


def _optional_os_flag(name: str) -> int:
    """Return an available integer open flag or zero on unsupported platforms."""
    value = getattr(os, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class AuthoringError(ValueError):
    """Raised when a canonical authoring transaction is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class PackageFile:
    """One bounded, regular UTF-8 file in a workflow package."""

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

    @classmethod
    def from_payload(cls, value: object) -> PackageFile:
        if not isinstance(value, dict):
            raise AuthoringError("package manifest file entry must be an object")
        path = value.get("path")
        byte_length = value.get("byte_length")
        line_count = value.get("line_count")
        digest = value.get("sha256")
        if not isinstance(path, str) or not path or len(path) > 512 or "\x00" in path:
            raise AuthoringError("package manifest contains an invalid path")
        relative = PurePosixPath(path)
        if (
            relative.is_absolute()
            or PureWindowsPath(path).drive
            or "\\" in path
            or relative.as_posix() != path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise AuthoringError("package manifest contains a non-canonical relative path")
        if type(byte_length) is not int or not 0 <= byte_length <= _MAX_FILE_BYTES:
            raise AuthoringError("package manifest contains an invalid byte length")
        if type(line_count) is not int or not 0 <= line_count <= byte_length + 1:
            raise AuthoringError("package manifest contains an invalid line count")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AuthoringError("package manifest contains an invalid file digest")
        return cls(path, byte_length, line_count, digest)


@dataclass(frozen=True, slots=True)
class PackageManifest:
    """Exact inventory and digest of one run-owned canonical package."""

    workflow_name: str
    run_id: str
    target_path: str
    files: tuple[PackageFile, ...]
    total_bytes: int
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "agenthicc.workflow-package.v1",
            "workflow_name": self.workflow_name,
            "run_id": self.run_id,
            "target_path": self.target_path,
            "files": [item.to_dict() for item in self.files],
            "total_bytes": self.total_bytes,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, value: object) -> PackageManifest:
        if not isinstance(value, dict) or value.get("version") != "agenthicc.workflow-package.v1":
            raise AuthoringError("checkpoint has no valid canonical package manifest")
        name = value.get("workflow_name")
        run_id = value.get("run_id")
        target_path = value.get("target_path")
        raw_files = value.get("files")
        total_bytes = value.get("total_bytes")
        fingerprint = value.get("fingerprint")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise AuthoringError("checkpoint package manifest has an invalid workflow name")
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise AuthoringError("checkpoint package manifest has an invalid run ID")
        if not isinstance(target_path, str) or not Path(target_path).is_absolute():
            raise AuthoringError("checkpoint package manifest has an invalid target path")
        if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= _MAX_FILES:
            raise AuthoringError("checkpoint package manifest has an invalid file inventory")
        files = tuple(PackageFile.from_payload(item) for item in raw_files)
        if len({item.path for item in files}) != len(files):
            raise AuthoringError("checkpoint package manifest contains duplicate file paths")
        if tuple(sorted(files, key=lambda item: item.path)) != files:
            raise AuthoringError("checkpoint package manifest file inventory is not ordered")
        if "runner.py" not in {item.path for item in files}:
            raise AuthoringError("checkpoint package manifest has no runner.py")
        if type(total_bytes) is not int or total_bytes != sum(item.byte_length for item in files):
            raise AuthoringError("checkpoint package manifest has an invalid total size")
        if total_bytes > _MAX_TOTAL_BYTES:
            raise AuthoringError("checkpoint package manifest exceeds the total size limit")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise AuthoringError("checkpoint package manifest has an invalid fingerprint")
        result = cls(name, run_id, target_path, files, total_bytes, fingerprint)
        if _fingerprint(name, run_id, files) != fingerprint:
            raise AuthoringError(
                "checkpoint package manifest fingerprint does not match its inventory"
            )
        return result

    def render(self) -> str:
        """Return a bounded manifest suitable for a validation prompt."""
        lines = [
            "[WORKFLOW PACKAGE MANIFEST]",
            f"workflow_name: {self.workflow_name}",
            f"run_id: {self.run_id}",
            f"target_path: {self.target_path}",
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
class _TrustRecordSnapshot:
    """Exact pre-replacement trust metadata for one workflow source file."""

    path: str
    sha256: str
    kind: str
    trusted_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "kind": self.kind,
            "trusted_at": self.trusted_at,
        }

    @classmethod
    def from_payload(cls, value: object) -> _TrustRecordSnapshot:
        if not isinstance(value, dict):
            raise AuthoringError("checkpoint original trust record must be an object")
        fields = ("path", "sha256", "kind", "trusted_at")
        if any(not isinstance(value.get(key), str) for key in fields):
            raise AuthoringError("checkpoint original trust record is malformed")
        path = str(value["path"])
        digest = str(value["sha256"])
        kind = str(value["kind"])
        trusted_at = str(value["trusted_at"])
        if (
            not Path(path).is_absolute()
            or str(Path(path).resolve()) != path
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not kind
            or len(kind) > 64
            or not trusted_at
            or len(trusted_at) > 128
        ):
            raise AuthoringError("checkpoint original trust record has invalid fields")
        return cls(path=path, sha256=digest, kind=kind, trusted_at=trusted_at)


@dataclass(frozen=True, slots=True)
class AuthoringLease:
    """Durable identity for the writer, pending marker, lock, and backup."""

    workspace_root: str
    workflow_name: str
    run_id: str
    lease_id: str
    target_path: str
    marker_path: str
    lock_path: str
    backup_path: str
    original_fingerprint: str
    replacing: bool
    original_target_exists: bool
    original_legacy_exists: bool
    original_trust_records: tuple[_TrustRecordSnapshot, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "agenthicc.workflow-authoring-lease.v1",
            "workspace_root": self.workspace_root,
            "workflow_name": self.workflow_name,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "target_path": self.target_path,
            "marker_path": self.marker_path,
            "lock_path": self.lock_path,
            "backup_path": self.backup_path,
            "original_fingerprint": self.original_fingerprint,
            "replacing": self.replacing,
            "original_target_exists": self.original_target_exists,
            "original_legacy_exists": self.original_legacy_exists,
            "original_trust_records": [record.to_dict() for record in self.original_trust_records],
        }

    @classmethod
    def from_payload(cls, value: object) -> AuthoringLease:
        """Decode a strict, JSON-safe lease without coercing malformed fields."""
        if (
            not isinstance(value, dict)
            or value.get("version") != "agenthicc.workflow-authoring-lease.v1"
        ):
            raise AuthoringError("checkpoint has no valid canonical authoring lease")
        string_fields = (
            "workspace_root",
            "workflow_name",
            "run_id",
            "lease_id",
            "target_path",
            "marker_path",
            "lock_path",
            "backup_path",
            "original_fingerprint",
        )
        if any(not isinstance(value.get(key), str) for key in string_fields):
            raise AuthoringError("checkpoint authoring lease has malformed identity fields")
        if type(value.get("replacing")) is not bool:
            raise AuthoringError("checkpoint authoring lease has malformed replacement flag")
        if (
            type(value.get("original_target_exists")) is not bool
            or type(value.get("original_legacy_exists")) is not bool
        ):
            raise AuthoringError("checkpoint authoring lease has malformed original-path flags")
        raw_trust_records = value.get("original_trust_records")
        if not isinstance(raw_trust_records, list) or len(raw_trust_records) > _MAX_FILES + 1:
            raise AuthoringError("checkpoint original trust records are malformed")
        trust_records = tuple(_TrustRecordSnapshot.from_payload(item) for item in raw_trust_records)
        if len({item.path for item in trust_records}) != len(trust_records):
            raise AuthoringError("checkpoint original trust records contain duplicate paths")
        lease = cls(
            workspace_root=str(value["workspace_root"]),
            workflow_name=str(value["workflow_name"]),
            run_id=str(value["run_id"]),
            lease_id=str(value["lease_id"]),
            target_path=str(value["target_path"]),
            marker_path=str(value["marker_path"]),
            lock_path=str(value["lock_path"]),
            backup_path=str(value["backup_path"]),
            original_fingerprint=str(value["original_fingerprint"]),
            replacing=value["replacing"],
            original_target_exists=value["original_target_exists"],
            original_legacy_exists=value["original_legacy_exists"],
            original_trust_records=trust_records,
        )
        _validate_lease_fields(lease)
        return lease


@dataclass(frozen=True, slots=True)
class ExistingWorkflowTarget:
    """Preflight snapshot used to bind collision approval to exact old bytes."""

    workflow_name: str
    target_path: str
    exists: bool
    target_exists: bool
    legacy_exists: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class AuthoringLeaseStatus:
    """An existing transaction lock and its durable lifecycle status."""

    lease: AuthoringLease
    status: str


def _validate_lease_fields(lease: AuthoringLease) -> None:
    if not _NAME_RE.fullmatch(lease.workflow_name):
        raise AuthoringError("authoring lease has an invalid workflow name")
    if not _RUN_ID_RE.fullmatch(lease.run_id):
        raise AuthoringError("authoring lease has an invalid run ID")
    if not _LEASE_ID_RE.fullmatch(lease.lease_id):
        raise AuthoringError("authoring lease has an invalid lease ID")
    if lease.replacing != (lease.original_target_exists or lease.original_legacy_exists):
        raise AuthoringError("authoring lease original-path flags conflict with replacement state")
    root = Path(lease.workspace_root)
    if not root.is_absolute() or str(root.resolve()) != lease.workspace_root:
        raise AuthoringError("authoring lease workspace root is not canonical")
    expected_target = root / _WORKFLOW_ROOT / lease.workflow_name
    expected_marker = expected_target / PENDING_MARKER_NAME
    expected_lock = root / _LOCK_ROOT / f"{lease.workflow_name}.json"
    expected_backup = root / _BACKUP_ROOT / lease.workflow_name / f"{lease.run_id}-{lease.lease_id}"
    if (
        Path(lease.target_path) != expected_target
        or Path(lease.marker_path) != expected_marker
        or Path(lease.lock_path) != expected_lock
        or Path(lease.backup_path) != expected_backup
    ):
        raise AuthoringError("authoring lease paths do not match the canonical package identity")
    legacy = expected_target.parent / f"{lease.workflow_name}.py"
    for record in lease.original_trust_records:
        path = Path(record.path)
        if path.suffix != ".py" or not (path.is_relative_to(expected_target) or path == legacy):
            raise AuthoringError("checkpoint original trust record is outside the workflow package")
    if lease.original_fingerprint and not re.fullmatch(r"[a-f0-9]{64}", lease.original_fingerprint):
        raise AuthoringError("authoring lease has a malformed original-package fingerprint")


def _safe_name(value: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise AuthoringError("workflow_name must be a lower_snake_case identifier")
    return value


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise AuthoringError("run_id contains unsafe path characters")
    return value


def _reject_symlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise AuthoringError(f"symlink is not allowed in workflow package: {path}")
    except OSError as exc:
        raise AuthoringError(f"could not inspect workflow path {path}: {exc}") from exc


def _reject_symlink_components(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    current = Path(os.path.abspath(path))
    while True:
        _reject_symlink(current)
        if current == resolved_root:
            return
        if current.parent == current:
            raise AuthoringError(f"path {path} is not contained by workspace {root}")
        current = current.parent


def canonical_workflow_path(root: Path, workflow_name: str) -> Path:
    """Return the exact canonical package target under *root*."""
    name = _safe_name(workflow_name)
    workspace = root.resolve()
    target = workspace / _WORKFLOW_ROOT / name
    if not target.is_relative_to(workspace):
        raise AuthoringError("canonical workflow target escapes the authorized workspace")
    _reject_symlink_components(target, workspace)
    return target


def _lock_path(root: Path, workflow_name: str) -> Path:
    return root.resolve() / _LOCK_ROOT / f"{_safe_name(workflow_name)}.json"


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    _reject_symlink(path)
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_METADATA_BYTES:
            raise AuthoringError(f"{label} is not a bounded regular file")
        fd = os.open(path, os.O_RDONLY | _optional_os_flag("O_NOFOLLOW"))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AuthoringError(f"could not read {label}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise AuthoringError(f"{label} changed while it was opened")
        raw = os.read(fd, _MAX_METADATA_BYTES + 1)
        if len(raw) > _MAX_METADATA_BYTES:
            raise AuthoringError(f"{label} exceeds the metadata limit")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuthoringError(f"{label} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise AuthoringError(f"{label} must contain a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, object], *, create: bool = False) -> None:
    _reject_symlink_components(path.parent, Path(path.anchor))
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path.parent, Path(path.anchor))
    _reject_symlink(path)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(data) > _MAX_METADATA_BYTES:
        raise AuthoringError("authoring metadata exceeds the size limit")
    if create:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _file_entry(path: Path, relative: str) -> PackageFile:
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AuthoringError(f"workflow file {relative} could not be inspected: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AuthoringError(f"workflow entry is not a regular file: {relative}")
    if before.st_size > _MAX_FILE_BYTES:
        raise AuthoringError(f"workflow file {relative} exceeds {_MAX_FILE_BYTES} bytes")
    try:
        fd = os.open(path, os.O_RDONLY | _optional_os_flag("O_NOFOLLOW"))
    except OSError as exc:
        raise AuthoringError(f"workflow file {relative} could not be read: {exc}") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise AuthoringError(f"workflow file {relative} changed while it was opened")
        while True:
            chunk = os.read(fd, min(65_536, _MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise AuthoringError(f"workflow file {relative} exceeds {_MAX_FILE_BYTES} bytes")
        after = os.fstat(fd)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
        ):
            raise AuthoringError(f"workflow file {relative} changed while it was read")
    except OSError as exc:
        raise AuthoringError(f"workflow file {relative} could not be read: {exc}") from exc
    finally:
        os.close(fd)
    data = b"".join(chunks)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthoringError(f"workflow file {relative} is not valid UTF-8") from exc
    return PackageFile(
        path=relative,
        byte_length=len(data),
        line_count=text.count("\n") + (1 if text else 0),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _tree_files(path: Path, *, allow_marker: bool = False) -> tuple[PackageFile, ...]:
    files: list[PackageFile] = []
    total = 0
    for current, directories, names in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if name != "__pycache__"]
        for name in directories:
            _reject_symlink(current_path / name)
        for name in sorted(names):
            file_path = current_path / name
            if allow_marker and file_path == path / PENDING_MARKER_NAME:
                continue
            if name.endswith((".pyc", ".pyo")):
                continue
            _reject_symlink(file_path)
            entry = _file_entry(file_path, file_path.relative_to(path).as_posix())
            files.append(entry)
            total += entry.byte_length
            if len(files) > _MAX_FILES:
                raise AuthoringError(f"workflow package contains more than {_MAX_FILES} files")
            if total > _MAX_TOTAL_BYTES:
                raise AuthoringError(f"workflow package exceeds {_MAX_TOTAL_BYTES} total bytes")
    return tuple(sorted(files, key=lambda item: item.path))


def _fingerprint(workflow_name: str, run_id: str, files: tuple[PackageFile, ...]) -> str:
    payload = {
        "version": "agenthicc.workflow-package.v1",
        "workflow_name": workflow_name,
        "run_id": run_id,
        "files": [item.to_dict() for item in files],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def inspect_existing_target(
    root: Path,
    *,
    run_id: str,
    workflow_name: str,
) -> ExistingWorkflowTarget:
    """Snapshot existing canonical and legacy entries before replacement approval."""
    name = _safe_name(workflow_name)
    run = _safe_run_id(run_id)
    workspace = root.resolve()
    target = canonical_workflow_path(workspace, name)
    legacy = target.parent / f"{name}.py"
    _reject_symlink_components(legacy, workspace)
    marker = target / PENDING_MARKER_NAME
    if marker.exists() or marker.is_symlink():
        raise AuthoringError(
            "workflow target has a pending marker; resume or recover that run first"
        )
    target_exists = target.exists() or target.is_symlink()
    legacy_exists = legacy.exists() or legacy.is_symlink()
    if target.is_symlink() or legacy.is_symlink():
        raise AuthoringError("workflow target or legacy entry is a symlink")
    if target_exists and not target.is_dir():
        raise AuthoringError(f"workflow target is not a directory: {target}")

    files: list[PackageFile] = []
    if target_exists:
        files.extend(
            PackageFile(
                path=f"package/{item.path}",
                byte_length=item.byte_length,
                line_count=item.line_count,
                sha256=item.sha256,
            )
            for item in _tree_files(target)
        )
    if legacy_exists:
        files.append(_file_entry(legacy, "legacy.py"))
    ordered = tuple(sorted(files, key=lambda item: item.path))
    fingerprint = _fingerprint(name, run, ordered) if ordered else ""
    return ExistingWorkflowTarget(
        workflow_name=name,
        target_path=str(target),
        exists=bool(target_exists or legacy_exists),
        target_exists=target_exists,
        legacy_exists=legacy_exists,
        fingerprint=fingerprint,
    )


def _snapshot_original_trust_records(
    target: Path,
    legacy: Path,
    snapshot: ExistingWorkflowTarget,
) -> tuple[_TrustRecordSnapshot, ...]:
    from agenthicc.plugins.trust import current_extension_trust

    existing_paths: set[str] = set()
    if snapshot.target_exists:
        existing_paths.update(
            str((target / item.path).resolve())
            for item in _tree_files(target)
            if item.path.endswith(".py")
        )
    if snapshot.legacy_exists:
        existing_paths.add(str(legacy.resolve()))
    records = current_extension_trust().records()
    return tuple(
        _TrustRecordSnapshot(
            path=record.path,
            sha256=record.sha256,
            kind=record.kind,
            trusted_at=record.trusted_at,
        )
        for record in records
        if record.path in existing_paths
    )


def _backup_fingerprint(backup: Path, workflow_name: str, run_id: str) -> str:
    files: list[PackageFile] = []
    package = backup / "package"
    legacy = backup / "legacy.py"
    if package.exists() or package.is_symlink():
        if package.is_symlink() or not package.is_dir():
            raise AuthoringError("workflow recovery package is not a regular directory")
        files.extend(
            PackageFile(
                path=f"package/{item.path}",
                byte_length=item.byte_length,
                line_count=item.line_count,
                sha256=item.sha256,
            )
            for item in _tree_files(package)
        )
    if legacy.exists():
        item = _file_entry(legacy, "legacy.py")
        files.append(item)
    ordered = tuple(sorted(files, key=lambda item: item.path))
    return _fingerprint(workflow_name, run_id, ordered) if ordered else ""


def _lease_record(lease: AuthoringLease, status: str) -> dict[str, object]:
    return {**lease.to_dict(), "status": status}


def _read_lease_record(path: Path, *, label: str) -> AuthoringLeaseStatus:
    raw = _read_json(path, label=label)
    lease = AuthoringLease.from_payload(
        {key: value for key, value in raw.items() if key != "status"}
    )
    status = raw.get("status")
    if status not in {"preparing", "active", "committing", "committed", "restored"}:
        raise AuthoringError(f"{label} has an invalid lifecycle status")
    return AuthoringLeaseStatus(lease=lease, status=str(status))


def _same_lease(left: AuthoringLease, right: AuthoringLease) -> bool:
    return left.to_dict() == right.to_dict()


def read_authoring_lease(root: Path, workflow_name: str) -> AuthoringLeaseStatus | None:
    """Read a pending transaction identity without adopting or changing it."""
    workspace = root.resolve()
    lock_path = _lock_path(workspace, workflow_name)
    try:
        return _read_lease_record(lock_path, label="workflow authoring lock")
    except FileNotFoundError:
        return None


def _create_lock(lease: AuthoringLease) -> None:
    _write_json_atomic(Path(lease.lock_path), _lease_record(lease, "preparing"), create=True)


def _write_marker(lease: AuthoringLease, status: str) -> None:
    _write_json_atomic(Path(lease.marker_path), _lease_record(lease, status))


def _lock_status(lease: AuthoringLease) -> str:
    record = _read_lease_record(Path(lease.lock_path), label="workflow authoring lock")
    if not _same_lease(record.lease, lease):
        raise AuthoringError("workflow authoring lock belongs to another run")
    return record.status


def _cleanup_committed_lock(root: Path, workflow_name: str) -> None:
    record = read_authoring_lease(root, workflow_name)
    if record is None:
        return
    marker = Path(record.lease.marker_path)
    if (
        record.status in {"committed", "restored"}
        and not marker.exists()
        and not marker.is_symlink()
    ):
        Path(record.lease.lock_path).unlink(missing_ok=True)
        return
    raise AuthoringError(
        f"workflow {workflow_name!r} is owned by run {record.lease.run_id}; "
        "resume that run or explicitly restore its pending package before starting another"
    )


def prepare_authoring(
    root: Path,
    *,
    run_id: str,
    workflow_name: str,
    expected_original_fingerprint: str,
    replace_existing: bool = False,
) -> AuthoringLease:
    """Create a transaction identity without changing the target filesystem."""
    name = _safe_name(workflow_name)
    run = _safe_run_id(run_id)
    workspace = root.resolve()
    snapshot = inspect_existing_target(workspace, run_id=run, workflow_name=name)
    if snapshot.fingerprint != expected_original_fingerprint:
        raise AuthoringError("workflow target changed after replacement preflight")
    if snapshot.exists and not replace_existing:
        raise AuthoringError(
            f"workflow target {snapshot.target_path} already exists; explicit replacement approval is required"
        )
    if replace_existing and not snapshot.exists:
        raise AuthoringError("replacement was approved but no existing workflow target remains")

    target = Path(snapshot.target_path)
    legacy = target.parent / f"{name}.py"
    original_trust_records = _snapshot_original_trust_records(target, legacy, snapshot)
    lease_id = uuid.uuid4().hex
    return AuthoringLease(
        workspace_root=str(workspace),
        workflow_name=name,
        run_id=run,
        lease_id=lease_id,
        target_path=str(target),
        marker_path=str(target / PENDING_MARKER_NAME),
        lock_path=str(_lock_path(workspace, name)),
        backup_path=str(workspace / _BACKUP_ROOT / name / f"{run}-{lease_id}"),
        original_fingerprint=snapshot.fingerprint,
        replacing=snapshot.exists,
        original_target_exists=snapshot.target_exists,
        original_legacy_exists=snapshot.legacy_exists,
        original_trust_records=original_trust_records,
    )


def begin_authoring(lease: AuthoringLease) -> AuthoringLease:
    """Claim and prepare the canonical target, preserving any approved original."""
    _validate_lease_fields(lease)
    workspace = Path(lease.workspace_root)
    name = lease.workflow_name
    run = lease.run_id
    target = Path(lease.target_path)
    parent = target.parent
    _reject_symlink_components(parent, workspace)
    parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(parent, workspace)
    lock = Path(lease.lock_path)
    _reject_symlink_components(lock.parent, workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(lock.parent, workspace)
    _cleanup_committed_lock(workspace, name)

    try:
        _create_lock(lease)
    except FileExistsError as exc:
        try:
            existing = _read_lease_record(lock, label="workflow authoring lock")
        except (FileNotFoundError, AuthoringError):
            raise AuthoringError(
                f"workflow {name!r} is locked by another authoring run; resume or restore it first"
            ) from exc
        if not _same_lease(existing.lease, lease):
            raise AuthoringError(
                f"workflow {name!r} is locked by another authoring run; resume or restore it first"
            ) from exc
        if existing.status == "active":
            verify_authoring(lease)
            return lease
        raise AuthoringError(
            "workflow authoring setup was interrupted before the pending package became active; "
            "restore the original from the run-bound backup before retrying"
        ) from exc

    moved_package = False
    moved_legacy = False
    created_target = False
    backup = Path(lease.backup_path)
    legacy = parent / f"{name}.py"
    try:
        snapshot = inspect_existing_target(workspace, run_id=run, workflow_name=name)
        if (
            snapshot.fingerprint != lease.original_fingerprint
            or snapshot.target_exists != lease.original_target_exists
            or snapshot.legacy_exists != lease.original_legacy_exists
        ):
            raise AuthoringError(
                "workflow target changed after authoring identity was checkpointed"
            )
        if lease.replacing:
            _reject_symlink_components(backup.parent, workspace)
            backup.parent.mkdir(parents=True, exist_ok=True)
            _reject_symlink_components(backup.parent, workspace)
            backup.mkdir(exist_ok=False)
            if target.exists():
                os.replace(target, backup / "package")
                moved_package = True
            if legacy.exists():
                os.replace(legacy, backup / "legacy.py")
                moved_legacy = True
            if _backup_fingerprint(backup, name, run) != lease.original_fingerprint:
                raise AuthoringError("existing workflow changed while its recovery backup was made")
        target.mkdir()
        created_target = True
        _write_json_atomic(Path(lease.marker_path), _lease_record(lease, "active"), create=True)
        _write_json_atomic(lock, _lease_record(lease, "active"))
        if lease.replacing:
            _clear_replacement_trust(lease)
        return lease
    except BaseException:
        try:
            if created_target and target.exists() and target.is_dir() and not target.is_symlink():
                _remove_tree(target)
            if moved_package and (backup / "package").exists():
                os.replace(backup / "package", target)
            if moved_legacy and (backup / "legacy.py").exists():
                os.replace(backup / "legacy.py", legacy)
            if lease.replacing:
                _restore_trust(lease)
            if backup.exists():
                _remove_tree(backup)
        except BaseException as rollback_error:
            raise AuthoringError(
                "workflow replacement setup failed and the original could not be restored; "
                f"transaction {lease.run_id}/{lease.lease_id} remains locked for recovery"
            ) from rollback_error
        lock.unlink(missing_ok=True)
        raise


def verify_authoring(lease: AuthoringLease, *, require_marker: bool = True) -> str:
    """Verify the exact lock and pending marker; return the lock status."""
    _validate_lease_fields(lease)
    root = Path(lease.workspace_root)
    target = Path(lease.target_path)
    _reject_symlink_components(target, root)
    if not target.is_dir():
        raise AuthoringError("canonical workflow package is missing or not a directory")
    status = _lock_status(lease)
    marker = Path(lease.marker_path)
    if marker.exists() or marker.is_symlink():
        marker_record = _read_lease_record(marker, label="workflow pending marker")
        if not _same_lease(marker_record.lease, lease):
            raise AuthoringError("workflow pending marker belongs to another run")
        if marker_record.status not in {"active", "committing", "committed"}:
            raise AuthoringError("workflow pending marker has an invalid lifecycle status")
    elif require_marker:
        raise AuthoringError("workflow pending marker is missing; refusing to continue")
    if require_marker and status not in {"active", "committing", "committed"}:
        raise AuthoringError(f"workflow authoring lock is not active (status={status})")
    return status


def scan_package(lease: AuthoringLease) -> PackageManifest:
    """Build a bounded manifest from the exact run-owned canonical package."""
    verify_authoring(lease)
    target = Path(lease.target_path)
    files = _tree_files(target, allow_marker=True)
    if not any(item.path == "runner.py" for item in files):
        raise AuthoringError("workflow package must contain runner.py")
    return PackageManifest(
        workflow_name=lease.workflow_name,
        run_id=lease.run_id,
        target_path=lease.target_path,
        files=files,
        total_bytes=sum(item.byte_length for item in files),
        fingerprint=_fingerprint(lease.workflow_name, lease.run_id, files),
    )


def scan_committed_package(
    lease: AuthoringLease,
    *,
    expected_fingerprint: str,
) -> PackageManifest:
    """Verify a committed checkpoint after marker removal and before summary."""
    if _lock_status(lease) != "committed":
        raise AuthoringError("workflow authoring transaction is not committed")
    marker = Path(lease.marker_path)
    if marker.exists() or marker.is_symlink():
        raise AuthoringError("committed workflow unexpectedly still has a pending marker")
    root = Path(lease.workspace_root)
    target = Path(lease.target_path)
    _reject_symlink_components(target, root)
    if not target.is_dir():
        raise AuthoringError("committed workflow package is missing")
    files = _tree_files(target)
    if not any(item.path == "runner.py" for item in files):
        raise AuthoringError("committed workflow package has no runner.py")
    manifest = PackageManifest(
        workflow_name=lease.workflow_name,
        run_id=lease.run_id,
        target_path=lease.target_path,
        files=files,
        total_bytes=sum(item.byte_length for item in files),
        fingerprint=_fingerprint(lease.workflow_name, lease.run_id, files),
    )
    if manifest.fingerprint != expected_fingerprint:
        raise AuthoringError("committed workflow changed after validation approval")
    return manifest


def _audit_removal_tree(path: Path, *, skip_marker: bool = False) -> None:
    for current, directories, names in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            _reject_symlink(current_path / name)
        for name in names:
            child = current_path / name
            if skip_marker and child == path / PENDING_MARKER_NAME:
                _reject_symlink(child)
                continue
            _reject_symlink(child)
            mode = os.stat(child, follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise AuthoringError(f"workflow entry is not a regular file: {child}")


def _remove_tree(path: Path, *, skip_marker: bool = False) -> None:
    _audit_removal_tree(path, skip_marker=skip_marker)
    for child in sorted(path.iterdir(), key=lambda item: item.name, reverse=True):
        if skip_marker and child.name == PENDING_MARKER_NAME:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    if not skip_marker:
        path.rmdir()


def _verify_backup(lease: AuthoringLease) -> Path:
    backup = Path(lease.backup_path)
    if not lease.replacing:
        return backup
    _reject_symlink_components(backup, Path(lease.workspace_root))
    if not backup.is_dir() or backup.is_symlink():
        raise AuthoringError("approved replacement backup is missing or unsafe")
    if any(item.name not in {"package", "legacy.py"} for item in backup.iterdir()):
        raise AuthoringError("approved replacement backup contains unexpected entries")
    if _backup_fingerprint(backup, lease.workflow_name, lease.run_id) != lease.original_fingerprint:
        raise AuthoringError("approved replacement backup changed; refusing to restore it")
    return backup


def _verify_preparing_backup(lease: AuthoringLease) -> Path:
    """Validate a possibly interrupted backup using any not-yet-moved originals."""
    root = Path(lease.workspace_root)
    backup = Path(lease.backup_path)
    target = Path(lease.target_path)
    legacy = target.parent / f"{lease.workflow_name}.py"
    _reject_symlink_components(backup.parent, root)
    if backup.is_symlink():
        raise AuthoringError("interrupted replacement backup is a symlink")
    if backup.exists() and not backup.is_dir():
        raise AuthoringError("interrupted replacement backup is not a directory")
    if backup.exists() and any(
        item.name not in {"package", "legacy.py"} for item in backup.iterdir()
    ):
        raise AuthoringError("interrupted replacement backup contains unexpected entries")

    files: list[PackageFile] = []
    backed_up_package = backup / "package"
    backed_up_legacy = backup / "legacy.py"
    if backed_up_package.is_symlink() or backed_up_legacy.is_symlink():
        raise AuthoringError("interrupted replacement backup contains a symlink")
    if backed_up_package.exists() and not backed_up_package.is_dir():
        raise AuthoringError("interrupted replacement package is not a directory")
    if backed_up_legacy.exists() and not backed_up_legacy.is_file():
        raise AuthoringError("interrupted replacement legacy entry is not a regular file")
    if backed_up_package.exists() and not lease.original_target_exists:
        raise AuthoringError("interrupted backup contains an unapproved workflow package")
    if lease.original_target_exists:
        package = backed_up_package if backed_up_package.exists() else target
        if not package.exists() or package.is_symlink() or not package.is_dir():
            raise AuthoringError("interrupted replacement is missing its original package")
        files.extend(
            PackageFile(
                path=f"package/{item.path}",
                byte_length=item.byte_length,
                line_count=item.line_count,
                sha256=item.sha256,
            )
            for item in _tree_files(package)
        )
    if backed_up_legacy.exists() and not lease.original_legacy_exists:
        raise AuthoringError("interrupted backup contains an unapproved legacy entry")
    if lease.original_legacy_exists:
        legacy_source = backed_up_legacy if backed_up_legacy.exists() else legacy
        if not legacy_source.exists() or legacy_source.is_symlink():
            raise AuthoringError("interrupted replacement is missing its original legacy entry")
        files.append(_file_entry(legacy_source, "legacy.py"))
    ordered = tuple(sorted(files, key=lambda item: item.path))
    fingerprint = _fingerprint(lease.workflow_name, lease.run_id, ordered) if ordered else ""
    if fingerprint != lease.original_fingerprint:
        raise AuthoringError(
            "interrupted replacement backup does not match the approved original bytes"
        )
    return backup


def _trust_paths(lease: AuthoringLease) -> tuple[Path, ...]:
    from agenthicc.plugins.trust import current_extension_trust

    target = Path(lease.target_path)
    legacy = target.parent / f"{lease.workflow_name}.py"
    paths = {
        Path(record.path)
        for record in current_extension_trust().records()
        if Path(record.path).is_relative_to(target) or Path(record.path) == legacy
    }
    paths.update(Path(record.path) for record in lease.original_trust_records)
    return tuple(sorted(paths))


def _clear_replacement_trust(lease: AuthoringLease) -> None:
    from agenthicc.plugins.trust import current_extension_trust

    current_extension_trust().restore_records(_trust_paths(lease), ())


def _restore_trust(lease: AuthoringLease) -> None:
    from agenthicc.plugins.trust import TrustRecord, current_extension_trust

    records = tuple(
        TrustRecord(
            path=record.path,
            sha256=record.sha256,
            kind=record.kind,
            trusted_at=record.trusted_at,
        )
        for record in lease.original_trust_records
    )
    current_extension_trust().restore_records(_trust_paths(lease), records)


def _revoke_candidate_trust(lease: AuthoringLease) -> None:
    from agenthicc.plugins.trust import current_extension_trust

    trust = current_extension_trust()
    target = Path(lease.target_path)
    if target.is_dir() and not target.is_symlink():
        for source in target.rglob("*.py"):
            _reject_symlink(source)
            trust.revoke(source)


def restore_authoring(lease: AuthoringLease) -> str:
    """Restore the original bytes or discard a first-generation candidate."""
    _validate_lease_fields(lease)
    lock_status = _lock_status(lease)
    root = Path(lease.workspace_root)
    target = Path(lease.target_path)
    marker = Path(lease.marker_path)
    _reject_symlink_components(target, root)
    marker_present = marker.exists() or marker.is_symlink()

    if not marker_present:
        if lease.replacing:
            snapshot = inspect_existing_target(
                root,
                run_id=lease.run_id,
                workflow_name=lease.workflow_name,
            )
            if (
                snapshot.fingerprint == lease.original_fingerprint
                and snapshot.target_exists == lease.original_target_exists
                and snapshot.legacy_exists == lease.original_legacy_exists
            ):
                _restore_trust(lease)
                _write_json_atomic(Path(lease.lock_path), _lease_record(lease, "restored"))
                return "restored"
        elif not target.exists():
            _write_json_atomic(Path(lease.lock_path), _lease_record(lease, "restored"))
            return "discarded"
        if lock_status == "committed":
            raise AuthoringError("workflow authoring transaction is already committed")
        if lock_status != "preparing":
            raise AuthoringError("workflow pending marker is missing; refusing recovery")

    if marker_present:
        marker_record = _read_lease_record(marker, label="workflow pending marker")
        if not _same_lease(marker_record.lease, lease):
            raise AuthoringError("workflow pending marker belongs to another run")
        if lock_status == "committed" and marker_record.status != "committed":
            raise AuthoringError("workflow marker and lock lifecycle states disagree")

    backup = (
        _verify_preparing_backup(lease)
        if lock_status == "preparing" and not marker_present and lease.replacing
        else _verify_backup(lease)
        if lease.replacing
        else Path(lease.backup_path)
    )
    legacy_path = target.parent / f"{lease.workflow_name}.py"
    preparing_recovery = lock_status == "preparing" and not marker_present
    package_backup = backup / "package"
    target_is_original = (
        preparing_recovery and lease.original_target_exists and not package_backup.exists()
    )
    if not target_is_original:
        _revoke_candidate_trust(lease)
    if target.exists() and not target_is_original:
        if target.is_symlink() or not target.is_dir():
            raise AuthoringError("pending workflow target is not a regular directory")
        _remove_tree(target)

    original_restored = False
    if lease.replacing:
        package = backup / "package"
        legacy = backup / "legacy.py"
        if package.exists():
            _reject_symlink_components(package, root)
            os.replace(package, target)
            original_restored = True
        elif target_is_original:
            original_restored = True
        if legacy.exists():
            _reject_symlink_components(legacy, root)
            os.replace(legacy, legacy_path)
            original_restored = True
        elif preparing_recovery and lease.original_legacy_exists:
            original_restored = True
        if backup.exists():
            _remove_tree(backup)
        _restore_trust(lease)

    _write_json_atomic(Path(lease.lock_path), _lease_record(lease, "restored"))
    return "restored" if original_restored else "discarded"


def _sync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return


def finalize_authoring(lease: AuthoringLease, *, expected_fingerprint: str) -> PackageManifest:
    """Trust validated bytes, recheck the manifest, then atomically expose them."""
    if not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint):
        raise AuthoringError("validation evidence has no valid package fingerprint")
    manifest = scan_package(lease)
    if manifest.fingerprint != expected_fingerprint:
        raise AuthoringError("workflow package changed after validation approval")

    _write_marker(lease, "committing")
    _write_json_atomic(Path(lease.lock_path), _lease_record(lease, "committing"))
    from agenthicc.plugins.trust import current_extension_trust

    trust = current_extension_trust()
    package_root = Path(lease.target_path)
    for item in manifest.files:
        if item.path.endswith(".py"):
            trust.trust(package_root / item.path, kind="workflow")

    current = scan_package(lease)
    if current.fingerprint != expected_fingerprint:
        raise AuthoringError("workflow package changed while trust was being recorded")
    _write_json_atomic(Path(lease.lock_path), _lease_record(lease, "committed"))
    _write_marker(lease, "committed")
    marker = Path(lease.marker_path)
    marker_record = _read_lease_record(marker, label="workflow pending marker")
    if not _same_lease(marker_record.lease, lease):
        raise AuthoringError("workflow pending marker changed during finalization")
    marker.unlink()
    _sync_directory(marker.parent)
    return current
