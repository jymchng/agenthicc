"""Durable, workspace-scoped evidence for reconstruct-site runs.

The provider conversation remains the source of conversational continuity;
this store is the source of truth for large research and validation bodies.
Only small immutable references and digests need to be copied into a workflow
checkpoint.  Writes use the same atomic publication primitive as checkpoint
storage and every path is resolved through ``WorkspaceView``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from agenthicc.runners.process_lease import atomic_replace
from agenthicc.tools.sandbox import WorkspaceView

if TYPE_CHECKING:
    from agenthicc.tools.cloakbrowser.artifacts import BrowserArtifact

__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "EvidenceError",
    "EvidenceIntegrityError",
    "ArtifactRecord",
    "ScreenshotEvidence",
    "SkippedEvidencePhase",
    "ReentryEvidence",
    "EvidenceManifest",
    "ReconstructEvidenceStore",
]

EVIDENCE_SCHEMA_VERSION = 1
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_KEY = re.compile(r"(?i)(authorization|api[_ -]?key|token|password|cookie|secret)")


class EvidenceError(ValueError):
    """Base error for invalid evidence operations."""


class EvidenceIntegrityError(EvidenceError):
    """Raised when a manifest or complete artifact cannot be trusted."""


def _safe_component(value: str, label: str) -> str:
    text = str(value).strip()
    if not _SAFE_COMPONENT.fullmatch(text) or text in {".", ".."}:
        raise EvidenceError(f"invalid {label}: {value!r}")
    return text


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _mapping_items(value: object, label: str) -> list[Mapping[str, object]]:
    """Validate a manifest collection without silently dropping bad rows."""
    if not isinstance(value, list):
        raise EvidenceIntegrityError(f"manifest {label} must be a list")
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise EvidenceIntegrityError(f"manifest {label} contains a non-object")
        result.append(item)
    return result


def _string_items(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EvidenceIntegrityError(f"manifest {label} must be a list of strings")
    return tuple(value)


def _source_cells(value: Iterable[str]) -> tuple[str, ...]:
    """Normalize coverage-cell links without accepting an accidental string."""
    if isinstance(value, str):
        raise EvidenceError("source_cells must be an iterable of strings")
    try:
        raw = tuple(value)
    except TypeError as exc:
        raise EvidenceError("source_cells must be an iterable of strings") from exc
    if not all(isinstance(item, str) for item in raw):
        raise EvidenceError("source_cells must be an iterable of strings")
    result = tuple(item.strip() for item in raw)
    if any(not item for item in result):
        raise EvidenceError("source_cells must not contain empty values")
    if len(result) != len(set(result)):
        raise EvidenceError("source_cells must not contain duplicates")
    return result


def _safe_url(value: str) -> str:
    """Keep screenshot identity useful without retaining URL credentials."""
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return ""


def _optional_bool(value: object) -> bool | None:
    """Decode nullable boolean capture metadata without truthy-string bugs."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise EvidenceIntegrityError("screenshot load metadata must be boolean or null")


