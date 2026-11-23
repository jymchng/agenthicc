"""Runtime-owned installation and provenance of validated project extensions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agenthicc.plugins.authored_types import (
    PublishedArtifact,
    AuthoredRun,
    StagedArtifact,
    StagedWorkflow,
    _digest,
    _relative,
    _run_id,
    content_digest,
)
from agenthicc.plugins.trust import TrustRecord, _authored_state_directory, current_extension_trust
from agenthicc.runners.process_lease import (
    InterProcessLock,
    atomic_replace,
    directory_fsync,
    read_json_object,
)
from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.workflows.authoring.verdict import ValidationReport

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agenthicc.plugins.authored_types import ArtifactKind
    from agenthicc.plugins.trust import ExtensionTrust
    from agenthicc.workflows.create_workflow.authoring import AuthoringLease, ExistingWorkflowTarget


_MAX_FILE_BYTES = 1_000_000
_VERSION_LIMIT = 5
_PUBLISH_APPROVAL = Literal["policy", "user"]


class AuthoredError(ValueError):
    """A safe, machine-readable rejection of an authoring operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AuthoredRecord:
    """An installed artifact with the provenance of its accepted run."""

    artifact: PublishedArtifact
    run_id: str
    session_id: str
    authored_at: str
    model: str
    approved_by: _PUBLISH_APPROVAL

    def to_dict(self) -> dict[str, object]:
        return {
            **self.artifact.to_dict(),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "authored_at": self.authored_at,
            "model": self.model,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_payload(cls, value: object) -> AuthoredRecord:
        artifact = PublishedArtifact.from_payload(value)
        if not isinstance(value, dict):
            raise ValueError("Invalid authored provenance.")
        data: Mapping[str, object] = value
        approved_by = data.get("approved_by")
        if approved_by not in ("policy", "user"):
            raise ValueError("Invalid authored approval source.")
        identity = _field(data, "run_id")
        _run_id(identity)
        return cls(
            artifact,
            identity,
            _field(data, "session_id"),
            _field(data, "authored_at"),
            _field(data, "model"),
            "user" if approved_by == "user" else "policy",
        )


@dataclass(frozen=True, slots=True)
class _InstallFile:
    source: Path
    target: Path
    data: bytes
    prior: bytes | None
    artifact: PublishedArtifact

    @property
    def requires_trust(self) -> bool:
        return self.artifact.kind != "skill" and self.target.suffix == ".py"


@dataclass(frozen=True, slots=True)
class _CanonicalWorkflow:
    candidate: StagedWorkflow
    fingerprint: str


def _field(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Invalid authored provenance field {key!r}.")
    return value


def _workspace_path(view: WorkspaceView, relative: str) -> Path:
    path = view.root.joinpath(*_relative(relative).parts)
    try:
        safe = view.resolve(path)
    except PermissionError as exc:
        raise AuthoredError(
            "outside_workspace", "The authoring path escapes its workspace."
        ) from exc
    if safe != path:
        raise AuthoredError("symlink", "An authoring path resolves through a symlink.")
    return safe


def _source_bytes(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise AuthoredError("invalid_source", "An authored file must be regular and unlinked.")
    flags = os.O_RDONLY
    if os.name == "posix":
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(metadata, opened) or opened.st_nlink != 1:
            raise AuthoredError("changed_bytes", "An authored file changed before it was read.")
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise AuthoredError("file_too_large", "An authored file exceeds the size limit.")
    return data


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _reserved_names(kind: ArtifactKind) -> frozenset[str]:
    if kind == "tool":
        from agenthicc.agent_tools import AGENT_TOOLS
        from agenthicc.skills.tools import SKILL_TOOL_NAMES
        from agenthicc.tools.base import tool_name
        from agenthicc.workflows.stack_tools import WORKFLOW_TOOL_NAMES

        return (
            frozenset(map(tool_name, AGENT_TOOLS))
            | SKILL_TOOL_NAMES
            | WORKFLOW_TOOL_NAMES
            | {"reload_tools", "spawn_subagents"}
        )
    if kind == "workflow":
        from agenthicc.workflows.loader import builtin_workflow_descriptors

        return frozenset(item.name for item in builtin_workflow_descriptors())
    if kind == "command":
        from agenthicc.commands.builtins import BUILTIN_COMMANDS

        return frozenset(
            name.lstrip("/")
            for command in BUILTIN_COMMANDS
            for name in (command.name, *command.aliases)
        )
    from agenthicc.skills.bootstrap import _DEFAULTS

    return frozenset(_DEFAULTS)


class AuthoredArtifactStore:
    """Install approved extensions under a workspace and keep operator-owned provenance."""

    def __init__(self, workspace: Path, *, trust: ExtensionTrust | None = None) -> None:
        self._workspace = WorkspaceView(workspace)
        self.trust = trust or current_extension_trust()
        self._assert_project(self._workspace)
        operator = _authored_state_directory(self.workspace, self.trust.store_path)
        self._state = operator / "manifest.json"
        self._lock = operator / "publish.lock"
        self._staged = operator / "staged"
        self._versions = operator / "versions"
        self._pending = operator / "pending.json"
        self._undo = operator / "pending-files"

    @property
    def workspace(self) -> Path:
        return self._workspace.root

    def _assert_project(self, view: WorkspaceView) -> None:
        user_root = (Path.home() / ".agenthicc").resolve()
        if any(view.root.is_relative_to(root) for root in (user_root, *self.trust.trusted_roots)):
            raise AuthoredError("trusted_location", "User-global directories cannot be authored.")
        if self.trust.store_path.resolve().is_relative_to(view.root):
            raise AuthoredError(
                "trusted_location", "The operator trust store must be outside the workspace."
            )

    def stage_directory(self, run_id: str, kind: ArtifactKind) -> Path:
        """Create the scoped staging directory owned by an authoring run."""
        path = self._path(f".agenthicc/.staging/{_run_id(run_id)}/{kind}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, relative: str) -> Path:
        return _workspace_path(self._workspace, relative)

    def records(self) -> tuple[AuthoredRecord, ...]:
        """Read provenance from the operator's state, independent of repository metadata."""
        try:
            value: object = json.loads(self._state.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        if not isinstance(value, list):
            raise AuthoredError("invalid_provenance", "Authored provenance must be a list.")
        return tuple(AuthoredRecord.from_payload(item) for item in value)

    def describe(self) -> str:
        """List published and staged extensions without their file bodies."""
        lines = [
            f"{record.artifact.kind} {record.artifact.name} · {record.artifact.digest[:12]} · "
            f"{record.artifact.state} · run {record.run_id}"
            for record in self.records()
        ]
        for run in self.staged_runs():
            for staged in run.candidates:
                artifact = staged.artifact
                lines.append(
                    f"{artifact.kind} {artifact.name} · {artifact.digest[:12]} · staged · run {run.run_id}"
                )
        return "\n".join(lines) if lines else "No authored extensions in this workspace."

    def _source_path(self, view: WorkspaceView, run_id: str, relative: str) -> Path:
        _, surface, *parts = _relative(relative).parts
        staged = "/".join((".agenthicc", ".staging", run_id, surface.removesuffix("s"), *parts))
        return _workspace_path(view, staged)

    def _gate(
        self, run: AuthoredRun, view: WorkspaceView
    ) -> tuple[dict[str, tuple[Path, bytes, PublishedArtifact]], tuple[_CanonicalWorkflow, ...]]:
        inventory: dict[str, tuple[Path, bytes, PublishedArtifact]] = {}
        workflows: list[_CanonicalWorkflow] = []
        for item in run.candidates:
            artifact = item.artifact
            if artifact.name in _reserved_names(artifact.kind):
                raise AuthoredError("name_conflict", f"{artifact.name} is already in use.")
            if (
                item.verdict.errors
                or item.verdict.digest != artifact.digest
                or artifact.state != "staged"
            ):
                raise AuthoredError(
                    "validation_failed", "The runner verdict did not approve every artifact."
                )
            if isinstance(item, StagedWorkflow):
                workflows.append(self._workflow_package(item, run.run_id, view))
            candidate_files: dict[str, bytes] = {}
            for relative in artifact.files:
                path = (
                    _workspace_path(view, relative)
                    if isinstance(item, StagedWorkflow)
                    else self._source_path(view, run.run_id, relative)
                )
                data = _source_bytes(path)
                candidate_files[relative] = data
                previous = inventory.get(relative)
                if previous is not None and previous[1] != data:
                    raise AuthoredError("changed_bytes", "Shared source changed after validation.")
                if previous is None or previous[2].supersedes is None:
                    inventory[relative] = (path, data, artifact)
            if content_digest(candidate_files) != artifact.digest:
                raise AuthoredError("changed_bytes", f"{artifact.name} changed after validation.")
        return inventory, tuple(workflows)

    def _workflow_package(
        self, item: StagedWorkflow, run_id: str, view: WorkspaceView
    ) -> _CanonicalWorkflow:
        from agenthicc.workflows.create_workflow.authoring import AuthoringError, scan_package

        lease = item.lease
        if lease.run_id != run_id or Path(lease.workspace_root) != view.root:
            raise AuthoredError("workflow_lease", "The workflow lease belongs to another run.")
        try:
            manifest = scan_package(lease)
        except AuthoringError as exc:
            raise AuthoredError(
                "workflow_lease", "The workflow lease could not be verified."
            ) from exc
        target = Path(lease.target_path).relative_to(view.root).as_posix()
        files = tuple(sorted(f"{target}/{file.path}" for file in manifest.files))
        if files != item.artifact.files:
            raise AuthoredError("changed_bytes", "The workflow inventory changed after validation.")
        return _CanonicalWorkflow(item, manifest.fingerprint)

    def _discard(self, run_id: str, view: WorkspaceView) -> None:
        location = _workspace_path(view, f".agenthicc/.staging/{_run_id(run_id)}")
        if location.is_dir():
            shutil.rmtree(location)
            directory_fsync(location.parent)

    def discard(self, run: AuthoredRun, *, source_root: Path | None = None) -> None:
        """Discard the candidate set owned by the run."""
        view = self._candidate_view(source_root)
        with InterProcessLock(self._lock):
            self._recover_pending()
            self._discard_candidates(run, view)

    def discard_staging(self, run_id: str) -> None:
        """Remove an unfinished source set that has no publication ticket."""
        with InterProcessLock(self._lock):
            if self._stage_ticket(run_id).exists():
                raise AuthoredError("publication_pending", "A staged approval owns this run.")
            self._discard(run_id, self._workspace)

    def _discard_candidates(self, run: AuthoredRun, view: WorkspaceView) -> None:
        from agenthicc.plugins.trust import override_extension_trust
        from agenthicc.workflows.create_workflow.authoring import restore_authoring

        with override_extension_trust(self.trust):
            for item in run.candidates:
                if not isinstance(item, StagedWorkflow):
                    continue
                lease = item.lease
                if lease.run_id != run.run_id or Path(lease.workspace_root) != view.root:
                    raise AuthoredError(
                        "workflow_lease", "The workflow lease belongs to another run."
                    )
                restore_authoring(lease)
        self._discard(run.run_id, view)

    def _stage_ticket(self, run_id: str) -> Path:
        return self._staged / f"{_run_id(run_id)}.json"

    def stage(
        self, run: AuthoredRun, *, source_root: Path | None = None
    ) -> tuple[PublishedArtifact, ...]:
        """Keep a verified set for a later operator decision."""
        view = self._candidate_view(source_root)
        with InterProcessLock(self._lock):
            self._recover_pending()
            try:
                inventory, workflows = self._gate(run, view)
            except (AuthoredError, OSError):
                self._discard_candidates(run, view)
                raise
            artifacts = tuple(item.artifact for item in run.candidates)
            ticket = self._stage_ticket(run.run_id)
            document = _json_bytes(
                {
                    "run_id": run.run_id,
                    "session_id": run.session_id,
                    "model": run.model,
                    "artifacts": [artifact.to_dict() for artifact in artifacts],
                    "workflow_leases": {
                        item.candidate.artifact.name: item.candidate.lease.to_dict()
                        for item in workflows
                        if view.root == self.workspace
                    },
                }
            )
            if ticket.exists():
                if ticket.read_bytes() != document:
                    raise AuthoredError(
                        "name_conflict", "An approval ticket already owns this run."
                    )
                return artifacts
            if view.root != self.workspace:
                for relative, (_, data, _) in inventory.items():
                    local = self._source_path(self._workspace, run.run_id, relative)
                    atomic_replace(local, data, prefix=".authored-stage-")
            atomic_replace(ticket, document, prefix=".authored-ticket-")
            return artifacts

    def staged(self, run_id: str) -> AuthoredRun:
        """Read the operator-owned snapshot of a set that passed the gate."""
        try:
            payload: object = json.loads(self._stage_ticket(run_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthoredError("unknown_run", f"No staged artifacts for run {run_id}.") from exc
        if not isinstance(payload, dict):
            raise AuthoredError("invalid_provenance", "The approval ticket is invalid.")
        data: Mapping[str, object] = payload
        artifacts = data.get("artifacts")
        if _field(data, "run_id") != run_id or not isinstance(artifacts, list):
            raise AuthoredError("invalid_provenance", "The approval ticket is invalid.")
        from agenthicc.workflows.create_workflow.authoring import AuthoringLease

        raw_leases = data.get("workflow_leases", {})
        if not isinstance(raw_leases, dict):
            raise AuthoredError("invalid_provenance", "The workflow approval ticket is invalid.")
        candidates: list[StagedArtifact] = []
        for artifact in map(PublishedArtifact.from_payload, artifacts):
            report = ValidationReport(artifact.digest, ())
            if artifact.kind == "workflow" and artifact.name in raw_leases:
                lease = AuthoringLease.from_payload(raw_leases[artifact.name])
                candidates.append(StagedWorkflow(artifact, report, lease))
            else:
                candidates.append(StagedArtifact(artifact, report))
        return AuthoredRun(
            run_id,
            _field(data, "session_id"),
            _field(data, "model"),
            tuple(candidates),
        )

    def staged_runs(self) -> tuple[AuthoredRun, ...]:
        """List the runs waiting for operator approval."""
        return tuple(self.staged(ticket.stem) for ticket in sorted(self._staged.glob("*.json")))

    def reject_staged(self, run_id: str) -> AuthoredRun:
        """Discard an operator-queued set without trusting any of its content."""
        with InterProcessLock(self._lock):
            self._recover_pending()
            run = self.staged(run_id)
            self._discard_candidates(run, self._workspace)
            self._stage_ticket(run_id).unlink()
            return run

    def _candidate_view(self, source_root: Path | None) -> WorkspaceView:
        view = self._workspace if source_root is None else WorkspaceView(source_root)
        self._assert_project(view)
        return view

    def _plan(
        self,
        run: AuthoredRun,
        inventory: Mapping[str, tuple[Path, bytes, PublishedArtifact]],
        records: tuple[AuthoredRecord, ...],
    ) -> tuple[_InstallFile, ...]:
        owned = {(item.artifact.kind, item.artifact.name): item.artifact for item in records}
        incoming = frozenset((item.artifact.kind, item.artifact.name) for item in run.candidates)
        for candidate in run.candidates:
            artifact = candidate.artifact
            old = owned.get((artifact.kind, artifact.name))
            if (None if old is None else old.digest) != artifact.supersedes:
                raise AuthoredError("name_conflict", f"{artifact.name} is already in use.")
        planned: list[_InstallFile] = []
        for relative, (source, data, artifact) in inventory.items():
            target = self._path(relative)
            prior: bytes | None = None
            if target.exists():
                if source != target and not any(
                    relative in previous.files and previous.digest == artifact.supersedes
                    for previous in owned.values()
                ):
                    raise AuthoredError(
                        "not_authored", "A destination is outside the approved authored version."
                    )
                if any(
                    relative in previous.files and identity not in incoming
                    for identity, previous in owned.items()
                ):
                    raise AuthoredError(
                        "name_conflict", "Artifacts sharing a source file must update together."
                    )
                prior = _source_bytes(target)
            planned.append(_InstallFile(source, target, data, prior, artifact))
        return tuple(planned)

    def _install_file(self, source: Path, target: Path) -> None:
        atomic_replace(target, _source_bytes(source), prefix=".authored-install-")

    def _begin_pending(
        self,
        run_id: str,
        original_files: Mapping[Path, bytes | None],
        live: tuple[PublishedArtifact, ...],
        trust_paths: tuple[Path, ...],
        old_trust: tuple[TrustRecord, ...],
    ) -> None:
        before: dict[str, bool] = {}
        for file, original in original_files.items():
            path = file.relative_to(self.workspace).as_posix()
            before[path] = original is not None
            if original is not None:
                atomic_replace(self._undo / run_id / path, original, prefix=".authored-undo-")
        atomic_replace(
            self._pending,
            _json_bytes(
                {
                    "run_id": run_id,
                    "files": sorted(before),
                    "prior_files": before,
                    "trust_paths": [
                        path.relative_to(self.workspace).as_posix() for path in trust_paths
                    ],
                    "prior_trust": [item.to_dict() for item in old_trust],
                    "artifacts": [artifact.to_dict() for artifact in live],
                    "mirror_existed": self._path(".agenthicc/authored.json").exists(),
                }
            ),
            prefix=".authored-pending-",
        )

    def recover(self) -> None:
        """Reconcile an interrupted publication before a new operator operation."""
        with InterProcessLock(self._lock):
            self._recover_pending()

    @classmethod
    def recover_workspace(cls, workspace: Path) -> None:
        """Reconcile a pending workspace transaction before extension discovery."""
        trust = current_extension_trust()
        root = workspace.resolve()
        operator = _authored_state_directory(root, trust.store_path)
        if (operator / "pending.json").is_file():
            cls(root, trust=trust).recover()

    def _recover_pending(self) -> None:
        document = read_json_object(self._pending, max_bytes=1_000_000)
        if document is None:
            return
        run_id = _run_id(_field(document, "run_id"))
        artifacts = document.get("artifacts")
        prior_files = document.get("prior_files")
        prior_trust = document.get("prior_trust")
        trust_paths = document.get("trust_paths")
        if not (
            isinstance(artifacts, list)
            and artifacts
            and isinstance(prior_files, dict)
            and isinstance(prior_trust, list)
            and isinstance(trust_paths, list)
            and all(isinstance(path, str) for path in trust_paths)
        ):
            raise AuthoredError("invalid_provenance", "The pending publication record is invalid.")
        recorded = self.records()
        completed = {item.artifact for item in recorded if item.run_id == run_id}
        attempted = set(map(PublishedArtifact.from_payload, artifacts))
        if completed != attempted:
            for relative, existed in prior_files.items():
                if not isinstance(relative, str) or not isinstance(existed, bool):
                    raise AuthoredError(
                        "invalid_provenance", "The pending file inventory is invalid."
                    )
                target = self._path(relative)
                if existed:
                    previous = _source_bytes(self._undo / run_id / relative)
                    atomic_replace(target, previous, prefix=".authored-restore-")
                else:
                    target.unlink(missing_ok=True)
            self.trust.restore_records(
                tuple(self._path(path) for path in trust_paths),
                tuple(map(TrustRecord.from_payload, prior_trust)),
            )
            mirror = self._path(".agenthicc/authored.json")
            if document.get("mirror_existed") is True:
                atomic_replace(
                    mirror,
                    _json_bytes([record.to_dict() for record in recorded]),
                    prefix=".authored-restore-",
                )
            else:
                mirror.unlink(missing_ok=True)
        self._pending.unlink(missing_ok=True)
        directory_fsync(self._pending.parent)
        undo = self._undo / run_id
        if undo.is_dir():
            shutil.rmtree(undo)

    @contextmanager
    def commit(
        self,
        run: AuthoredRun,
        *,
        source_root: Path | None = None,
        approved_by: _PUBLISH_APPROVAL = "policy",
    ) -> Iterator[tuple[PublishedArtifact, ...]]:
        """Install a complete set provisionally and commit it after loader verification.

        A failed caller check restores the exact prior file and
        trust state. The operator-owned manifest records only completed sets.
        """
        view = self._candidate_view(source_root)
        with InterProcessLock(self._lock):
            self._recover_pending()
            try:
                inventory, workflows = self._gate(run, view)
            except (AuthoredError, OSError):
                self._discard_candidates(run, view)
                raise
            canonical = {
                item.candidate.artifact.name: item
                for item in workflows
                if view.root == self.workspace
            }
            records = self.records()
            plan = self._plan(run, inventory, records)
            replacing = {
                (item.artifact.kind, item.artifact.name)
                for item in run.candidates
                if item.artifact.supersedes is not None
            }
            versions = tuple(
                record
                for record in records
                if (record.artifact.kind, record.artifact.name) in replacing
            )
            for record in versions:
                workflow = (
                    canonical.get(record.artifact.name)
                    if record.artifact.kind == "workflow"
                    else None
                )
                self._archive(
                    record, lease=workflow.candidate.lease if workflow is not None else None
                )
            retained = set(inventory)
            retained.update(
                relative
                for record in records
                if record not in versions
                for relative in record.artifact.files
            )
            retired = {
                self._path(relative)
                for record in versions
                for relative in record.artifact.files
                if relative not in retained
            }
            retired = {path for path in retired if path.exists()}
            retired_code = tuple(path for path in retired if path.suffix == ".py")
            trust_paths = (
                *tuple(item.target for item in plan if item.requires_trust),
                *retired_code,
            )
            old_trust = tuple(
                record for record in self.trust.records() if Path(record.path) in trust_paths
            )
            live = tuple(replace(item.artifact, state="loaded") for item in run.candidates)
            original_files = {item.target: item.prior for item in plan}
            original_files.update({path: _source_bytes(path) for path in retired})
            for package in canonical.values():
                for name in (
                    package.candidate.lease.marker_path,
                    package.candidate.lease.lock_path,
                ):
                    path = Path(name)
                    original_files[path] = _source_bytes(path)
            self._begin_pending(run.run_id, original_files, live, trust_paths, old_trust)
            try:
                for item in plan:
                    if _source_bytes(item.source) != item.data:
                        raise AuthoredError("changed_bytes", "An authored file changed at commit.")
                    if item.source == item.target:
                        continue
                    self._install_file(item.source, item.target)
                    if item.requires_trust:
                        self.trust.trust(
                            item.target,
                            kind=item.artifact.kind,
                            expected_sha256=hashlib.sha256(item.data).hexdigest(),
                            artifact_digest=item.artifact.digest,
                        )
                    elif _source_bytes(item.target) != item.data:
                        raise AuthoredError("changed_bytes", "An authored file changed at commit.")
                if canonical:
                    self._finalize_workflows(tuple(canonical.values()))
                for path in sorted(retired):
                    path.unlink()
                self.trust.restore_records(retired_code, ())
                yield live
                self._record_loaded(run, live, records, approved_by)
            except BaseException:
                self._recover_pending()
                raise
            self._recover_pending()
            self._discard(run.run_id, view)
            self._stage_ticket(run.run_id).unlink(missing_ok=True)
            for record in versions:
                self._prune_versions(record.artifact)

    def _finalize_workflows(self, packages: tuple[_CanonicalWorkflow, ...]) -> None:
        from agenthicc.plugins.trust import override_extension_trust
        from agenthicc.workflows.create_workflow.authoring import finalize_authoring

        with override_extension_trust(self.trust):
            for package in packages:
                candidate = package.candidate
                finalize_authoring(
                    candidate.lease,
                    expected_fingerprint=package.fingerprint,
                    artifact_digest=candidate.artifact.digest,
                )

    def _record_loaded(
        self,
        run: AuthoredRun,
        live: tuple[PublishedArtifact, ...],
        records: tuple[AuthoredRecord, ...],
        approved_by: _PUBLISH_APPROVAL,
    ) -> None:
        accepted = datetime.now(timezone.utc).isoformat()
        merged = {(item.artifact.kind, item.artifact.name): item for item in records}
        for artifact in live:
            merged[artifact.kind, artifact.name] = AuthoredRecord(
                artifact, run.run_id, run.session_id, accepted, run.model, approved_by
            )
        self._write_records(tuple(record for _, record in sorted(merged.items())), prior=records)

    def _write_records(
        self, records: tuple[AuthoredRecord, ...], *, prior: tuple[AuthoredRecord, ...]
    ) -> None:
        document = _json_bytes([record.to_dict() for record in records])
        mirror = self._path(".agenthicc/authored.json")
        existed = mirror.exists()
        atomic_replace(mirror, document, prefix=".authored-")
        try:
            atomic_replace(self._state, document, prefix=".authored-")
        except BaseException:
            if existed:
                previous = _json_bytes([record.to_dict() for record in prior])
                atomic_replace(mirror, previous, prefix=".authored-restore-")
            else:
                mirror.unlink(missing_ok=True)
            raise

    def _find(self, name: str) -> tuple[AuthoredRecord, tuple[AuthoredRecord, ...]]:
        records = self.records()
        matching = tuple(record for record in records if record.artifact.name == name)
        if not matching:
            raise AuthoredError("unknown_artifact", f"No authored artifact named {name!r}.")
        if len(matching) != 1:
            raise AuthoredError("name_conflict", f"More than one extension kind uses {name!r}.")
        return matching[0], records

    def record(self, name: str) -> AuthoredRecord:
        """Find one live record by its operator-visible name."""
        return self._find(name)[0]

    def workflow_update_digest(self, snapshot: ExistingWorkflowTarget) -> str | None:
        """Return the owned current version after rechecking its live bytes."""
        name = snapshot.workflow_name
        for record in self.records():
            artifact = record.artifact
            if artifact.kind != "workflow" or artifact.name != name:
                continue
            prefix = f".agenthicc/workflows/{name}/"
            files = tuple(prefix + item.path.removeprefix("package/") for item in snapshot.files)
            if snapshot.legacy_exists or files != artifact.files:
                raise AuthoredError("untrusted_modified", f"{name} has unrecorded files.")
            sources = {relative: _source_bytes(self._path(relative)) for relative in artifact.files}
            if content_digest(sources) != artifact.digest:
                raise AuthoredError("untrusted_modified", f"{name} changed since publication.")
            return artifact.digest
        return None

    def _archive_root(self, artifact: PublishedArtifact) -> str:
        return f".agenthicc/authored/versions/{artifact.name}/{artifact.digest}"

    def _archive_path(self, artifact: PublishedArtifact, relative: str) -> Path:
        return self._path(f"{self._archive_root(artifact)}/{relative}")

    def _version_index(self, artifact: PublishedArtifact) -> Path:
        return self._versions / artifact.kind / artifact.name / f"{artifact.digest}.json"

    def _archive(self, record: AuthoredRecord, *, lease: AuthoringLease | None = None) -> None:
        artifact = record.artifact
        data: dict[str, bytes] = {}
        for relative in artifact.files:
            source = self._path(relative)
            if lease is not None:
                original = (
                    Path(lease.backup_path) / "package" / source.relative_to(lease.target_path)
                )
                source = self._path(original.relative_to(self.workspace).as_posix())
            data[relative] = _source_bytes(source)
        if content_digest(data) != artifact.digest:
            raise AuthoredError("untrusted_modified", f"{artifact.name} changed since publication.")
        for relative, contents in data.items():
            atomic_replace(
                self._archive_path(artifact, relative), contents, prefix=".authored-version-"
            )
        atomic_replace(
            self._version_index(artifact),
            _json_bytes(record.to_dict()),
            prefix=".authored-version-",
        )

    def _version(self, current: AuthoredRecord, digest: str | None) -> AuthoredRecord:
        desired = digest or current.artifact.supersedes
        if desired is None:
            raise AuthoredError(
                "unknown_version", f"{current.artifact.name} has no previous version."
            )
        index = self._version_index(replace(current.artifact, digest=_digest(desired)))
        try:
            return AuthoredRecord.from_payload(json.loads(index.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise AuthoredError("unknown_version", f"No archived version {desired[:12]}.") from exc

    def _prune_versions(self, artifact: PublishedArtifact) -> None:
        directory = self._version_index(artifact).parent
        indexed = [
            (AuthoredRecord.from_payload(json.loads(item.read_text(encoding="utf-8"))), item)
            for item in directory.glob("*.json")
        ]
        indexed.sort(key=lambda pair: pair[0].authored_at, reverse=True)
        for record, index in indexed[_VERSION_LIMIT:]:
            archive = self._path(self._archive_root(record.artifact))
            if archive.is_dir():
                shutil.rmtree(archive)
            index.unlink(missing_ok=True)

    def _checked_archive(self, record: AuthoredRecord) -> dict[str, bytes]:
        artifact = record.artifact
        contents = {
            relative: _source_bytes(self._archive_path(artifact, relative))
            for relative in artifact.files
        }
        if content_digest(contents) != artifact.digest:
            raise AuthoredError(
                "changed_bytes", f"Archived bytes for {artifact.name} have changed."
            )
        return contents

    def diff(self, name: str) -> str:
        """Show changed lines against the previous approved version."""
        with InterProcessLock(self._lock):
            current, _ = self._find(name)
            previous = self._checked_archive(self._version(current, None))
            sections: list[str] = []
            for relative in sorted(previous.keys() | set(current.artifact.files)):
                target = self._path(relative)
                live = _source_bytes(target) if target.is_file() else b""
                before = previous.get(relative, b"").decode("utf-8", errors="replace")
                after = live.decode("utf-8", errors="replace")
                sections.extend(
                    unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile=f"previous/{relative}",
                        tofile=relative,
                        lineterm="",
                        n=0,
                    )
                )
            return "\n".join(sections)[:12_000]

    @contextmanager
    def rollback(
        self, name: str, digest: str | None = None
    ) -> Iterator[tuple[PublishedArtifact, ...]]:
        """Restore an operator-owned prior version through the normal commit gate."""
        run_id = uuid.uuid4().hex
        with InterProcessLock(self._lock):
            current, all_records = self._find(name)
            shared = set(current.artifact.files)
            related = tuple(
                record for record in all_records if shared.intersection(record.artifact.files)
            )
            candidates: list[StagedArtifact] = []
            for record in related:
                previous = self._version(record, digest)
                artifact = replace(
                    previous.artifact, state="staged", supersedes=record.artifact.digest
                )
                for relative, data in self._checked_archive(previous).items():
                    destination = self._source_path(self._workspace, run_id, relative)
                    atomic_replace(destination, data, prefix=".authored-rollback-")
                candidates.append(StagedArtifact(artifact, ValidationReport(artifact.digest, ())))
            run = AuthoredRun(run_id, current.session_id, current.model, tuple(candidates))
        with self.commit(run, approved_by="user") as installed:
            yield installed

    @contextmanager
    def remove(self, name: str) -> Iterator[tuple[PublishedArtifact, ...]]:
        """Remove the selected authored extension and any exports of its source."""
        with InterProcessLock(self._lock):
            current, records = self._find(name)
            shared = set(current.artifact.files)
            related = tuple(
                record for record in records if shared.intersection(record.artifact.files)
            )
            files = {
                self._path(relative) for record in related for relative in record.artifact.files
            }
            prior = {path: _source_bytes(path) for path in files if path.exists()}
            trust_paths = tuple(path for path in files if path.suffix == ".py")
            old_trust = tuple(
                record for record in self.trust.records() if Path(record.path) in trust_paths
            )
            try:
                for path in sorted(files):
                    path.unlink(missing_ok=True)
                self.trust.restore_records(trust_paths, ())
                yield tuple(record.artifact for record in related)
                self._write_records(
                    tuple(record for record in records if record not in related),
                    prior=records,
                )
            except BaseException:
                for path, data in prior.items():
                    atomic_replace(path, data, prefix=".authored-restore-")
                self.trust.restore_records(trust_paths, old_trust)
                raise