@dataclasses.dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Metadata for one complete or stale durable artifact."""

    artifact_id: str
    kind: str
    relative_path: str
    media_type: str
    sha256: str
    byte_count: int
    phase: str
    attempt: int
    status: str = "complete"
    source: str = "workflow"
    source_cells: tuple[str, ...] = ()
    created_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        value = dataclasses.asdict(self)
        value["source_cells"] = list(self.source_cells)
        return value

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ArtifactRecord":
        try:
            record = cls(
                artifact_id=str(raw["artifact_id"]),
                kind=str(raw["kind"]),
                relative_path=str(raw["relative_path"]),
                media_type=str(raw.get("media_type", "application/octet-stream")),
                sha256=str(raw["sha256"]),
                byte_count=int(str(raw["byte_count"])),
                phase=str(raw["phase"]),
                attempt=int(str(raw.get("attempt", 1))),
                status=str(raw.get("status", "complete")),
                source=str(raw.get("source", "workflow")),
                source_cells=_string_items(raw.get("source_cells", []), "artifact source_cells"),
                created_at=float(str(raw.get("created_at", time.time()))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("malformed artifact record") from exc
        if record.status not in {"complete", "stale", "degraded"}:
            raise EvidenceIntegrityError(f"invalid artifact status {record.status!r}")
        relative = Path(record.relative_path)
        if (
            not record.artifact_id.strip()
            or not record.kind.strip()
            or not record.phase.strip()
            or not record.relative_path.strip()
            or record.byte_count < 0
            or not re.fullmatch(r"[0-9a-f]{64}", record.sha256)
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise EvidenceIntegrityError("invalid artifact size or digest")
        return record


@dataclasses.dataclass(frozen=True, slots=True)
class ScreenshotEvidence:
    """A screenshot capture identity linked to a browser artifact."""

    screenshot_id: str
    role: str
    route: str
    url: str
    viewport: str
    width: int
    height: int
    device_scale: float
    page_state: str
    backend: str
    artifact_id: str | None
    sha256: str | None
    status: str = "complete"
    reason: str = ""
    source_revision: str = ""
    fonts_loaded: bool | None = None
    images_loaded: bool | None = None
    network_complete: bool | None = None
    redaction_status: str = "not_reported"
    created_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ScreenshotEvidence":
        try:
            result = cls(
                screenshot_id=str(raw["screenshot_id"]),
                role=str(raw["role"]),
                route=str(raw.get("route", "")),
                url=str(raw.get("url", "")),
                viewport=str(raw.get("viewport", "")),
                width=int(str(raw.get("width", 0))),
                height=int(str(raw.get("height", 0))),
                device_scale=float(str(raw.get("device_scale", 1.0))),
                page_state=str(raw.get("page_state", "default")),
                backend=str(raw.get("backend", "unknown")),
                artifact_id=(str(raw["artifact_id"]) if raw.get("artifact_id") else None),
                sha256=(str(raw["sha256"]) if raw.get("sha256") else None),
                status=str(raw.get("status", "complete")),
                reason=str(raw.get("reason", "")),
                source_revision=str(raw.get("source_revision", "")),
                fonts_loaded=_optional_bool(raw.get("fonts_loaded")),
                images_loaded=_optional_bool(raw.get("images_loaded")),
                network_complete=_optional_bool(raw.get("network_complete")),
                redaction_status=str(raw.get("redaction_status", "not_reported")),
                created_at=float(str(raw.get("created_at", time.time()))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("malformed screenshot record") from exc
        if result.role not in {"reference", "implementation", "exploratory"}:
            raise EvidenceIntegrityError(f"invalid screenshot role {result.role!r}")
        if result.status not in {"complete", "degraded", "stale"}:
            raise EvidenceIntegrityError(f"invalid screenshot status {result.status!r}")
        if result.width < 0 or result.height < 0 or result.device_scale <= 0:
            raise EvidenceIntegrityError("invalid screenshot dimensions")
        if result.status == "complete" and (not result.artifact_id or not result.sha256):
            raise EvidenceIntegrityError("complete screenshot has no artifact reference")
        if not result.redaction_status.strip():
            raise EvidenceIntegrityError("screenshot redaction status is empty")
        if result.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", result.sha256):
            raise EvidenceIntegrityError("invalid screenshot digest")
        if not result.screenshot_id.strip() or not result.route.strip():
            raise EvidenceIntegrityError("screenshot identity is incomplete")
        return result


@dataclasses.dataclass(frozen=True, slots=True)
class SkippedEvidencePhase:
    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"phase": self.name, "reason": self.reason}


@dataclasses.dataclass(frozen=True, slots=True)
class ReentryEvidence:
    source_phase: str
    target_phase: str
    reason: str
    invalidated_artifact_ids: tuple[str, ...]
    created_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_phase": self.source_phase,
            "target_phase": self.target_phase,
            "reason": self.reason,
            "invalidated_artifact_ids": list(self.invalidated_artifact_ids),
            "created_at": self.created_at,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceManifest:
    """Revisioned manifest containing only metadata and safe references."""

    run_id: str
    plan_version: str
    profile: str
    revision: int = 0
    artifacts: tuple[ArtifactRecord, ...] = ()
    screenshots: tuple[ScreenshotEvidence, ...] = ()
    skipped_phases: tuple[SkippedEvidencePhase, ...] = ()
    reentry_history: tuple[ReentryEvidence, ...] = ()
    status: str = "running"
    updated_at: float = dataclasses.field(default_factory=time.time)
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "plan_version": self.plan_version,
            "profile": self.profile,
            "revision": self.revision,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "screenshots": [item.to_dict() for item in self.screenshots],
            "skipped_phases": [item.to_dict() for item in self.skipped_phases],
            "reentry_history": [item.to_dict() for item in self.reentry_history],
            "status": self.status,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceManifest":
        if not isinstance(raw, Mapping) or raw.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            raise EvidenceIntegrityError("unsupported or malformed evidence manifest")
        if (
            not isinstance(raw.get("run_id"), str)
            or not isinstance(raw.get("plan_version"), str)
            or not str(raw.get("run_id")).strip()
            or not str(raw.get("plan_version")).strip()
            or not isinstance(raw.get("profile"), str)
            or not str(raw.get("profile")).strip()
        ):
            raise EvidenceIntegrityError("manifest identity is malformed")
        artifacts = _mapping_items(raw.get("artifacts", []), "artifacts")
        screenshots = _mapping_items(raw.get("screenshots", []), "screenshots")
        skipped = _mapping_items(raw.get("skipped_phases", []), "skipped_phases")
        reentries = _mapping_items(raw.get("reentry_history", []), "reentry_history")
        try:
            result = cls(
                run_id=raw["run_id"],
                plan_version=raw["plan_version"],
                profile=str(raw.get("profile", "")),
                revision=int(str(raw.get("revision", 0))),
                artifacts=tuple(ArtifactRecord.from_dict(item) for item in artifacts),
                screenshots=tuple(ScreenshotEvidence.from_dict(item) for item in screenshots),
                skipped_phases=tuple(
                    SkippedEvidencePhase(str(item.get("phase", "")), str(item.get("reason", "")))
                    for item in skipped
                ),
                reentry_history=tuple(
                    ReentryEvidence(
                        source_phase=str(item.get("source_phase", "")),
                        target_phase=str(item.get("target_phase", "")),
                        reason=str(item.get("reason", "")),
                        invalidated_artifact_ids=_string_items(
                            item.get("invalidated_artifact_ids", []),
                            "reentry invalidated_artifact_ids",
                        ),
                        created_at=float(str(item.get("created_at", time.time()))),
                    )
                    for item in reentries
                ),
                status=str(raw.get("status", "running")),
                updated_at=float(str(raw.get("updated_at", time.time()))),
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("manifest contains invalid values") from exc
        if result.revision < 0 or result.status not in {"running", "complete", "paused", "failed"}:
            raise EvidenceIntegrityError("manifest revision or status is invalid")
        # An artifact digest is content-addressed, so the same bytes can be
        # deliberately referenced by more than one phase (for example the
        # route/state inventory is useful to both recon and visual research).
        # A duplicate is only malformed when the same artifact is recorded for
        # the same phase/attempt.  Treating the phase as part of the manifest
        # identity also keeps manifests written before provenance links were
        # added resumable.
        artifact_keys = [
            (item.artifact_id, item.kind, item.phase, item.attempt) for item in result.artifacts
        ]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise EvidenceIntegrityError("manifest contains duplicate artifact records")
        screenshot_ids = [item.screenshot_id for item in result.screenshots]
        if len(screenshot_ids) != len(set(screenshot_ids)):
            raise EvidenceIntegrityError("manifest contains duplicate screenshot records")
        return result

    def compact(self, manifest_path: str) -> dict[str, object]:
        """Return bounded checkpoint data without embedding artifact bodies."""
        return {
            "manifest_path": manifest_path,
            "manifest_revision": self.revision,
            "plan_version": self.plan_version,
            "profile": self.profile,
            "artifact_refs": [
                {
                    "artifact_id": item.artifact_id,
                    "kind": item.kind,
                    "path": item.relative_path,
                    "sha256": item.sha256,
                    "status": item.status,
                }
                for item in self.artifacts
            ],
            "screenshot_ids": [item.screenshot_id for item in self.screenshots],
            "complete_artifact_count": sum(item.status == "complete" for item in self.artifacts),
            "stale_artifact_count": sum(item.status == "stale" for item in self.artifacts),
        }


class ReconstructEvidenceStore:
    """Atomic evidence store for one run and one authorized workspace."""

    def __init__(
        self, workspace: WorkspaceView, run_id: str, *, plan_version: str, profile: str
    ) -> None:
        self.workspace = workspace
        self.run_id = _safe_component(run_id, "run id")
        self._root_relative = Path(".agenthicc") / "reconstruct_site" / self.run_id
        self.manifest_relative_path = str(self._root_relative / "manifest.json")
        self._manifest_path = self.workspace.resolve(self.manifest_relative_path)
        self._lock = threading.RLock()
        if self._manifest_path.exists():
            self._manifest = self._load()
            if self._manifest.run_id != self.run_id:
                raise EvidenceIntegrityError("manifest run identity mismatch")
        else:
            self._manifest = EvidenceManifest(
                run_id=self.run_id,
                plan_version=plan_version,
                profile=profile,
            )

    @property
    def manifest(self) -> EvidenceManifest:
        return self._manifest

    def _load(self) -> EvidenceManifest:
        try:
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceIntegrityError("evidence manifest is missing or unreadable") from exc
        return EvidenceManifest.from_dict(raw)

    def _publish(self, manifest: EvidenceManifest) -> EvidenceManifest:
        payload = json.dumps(
            manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ).encode()
        atomic_replace(self._manifest_path, payload, prefix=".manifest-")
        self._manifest = manifest
        return manifest

    def _with(self, **changes: object) -> EvidenceManifest:
        return dataclasses.replace(
            self._manifest,
            **changes,  # type: ignore[arg-type]
            revision=self._manifest.revision + 1,
            updated_at=time.time(),
        )

    def set_metadata(
        self, *, profile: str | None = None, status: str | None = None
    ) -> EvidenceManifest:
        with self._lock:
            return self._publish(
                self._with(
                    **{
                        "profile": profile or self._manifest.profile,
                        "status": status or self._manifest.status,
                    }
                )
            )

    def set_skipped(self, phases: Iterable[tuple[str, str]]) -> EvidenceManifest:
        """Publish deterministic profile skip records."""
        records = tuple(SkippedEvidencePhase(str(name), str(reason)) for name, reason in phases)
        with self._lock:
            if records == self._manifest.skipped_phases:
                return self._manifest
            return self._publish(self._with(skipped_phases=records))

    def put(
        self,
        kind: str,
        content: bytes | str,
        *,
        phase: str,
        attempt: int = 1,
        media_type: str = "application/octet-stream",
        source: str = "workflow",
        suffix: str = ".bin",
        source_cells: Iterable[str] = (),
    ) -> ArtifactRecord:
        """Atomically write one artifact and publish its manifest record."""
        safe_kind = _safe_component(kind, "artifact kind")
        safe_phase = _safe_component(phase, "phase")
        if attempt < 1:
            raise EvidenceError("artifact attempt must be positive")
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest = _hash(raw)
        normalized_source_cells = _source_cells(source_cells)
        with self._lock:
            for existing in self._manifest.artifacts:
                if (
                    existing.phase == safe_phase
                    and existing.attempt == attempt
                    and existing.kind == safe_kind
                    and existing.sha256 == digest
                    and existing.status == "complete"
                ):
                    return existing
            normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", normalized_suffix):
                raise EvidenceError("artifact suffix must be a simple extension")
            filename = f"{safe_kind}-{digest[:16]}{normalized_suffix}"
            relative = self._root_relative / "phases" / safe_phase / str(attempt) / filename
            target = self.workspace.resolve(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing_bytes = target.read_bytes()
                if existing_bytes != raw:
                    raise EvidenceIntegrityError(f"artifact path collision for {relative}")
            else:
                atomic_replace(target, raw, prefix=".artifact-")
            record = ArtifactRecord(
                artifact_id=digest,
                kind=safe_kind,
                relative_path=str(relative),
                media_type=media_type,
                sha256=digest,
                byte_count=len(raw),
                phase=safe_phase,
                attempt=attempt,
                source=source,
                source_cells=normalized_source_cells,
            )
            self._publish(self._with(artifacts=self._upsert_artifact(record)))
            return record

    def put_json(
        self,
        kind: str,
        value: object,
        *,
        phase: str,
        attempt: int = 1,
        media_type: str = "application/json",
        source: str = "workflow",
        suffix: str = ".json",
        source_cells: Iterable[str] = (),
    ) -> ArtifactRecord:
        raw = json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        return self.put(
            kind,
            raw,
            phase=phase,
            attempt=attempt,
            media_type=media_type,
            source=source,
            suffix=suffix,
            source_cells=source_cells,
        )

    def record_screenshot(
        self,
        artifact: "BrowserArtifact | Mapping[str, object]",
        *,
        role: str,
        route: str,
        url: str,
        viewport: str,
        width: int,
        height: int,
        device_scale: float = 1.0,
        page_state: str = "default",
        backend: str = "unknown",
        phase: str = "visual_research",
        attempt: int = 1,
        source_cells: Iterable[str] = (),
        source_revision: str = "",
        fonts_loaded: bool | None = None,
        images_loaded: bool | None = None,
        network_complete: bool | None = None,
        redaction_status: str = "not_reported",
    ) -> ScreenshotEvidence:
        """Link an existing bounded browser artifact without copying it."""
        if role not in {"reference", "implementation", "exploratory"}:
            raise EvidenceError(f"invalid screenshot role {role!r}")
        if attempt < 1:
            raise EvidenceError("screenshot attempt must be positive")
        if width < 0 or height < 0 or device_scale <= 0:
            raise EvidenceError("invalid screenshot dimensions")
        if isinstance(artifact, Mapping):
            artifact_id = str(artifact.get("artifact_id", ""))
            relative_path = str(artifact.get("path", artifact.get("relative_path", "")))
        else:
            artifact_id = artifact.artifact_id
            relative_path = artifact.relative_path
        if not artifact_id or not relative_path:
            raise EvidenceError("browser artifact must include id and path")
        path = self.workspace.resolve(relative_path)
        try:
            relative_path = str(path.relative_to(self.workspace.root))
        except ValueError as exc:
            raise EvidenceError("browser screenshot is outside the workspace") from exc
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise EvidenceIntegrityError("browser screenshot is unreadable") from exc
        digest = _hash(raw)
        normalized_source_cells = _source_cells(source_cells)
        normalized_source_revision = str(source_revision).strip()
        normalized_redaction_status = str(redaction_status).strip() or "not_reported"
        identity: dict[str, object] = {
            "role": role,
            "route": route,
            "url": _safe_url(url),
            "viewport": viewport,
            "width": width,
            "height": height,
            "device_scale": device_scale,
            "page_state": page_state,
            "backend": backend,
            "sha256": digest,
            "source_revision": normalized_source_revision,
        }
        screenshot_id = _hash(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
        with self._lock:
            for existing in self._manifest.screenshots:
                if existing.screenshot_id == screenshot_id:
                    if existing.artifact_id and normalized_source_cells:
                        self.attach_artifact_source_cells(
                            existing.artifact_id, normalized_source_cells
                        )
                    return existing
            # The linked browser artifact remains outside the reconstruct tree,
            # but the manifest contains its validated workspace-relative path.
            linked = ArtifactRecord(
                artifact_id=artifact_id,
                kind="screenshot",
                relative_path=relative_path,
                media_type="image/png" if relative_path.endswith(".png") else "image/jpeg",
                sha256=digest,
                byte_count=len(raw),
                phase=phase,
                attempt=attempt,
                source="browser",
                source_cells=normalized_source_cells,
            )
            screenshots = (
                *self._manifest.screenshots,
                ScreenshotEvidence(
                    screenshot_id=screenshot_id,
                    role=role,
                    route=route,
                    url=_safe_url(url),
                    viewport=viewport,
                    width=width,
                    height=height,
                    device_scale=device_scale,
                    page_state=page_state,
                    backend=backend,
                    artifact_id=artifact_id,
                    sha256=digest,
                    source_revision=normalized_source_revision,
                    fonts_loaded=fonts_loaded,
                    images_loaded=images_loaded,
                    network_complete=network_complete,
                    redaction_status=normalized_redaction_status,
                ),
            )
            return self._publish(
                self._with(artifacts=self._upsert_artifact(linked), screenshots=screenshots)
            ).screenshots[-1]

    def record_degraded_screenshot(
        self,
        *,
        role: str,
        route: str,
        viewport: str,
        backend: str,
        reason: str,
    ) -> ScreenshotEvidence:
        if role not in {"reference", "implementation", "exploratory"}:
            raise EvidenceError(f"invalid screenshot role {role!r}")
        identity = json.dumps(
            {
                "role": role,
                "route": route,
                "viewport": viewport,
                "backend": backend,
                "reason": reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        screenshot_id = _hash(identity.encode())
        with self._lock:
            for existing in self._manifest.screenshots:
                if existing.screenshot_id == screenshot_id:
                    return existing
            result = ScreenshotEvidence(
                screenshot_id=screenshot_id,
                role=role,
                route=route,
                url="",
                viewport=viewport,
                width=0,
                height=0,
                device_scale=1.0,
                page_state="unavailable",
                backend=backend,
                artifact_id=None,
                sha256=None,
                status="degraded",
                reason=reason[:512],
            )
            self._publish(self._with(screenshots=(*self._manifest.screenshots, result)))
            return result

    def _upsert_artifact(self, record: ArtifactRecord) -> tuple[ArtifactRecord, ...]:
        return tuple(
            dataclasses.replace(
                record,
                source_cells=tuple(dict.fromkeys((*item.source_cells, *record.source_cells))),
                created_at=item.created_at,
            )
            if (
                item.artifact_id == record.artifact_id
                and item.kind == record.kind
                and item.phase == record.phase
                and item.attempt == record.attempt
            )
            else item
            for item in self._manifest.artifacts
        ) + tuple(
            ()
            if any(
                item.artifact_id == record.artifact_id
                and item.kind == record.kind
                and item.phase == record.phase
                and item.attempt == record.attempt
                for item in self._manifest.artifacts
            )
            else (record,)
        )

    def write_phase_receipt(
        self,
        phase: str,
        attempt: int,
        summary: str,
        *,
        transition: str,
        source_cells: Iterable[str] = (),
    ) -> ArtifactRecord:
        return self.put_json(
            "phase_receipt",
            {
                "phase": phase,
                "attempt": attempt,
                "summary": summary[:4000],
                "transition": transition,
            },
            phase=phase,
            attempt=attempt,
            source="workflow",
            source_cells=source_cells,
        )

    def invalidate(
        self, kinds: Iterable[str], *, source_phase: str, target_phase: str, reason: str
    ) -> tuple[str, ...]:
        wanted = set(kinds)
        with self._lock:
            affected = tuple(
                item.artifact_id
                for item in self._manifest.artifacts
                if item.kind in wanted and item.status == "complete"
            )
            artifacts = tuple(
                dataclasses.replace(item, status="stale") if item.artifact_id in affected else item
                for item in self._manifest.artifacts
            )
            record = ReentryEvidence(source_phase, target_phase, reason[:1000], affected)
            self._publish(
                self._with(
                    artifacts=artifacts, reentry_history=(*self._manifest.reentry_history, record)
                )
            )
            return affected

    def mark_stale_artifacts(self, artifact_ids: Iterable[str], *, reason: str) -> tuple[str, ...]:
        """Mark exactly the failed artifact records stale for targeted recovery."""
        wanted = {str(item) for item in artifact_ids if str(item)}
        if not wanted:
            return ()
        with self._lock:
            affected = tuple(
                item.artifact_id
                for item in self._manifest.artifacts
                if item.artifact_id in wanted and item.status == "complete"
            )
            if not affected:
                return ()
            stale = tuple(
                dataclasses.replace(item, status="stale") if item.artifact_id in wanted else item
                for item in self._manifest.artifacts
            )
            self._publish(self._with(artifacts=stale))
            return affected

    def attach_artifact_source_cells(self, artifact_id: str, source_cells: Iterable[str]) -> bool:
        """Add coverage provenance to an existing artifact record.

        Browser adapters create their own artifact records before the
        reconstruct phase knows which coverage cells the screenshot satisfies.
        This metadata-only operation closes that provenance link without
        copying or rewriting the browser-owned bytes.
        """
        normalized_source_cells = _source_cells(source_cells)
        if not normalized_source_cells:
            return False
        with self._lock:
            changed = False
            artifacts: list[ArtifactRecord] = []
            for item in self._manifest.artifacts:
                if item.artifact_id != artifact_id:
                    artifacts.append(item)
                    continue
                merged = tuple(dict.fromkeys((*item.source_cells, *normalized_source_cells)))
                changed = changed or merged != item.source_cells
                artifacts.append(dataclasses.replace(item, source_cells=merged))
            if not changed:
                return False
            self._publish(self._with(artifacts=tuple(artifacts)))
            return True

    def verify(self) -> list[dict[str, object]]:
        """Verify complete files and return structured recoverable errors."""
        errors: list[dict[str, object]] = []
        for record in self._manifest.artifacts:
            if record.status != "complete":
                continue
            try:
                path = self.workspace.resolve(record.relative_path)
                raw = path.read_bytes()
            except (OSError, PermissionError) as exc:
                errors.append(
                    {
                        "artifact_id": record.artifact_id,
                        "kind": record.kind,
                        "error": "missing_or_unreadable",
                        "detail": str(exc)[:256],
                    }
                )
                continue
            if len(raw) != record.byte_count or _hash(raw) != record.sha256:
                errors.append(
                    {
                        "artifact_id": record.artifact_id,
                        "kind": record.kind,
                        "error": "content_hash_mismatch",
                    }
                )
        return errors

    def read_kind(self, kind: str) -> bytes | None:
        candidates = [
            item
            for item in self._manifest.artifacts
            if item.kind == kind and item.status == "complete"
        ]
        if not candidates:
            return None
        record = max(candidates, key=lambda item: (item.created_at, item.artifact_id))
        path = self.workspace.resolve(record.relative_path)
        raw = path.read_bytes()
        if len(raw) != record.byte_count or _hash(raw) != record.sha256:
            raise EvidenceIntegrityError(
                f"artifact {record.artifact_id} failed integrity verification"
            )
        return raw

    def checkpoint_digest(self) -> dict[str, object]:
        return self._manifest.compact(self.manifest_relative_path)
