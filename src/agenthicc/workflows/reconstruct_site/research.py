"""Evidence-complete research contracts for ``reconstruct_site``.

The browser and LLM runners produce observations; this module owns the small,
deterministic domain model that decides whether those observations are enough
to begin implementation.  It intentionally has no browser, provider, or
filesystem imports so it can be used by checkpoint, CLI, and test code without
initialising optional integrations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum

__all__ = [
    "DEFAULT_VIEWPORTS",
    "CoverageCell",
    "CoverageMatrix",
    "CoverageStatus",
    "FidelityBaseline",
    "ObservationReceipt",
    "ResearchGate",
    "ResearchGateDecision",
    "ResearchProfilePolicy",
    "ResearchValidationError",
    "ViewportSpec",
    "build_coverage_matrix",
    "profile_policy",
]


class ResearchValidationError(ValueError):
    """Raised when a research contract is malformed or incomplete."""


class CoverageStatus(StrEnum):
    """Lifecycle states for one required observation cell."""

    PENDING = "pending"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"
    WAIVED = "waived"
    NOT_APPLICABLE = "not_applicable"
    STALE = "stale"
    CONTRADICTORY = "contradictory"


# These are stable opaque IDs, not filesystem components. Routes commonly
# begin with ``/`` and interaction names may contain spaces, so reject control
# characters rather than applying a path-oriented allow-list.
_SAFE_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


def _text(value: object, field_name: str, *, required: bool = True) -> str:
    result = str(value).strip()
    if required and not result:
        raise ResearchValidationError(f"{field_name} must not be empty")
    return result


def _tuple_strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ResearchValidationError(f"{field_name} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ResearchValidationError(f"{field_name} must be a list of strings")
    result = tuple(_text(item, field_name) for item in value)
    if len(set(result)) != len(result):
        raise ResearchValidationError(f"{field_name} must not contain duplicates")
    return result


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclasses.dataclass(frozen=True, slots=True)
class ViewportSpec:
    """Deterministic browser environment for one observation."""

    viewport_id: str
    width: int
    height: int
    device_scale: float = 1.0
    orientation: str = "portrait"
    reduced_motion: bool = False
    color_scheme: str = "light"
    touch: bool = False

    def __post_init__(self) -> None:
        viewport_id = _text(self.viewport_id, "viewport_id")
        if not _SAFE_ID.fullmatch(viewport_id):
            raise ResearchValidationError(f"invalid viewport_id: {viewport_id!r}")
        if self.width < 1 or self.height < 1:
            raise ResearchValidationError("viewport dimensions must be positive")
        if self.device_scale <= 0:
            raise ResearchValidationError("device_scale must be positive")
        if self.orientation not in {"portrait", "landscape"}:
            raise ResearchValidationError("orientation must be portrait or landscape")
        if self.color_scheme not in {"light", "dark", "no-preference"}:
            raise ResearchValidationError("invalid color_scheme")

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ViewportSpec":
        try:
            return cls(
                viewport_id=_text(value["viewport_id"], "viewport_id"),
                width=int(str(value["width"])),
                height=int(str(value["height"])),
                device_scale=float(str(value.get("device_scale", 1.0))),
                orientation=_text(value.get("orientation", "portrait"), "orientation"),
                reduced_motion=bool(value.get("reduced_motion", False)),
                color_scheme=_text(value.get("color_scheme", "light"), "color_scheme"),
                touch=bool(value.get("touch", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchValidationError("malformed viewport specification") from exc


DEFAULT_VIEWPORTS: tuple[ViewportSpec, ...] = (
    ViewportSpec("mobile", 390, 844, touch=True),
    ViewportSpec("tablet", 768, 1024, touch=True),
    ViewportSpec("desktop", 1440, 900),
)


@dataclasses.dataclass(frozen=True, slots=True)
class ResearchProfilePolicy:
    """Coverage rules selected by a reconstruct profile."""

    profile: str
    require_screenshot: bool
    require_measurement: bool
    require_interaction: bool
    require_responsive: bool
    allow_degraded: bool = False

    def required_artifacts(self) -> tuple[str, ...]:
        required = []
        if self.require_screenshot:
            required.append("screenshot")
        if self.require_measurement:
            required.append("measurement")
        if self.require_interaction:
            required.append("interaction_trace")
        if self.require_responsive:
            required.append("responsive_observation")
        return tuple(required)


def profile_policy(profile: str) -> ResearchProfilePolicy:
    """Return the deterministic research policy for *profile*."""
    normalized = _text(profile, "profile").lower().replace("-", "_")
    if normalized == "static":
        return ResearchProfilePolicy(normalized, True, True, False, True)
    if normalized == "application":
        return ResearchProfilePolicy(normalized, True, True, True, True)
    if normalized == "production":
        return ResearchProfilePolicy(normalized, True, True, True, True)
    if normalized == "custom":
        return ResearchProfilePolicy(normalized, True, True, True, True)
    raise ResearchValidationError(f"unknown research profile {profile!r}")


@dataclasses.dataclass(frozen=True, slots=True)
class CoverageCell:
    """One route/viewport/state/interaction observation obligation."""

    cell_id: str
    surface_id: str
    viewport_id: str
    visual_state: str
    interaction_cluster: str
    role: str = "reference"
    status: str = CoverageStatus.PENDING.value
    required_artifacts: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    observation_receipt_id: str = ""
    limitations: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    observed_at: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "cell_id",
            "surface_id",
            "viewport_id",
            "visual_state",
            "interaction_cluster",
        ):
            value = _text(getattr(self, field_name), field_name)
            if not _SAFE_ID.fullmatch(value):
                raise ResearchValidationError(f"invalid {field_name}: {value!r}")
        if self.role not in {"reference", "implementation"}:
            raise ResearchValidationError(f"invalid coverage role {self.role!r}")
        if self.status not in {item.value for item in CoverageStatus}:
            raise ResearchValidationError(f"invalid coverage status {self.status!r}")
        _tuple_strings(list(self.required_artifacts), "required_artifacts")
        _tuple_strings(list(self.artifact_ids), "artifact_ids")
        _tuple_strings(list(self.limitations), "limitations")
        _tuple_strings(list(self.conflict_ids), "conflict_ids")
        if self.observed_at < 0:
            raise ResearchValidationError("observed_at cannot be negative")
        if self.status == CoverageStatus.COMPLETE.value:
            missing = set(self.required_artifacts).difference(self.artifact_kinds())
            if missing:
                raise ResearchValidationError(
                    f"complete cell {self.cell_id!r} is missing artifacts: {sorted(missing)}"
                )

    def artifact_kinds(self) -> frozenset[str]:
        """Return the kinds represented by the cell's artifact IDs.

        IDs are intentionally opaque.  A caller records the kind prefix in the
        ID (for example ``screenshot:<digest>``); bare IDs are treated as
        generic evidence and do not satisfy a typed requirement.
        """
        return frozenset(
            item.split(":", 1)[0]
            for item in self.artifact_ids
            if ":" in item and item.split(":", 1)[0]
        )

    def with_evidence(
        self,
        *,
        artifact_ids: Iterable[str] = (),
        receipt_id: str = "",
        status: str | None = None,
        limitations: Iterable[str] = (),
    ) -> "CoverageCell":
        merged_artifacts = tuple(dict.fromkeys((*self.artifact_ids, *artifact_ids)))
        merged_limitations = tuple(dict.fromkeys((*self.limitations, *limitations)))
        next_status = status or self.status
        merged_kinds = frozenset(
            item.split(":", 1)[0]
            for item in merged_artifacts
            if ":" in item and item.split(":", 1)[0]
        )
        if status is None and set(self.required_artifacts).issubset(merged_kinds):
            next_status = CoverageStatus.COMPLETE.value
        return dataclasses.replace(
            self,
            status=next_status,
            artifact_ids=merged_artifacts,
            observation_receipt_id=receipt_id or self.observation_receipt_id,
            limitations=merged_limitations,
            observed_at=time.time(),
        )

    def to_dict(self, *, include_volatile: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "cell_id": self.cell_id,
            "surface_id": self.surface_id,
            "viewport_id": self.viewport_id,
            "visual_state": self.visual_state,
            "interaction_cluster": self.interaction_cluster,
            "role": self.role,
            "status": self.status,
            "required_artifacts": list(self.required_artifacts),
            "artifact_ids": list(self.artifact_ids),
            "observation_receipt_id": self.observation_receipt_id,
            "limitations": list(self.limitations),
            "conflict_ids": list(self.conflict_ids),
        }
        if include_volatile:
            value["observed_at"] = self.observed_at
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CoverageCell":
        try:
            return cls(
                cell_id=_text(value["cell_id"], "cell_id"),
                surface_id=_text(value["surface_id"], "surface_id"),
                viewport_id=_text(value["viewport_id"], "viewport_id"),
                visual_state=_text(value["visual_state"], "visual_state"),
                interaction_cluster=_text(value["interaction_cluster"], "interaction_cluster"),
                role=_text(value.get("role", "reference"), "role"),
                status=_text(value.get("status", CoverageStatus.PENDING.value), "status"),
                required_artifacts=_tuple_strings(
                    value.get("required_artifacts", []), "required_artifacts"
                ),
                artifact_ids=_tuple_strings(value.get("artifact_ids", []), "artifact_ids"),
                observation_receipt_id=_text(
                    value.get("observation_receipt_id", ""),
                    "observation_receipt_id",
                    required=False,
                ),
                limitations=_tuple_strings(value.get("limitations", []), "limitations"),
                conflict_ids=_tuple_strings(value.get("conflict_ids", []), "conflict_ids"),
                observed_at=float(str(value.get("observed_at", 0.0))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ResearchValidationError):
                raise
            raise ResearchValidationError("malformed coverage cell") from exc


@dataclasses.dataclass
class CoverageMatrix:
    """Revisioned collection of independent research obligations."""

    profile: str
    cells: dict[str, CoverageCell] = dataclasses.field(default_factory=dict)
    revision: int = 0

    def __post_init__(self) -> None:
        self.profile = profile_policy(self.profile).profile
        if self.revision < 0:
            raise ResearchValidationError("coverage revision cannot be negative")
        if len(self.cells) != len(set(self.cells)):
            raise ResearchValidationError("coverage contains duplicate cells")
        for key, cell in self.cells.items():
            if key != cell.cell_id:
                raise ResearchValidationError("coverage key does not match cell_id")

    @property
    def total(self) -> int:
        return len(self.cells)

    def counts(self) -> dict[str, int]:
        result = {status.value: 0 for status in CoverageStatus}
        for cell in self.cells.values():
            result[cell.status] = result.get(cell.status, 0) + 1
        return result

    def blocking_cells(self, *, allow_degraded: bool = False) -> tuple[str, ...]:
        allowed = {
            CoverageStatus.COMPLETE.value,
            CoverageStatus.WAIVED.value,
            CoverageStatus.NOT_APPLICABLE.value,
        }
        if allow_degraded:
            allowed.add(CoverageStatus.UNAVAILABLE.value)
        return tuple(
            sorted(cell.cell_id for cell in self.cells.values() if cell.status not in allowed)
        )

    def upsert(self, cell: CoverageCell) -> bool:
        """Store *cell* and increment the revision only when it changed."""
        previous = self.cells.get(cell.cell_id)
        if previous == cell:
            return False
        self.cells[cell.cell_id] = cell
        self.revision += 1
        return True

    def record(
        self,
        cell_id: str,
        *,
        artifact_ids: Iterable[str] = (),
        receipt_id: str = "",
        status: str | None = None,
        limitations: Iterable[str] = (),
    ) -> CoverageCell:
        try:
            current = self.cells[cell_id]
        except KeyError as exc:
            raise ResearchValidationError(f"unknown coverage cell {cell_id!r}") from exc
        updated = current.with_evidence(
            artifact_ids=artifact_ids,
            receipt_id=receipt_id,
            status=status,
            limitations=limitations,
        )
        self.upsert(updated)
        return updated

    def mark_complete(
        self, cell_id: str, *, artifact_ids: Iterable[str], receipt_id: str = ""
    ) -> CoverageCell:
        """Mark a cell complete after validating all typed evidence IDs."""
        return self.record(
            cell_id,
            artifact_ids=artifact_ids,
            receipt_id=receipt_id,
            status=CoverageStatus.COMPLETE.value,
        )

    def mark_unavailable(self, cell_id: str, reason: str) -> CoverageCell:
        """Record a blocked observation without pretending it succeeded."""
        return self.record(
            cell_id,
            status=CoverageStatus.UNAVAILABLE.value,
            limitations=(reason,),
        )

    def waive(self, cell_id: str, reason: str) -> CoverageCell:
        """Record an explicit user-approved scope exception."""
        return self.record(cell_id, status=CoverageStatus.WAIVED.value, limitations=(reason,))

    def mark_not_applicable(self, cell_id: str, reason: str) -> CoverageCell:
        """Record a deliberate, reasoned exclusion from the research scope."""
        return self.record(
            cell_id,
            status=CoverageStatus.NOT_APPLICABLE.value,
            limitations=(reason,),
        )

    def mark_stale(self, cell_ids: Iterable[str], reason: str) -> None:
        reason_text = _text(reason, "reason")
        for cell_id in cell_ids:
            if cell_id not in self.cells:
                raise ResearchValidationError(f"unknown coverage cell {cell_id!r}")
            self.record(cell_id, status=CoverageStatus.STALE.value, limitations=(reason_text,))

    def to_dict(self, *, include_volatile: bool = True) -> dict[str, object]:
        return {
            "profile": self.profile,
            "revision": self.revision,
            "cells": [
                self.cells[key].to_dict(include_volatile=include_volatile)
                for key in sorted(self.cells)
            ],
            "counts": self.counts(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CoverageMatrix":
        raw_cells = value.get("cells", [])
        if not isinstance(raw_cells, list):
            raise ResearchValidationError("coverage cells must be a list")
        cells: dict[str, CoverageCell] = {}
        for raw in raw_cells:
            if not isinstance(raw, Mapping):
                raise ResearchValidationError("coverage contains a non-object cell")
            cell = CoverageCell.from_dict(raw)
            if cell.cell_id in cells:
                raise ResearchValidationError(f"duplicate coverage cell {cell.cell_id!r}")
            cells[cell.cell_id] = cell
        try:
            revision = int(str(value.get("revision", 0)))
        except (TypeError, ValueError) as exc:
            raise ResearchValidationError("coverage revision is invalid") from exc
        return cls(_text(value.get("profile", ""), "profile"), cells, revision)

    def compact_digest(self, *, max_blocking: int = 50) -> dict[str, object]:
        """Return bounded state suitable for a dynamic prompt/checkpoint."""
        blocking = self.blocking_cells()
        return {
            "profile": self.profile,
            "revision": self.revision,
            "total": self.total,
            "counts": self.counts(),
            "blocking_cell_ids": list(blocking[:max_blocking]),
            "blocking_truncated": len(blocking) > max_blocking,
        }


def _surface_values(
    surfaces: Sequence[Mapping[str, object]],
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    result: list[tuple[str, Mapping[str, object]]] = []
    seen: set[str] = set()
    for item in surfaces:
        route = _text(item.get("surface_id", item.get("route", "")), "surface_id")
        if route in seen:
            continue
        seen.add(route)
        result.append((route, item))
    if not result:
        raise ResearchValidationError("at least one route/surface is required")
    return tuple(result)


def _strings_from_mapping(
    item: Mapping[str, object], keys: tuple[str, ...], fallback: str
) -> tuple[str, ...]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (list, tuple)) and value:
            result: list[str] = []
            for entry in value:
                if isinstance(entry, Mapping):
                    entry_value = entry.get("id", entry.get("name", entry.get("interaction", "")))
                else:
                    entry_value = entry
                if str(entry_value).strip():
                    result.append(_text(entry_value, key))
            if result:
                return tuple(dict.fromkeys(result))
    return (fallback,)


def build_coverage_matrix(
    profile: str,
    surfaces: Sequence[Mapping[str, object]],
    viewports: Sequence[ViewportSpec] = DEFAULT_VIEWPORTS,
) -> CoverageMatrix:
    """Expand route observations into a deterministic coverage matrix."""
    policy = profile_policy(profile)
    if not viewports:
        raise ResearchValidationError("at least one viewport is required")
    matrix = CoverageMatrix(policy.profile)
    for surface_id, item in _surface_values(surfaces):
        states = _strings_from_mapping(item, ("visual_states", "states"), "loaded")
        clusters = _strings_from_mapping(
            item,
            ("interaction_clusters", "interactions"),
            "page",
        )
        for viewport in viewports:
            for state in states:
                for cluster in clusters:
                    cell_id = "|".join(
                        (surface_id, viewport.viewport_id, state, cluster, "reference")
                    )
                    route_status = (
                        str(item.get("coverage_status", item.get("status", "observed")))
                        .strip()
                        .lower()
                    )
                    if route_status not in {
                        "observed",
                        "discovered_not_observed",
                        "unavailable",
                        "excluded",
                        CoverageStatus.NOT_APPLICABLE.value,
                    }:
                        raise ResearchValidationError(
                            f"invalid route coverage_status {route_status!r} for {surface_id!r}"
                        )
                    if route_status in {"excluded", CoverageStatus.NOT_APPLICABLE.value}:
                        status = CoverageStatus.NOT_APPLICABLE.value
                    elif route_status == CoverageStatus.UNAVAILABLE.value:
                        status = CoverageStatus.UNAVAILABLE.value
                    else:
                        status = CoverageStatus.PENDING.value
                    reason = str(item.get("reason", item.get("limitations", ""))).strip()
                    matrix.upsert(
                        CoverageCell(
                            cell_id=cell_id,
                            surface_id=surface_id,
                            viewport_id=viewport.viewport_id,
                            visual_state=state,
                            interaction_cluster=cluster,
                            status=status,
                            required_artifacts=policy.required_artifacts(),
                            limitations=(reason,)
                            if status != CoverageStatus.PENDING.value and reason
                            else (),
                        )
                    )
    return matrix


@dataclasses.dataclass(frozen=True, slots=True)
class ObservationReceipt:
    """Provenance for one browser or structured research observation."""

    receipt_id: str
    cell_id: str
    phase: str
    status: str
    artifact_ids: tuple[str, ...] = ()
    source_revision: str = ""
    measured: bool = False
    limitations: tuple[str, ...] = ()
    observed_at: float = dataclasses.field(default_factory=time.time)

    def __post_init__(self) -> None:
        _text(self.receipt_id, "receipt_id")
        _text(self.cell_id, "cell_id")
        _text(self.phase, "phase")
        if self.status not in {item.value for item in CoverageStatus}:
            raise ResearchValidationError(f"invalid receipt status {self.status!r}")
        _tuple_strings(list(self.artifact_ids), "artifact_ids")
        _tuple_strings(list(self.limitations), "limitations")
        if self.observed_at < 0:
            raise ResearchValidationError("observed_at cannot be negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "cell_id": self.cell_id,
            "phase": self.phase,
            "status": self.status,
            "artifact_ids": list(self.artifact_ids),
            "source_revision": self.source_revision,
            "measured": self.measured,
            "limitations": list(self.limitations),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ObservationReceipt":
        try:
            return cls(
                receipt_id=_text(value.get("receipt_id", ""), "receipt_id"),
                cell_id=_text(value.get("cell_id", ""), "cell_id"),
                phase=_text(value.get("phase", ""), "phase"),
                status=_text(value.get("status", ""), "status"),
                artifact_ids=_tuple_strings(value.get("artifact_ids", []), "artifact_ids"),
                source_revision=_text(
                    value.get("source_revision", ""), "source_revision", required=False
                ),
                measured=bool(value.get("measured", False)),
                limitations=_tuple_strings(value.get("limitations", []), "limitations"),
                observed_at=float(str(value.get("observed_at", 0.0))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ResearchValidationError):
                raise
            raise ResearchValidationError("malformed observation receipt") from exc


@dataclasses.dataclass(frozen=True, slots=True)
class InteractionTrace:
    """An observable user action and its resulting state transition."""

    trace_id: str
    cell_id: str
    precondition: str
    action: Mapping[str, object]
    visible_outcome: str
    sequence: tuple[str, ...] = ()
    navigation_effect: Mapping[str, object] = dataclasses.field(default_factory=dict)
    data_effect: str = "unknown"
    focus_effect: str = ""
    screenshot_ids: tuple[str, ...] = ()
    status: str = CoverageStatus.COMPLETE.value

    def __post_init__(self) -> None:
        _text(self.trace_id, "trace_id")
        _text(self.cell_id, "cell_id")
        _text(self.precondition, "precondition")
        if not self.action:
            raise ResearchValidationError("interaction action must not be empty")
        _text(self.visible_outcome, "visible_outcome")
        if self.status not in {item.value for item in CoverageStatus}:
            raise ResearchValidationError(f"invalid interaction status {self.status!r}")
        _tuple_strings(list(self.sequence), "sequence")
        _tuple_strings(list(self.screenshot_ids), "screenshot_ids")

    def to_dict(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "cell_id": self.cell_id,
            "precondition": self.precondition,
            "action": dict(self.action),
            "sequence": list(self.sequence),
            "visible_outcome": self.visible_outcome,
            "navigation_effect": dict(self.navigation_effect),
            "data_effect": self.data_effect,
            "focus_effect": self.focus_effect,
            "screenshot_ids": list(self.screenshot_ids),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "InteractionTrace":
        action = value.get("action", {})
        navigation = value.get("navigation_effect", {})
        if not isinstance(action, Mapping) or not isinstance(navigation, Mapping):
            raise ResearchValidationError("interaction action/navigation must be objects")
        return cls(
            trace_id=_text(value.get("trace_id", ""), "trace_id"),
            cell_id=_text(value.get("cell_id", ""), "cell_id"),
            precondition=_text(value.get("precondition", ""), "precondition"),
            action=action,
            sequence=_tuple_strings(value.get("sequence", []), "sequence"),
            visible_outcome=_text(value.get("visible_outcome", ""), "visible_outcome"),
            navigation_effect=navigation,
            data_effect=_text(value.get("data_effect", "unknown"), "data_effect"),
            focus_effect=_text(value.get("focus_effect", ""), "focus_effect", required=False),
            screenshot_ids=_tuple_strings(value.get("screenshot_ids", []), "screenshot_ids"),
            status=_text(value.get("status", CoverageStatus.COMPLETE.value), "status"),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ResearchGateDecision:
    """Validated result of a research-gate evaluation."""

    status: str
    summary: str
    blocking_cell_ids: tuple[str, ...]
    exception_ids: tuple[str, ...] = ()
    rationale: str = ""
    baseline_artifact_id: str = ""

    @property
    def approved(self) -> bool:
        return self.status in {"approved", "approved_degraded"}

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "summary": self.summary,
            "blocking_cell_ids": list(self.blocking_cell_ids),
            "exception_ids": list(self.exception_ids),
            "rationale": self.rationale,
            "baseline_artifact_id": self.baseline_artifact_id,
            "approved": self.approved,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class FidelityBaseline:
    """Normalized research handoff consumed by implementation phases."""

    profile: str
    scope: Mapping[str, object]
    route_inventory: tuple[Mapping[str, object], ...]
    viewports: tuple[ViewportSpec, ...]
    coverage: CoverageMatrix
    artifact_ids: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    exceptions: tuple[Mapping[str, object], ...] = ()
    tolerances: Mapping[str, object] = dataclasses.field(
        default_factory=lambda: {
            "geometry_css_px": 1,
            "relative_geometry": 0.01,
            "colors": "exact_when_same_environment",
        }
    )
    source_revision: str = ""
    manifest_revision: int = 0

    def __post_init__(self) -> None:
        if self.profile != self.coverage.profile:
            raise ResearchValidationError("baseline profile differs from coverage profile")
        if not self.route_inventory:
            raise ResearchValidationError("baseline route inventory must not be empty")
        if not self.viewports:
            raise ResearchValidationError("baseline viewport matrix must not be empty")
        _tuple_strings(list(self.artifact_ids), "artifact_ids")
        _tuple_strings(list(self.unresolved_questions), "unresolved_questions")
        if self.manifest_revision < 0:
            raise ResearchValidationError("manifest_revision cannot be negative")

    def normalized_dict(self) -> dict[str, object]:
        """Return deterministic baseline data without volatile timestamps/IDs."""
        normalized_coverage = self.coverage.to_dict(include_volatile=False)
        # The revision is an operational mutation counter, not observed truth;
        # identical evidence must hash identically after an idempotent replay.
        normalized_coverage["revision"] = 0
        return {
            "profile": self.profile,
            "scope": dict(self.scope),
            "route_inventory": [dict(item) for item in self.route_inventory],
            "viewports": [item.to_dict() for item in self.viewports],
            "coverage": normalized_coverage,
            "artifact_ids": list(self.artifact_ids),
            "unresolved_questions": list(self.unresolved_questions),
            "exceptions": [dict(item) for item in self.exceptions],
            "tolerances": dict(self.tolerances),
            "source_revision": self.source_revision,
        }

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(_canonical(self.normalized_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        value = self.normalized_dict()
        value["baseline_hash"] = self.content_hash
        value["manifest_revision"] = self.manifest_revision
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FidelityBaseline":
        raw_routes = value.get("route_inventory", [])
        raw_viewports = value.get("viewports", [])
        raw_exceptions = value.get("exceptions", [])
        if not isinstance(raw_routes, list) or not all(
            isinstance(item, Mapping) for item in raw_routes
        ):
            raise ResearchValidationError("baseline route_inventory is malformed")
        if not isinstance(raw_viewports, list) or not all(
            isinstance(item, Mapping) for item in raw_viewports
        ):
            raise ResearchValidationError("baseline viewports are malformed")
        if not isinstance(raw_exceptions, list) or not all(
            isinstance(item, Mapping) for item in raw_exceptions
        ):
            raise ResearchValidationError("baseline exceptions are malformed")
        raw_coverage = value.get("coverage")
        if not isinstance(raw_coverage, Mapping):
            raise ResearchValidationError("baseline coverage is malformed")
        scope = value.get("scope", {})
        tolerances = value.get("tolerances", {})
        if not isinstance(scope, Mapping) or not isinstance(tolerances, Mapping):
            raise ResearchValidationError("baseline scope and tolerances are malformed")
        try:
            manifest_revision = int(str(value.get("manifest_revision", 0)))
        except (TypeError, ValueError) as exc:
            raise ResearchValidationError("baseline manifest_revision is invalid") from exc
        baseline = cls(
            profile=_text(value.get("profile", ""), "profile"),
            scope=scope,
            route_inventory=tuple(item for item in raw_routes if isinstance(item, Mapping)),
            viewports=tuple(ViewportSpec.from_dict(item) for item in raw_viewports),
            coverage=CoverageMatrix.from_dict(raw_coverage),
            artifact_ids=_tuple_strings(value.get("artifact_ids", []), "artifact_ids"),
            unresolved_questions=_tuple_strings(
                value.get("unresolved_questions", []), "unresolved_questions"
            ),
            exceptions=tuple(item for item in raw_exceptions if isinstance(item, Mapping)),
            tolerances=tolerances,
            source_revision=_text(
                value.get("source_revision", ""), "source_revision", required=False
            ),
            manifest_revision=manifest_revision,
        )
        supplied_hash = str(value.get("baseline_hash", ""))
        if supplied_hash and supplied_hash != baseline.content_hash:
            raise ResearchValidationError("baseline hash does not match normalized content")
        return baseline


class ResearchGate:
    """Evaluate and validate explicit research-gate decisions."""

    def __init__(self, policy: ResearchProfilePolicy | str) -> None:
        self.policy = profile_policy(policy) if isinstance(policy, str) else policy

    def evaluate(
        self,
        matrix: CoverageMatrix,
        *,
        exceptions: Sequence[Mapping[str, object]] = (),
    ) -> ResearchGateDecision:
        if matrix.profile != self.policy.profile:
            raise ResearchValidationError("gate policy and coverage profile differ")
        blocking = matrix.blocking_cells(allow_degraded=self.policy.allow_degraded)
        exception_ids_list: list[str] = []
        for item in exceptions:
            if not isinstance(item, Mapping):
                raise ResearchValidationError("research exceptions must be objects")
            exception_ids_list.append(_text(item.get("exception_id", ""), "exception_id"))
        exception_ids = tuple(exception_ids_list)
        status = "ready" if not blocking else "blocked"
        summary = (
            "Research coverage is complete."
            if not blocking
            else f"Research coverage is incomplete for {len(blocking)} cell(s)."
        )
        return ResearchGateDecision(status, summary, blocking, exception_ids)

    def approve(
        self,
        matrix: CoverageMatrix,
        *,
        baseline_artifact_id: str,
        summary: str,
    ) -> ResearchGateDecision:
        decision = self.evaluate(matrix)
        if decision.blocking_cell_ids:
            raise ResearchValidationError(
                "cannot approve incomplete research: " + ", ".join(decision.blocking_cell_ids[:10])
            )
        return dataclasses.replace(
            decision,
            status="approved",
            summary=_text(summary, "summary"),
            baseline_artifact_id=_text(baseline_artifact_id, "baseline_artifact_id"),
        )

    def approve_degraded(
        self,
        matrix: CoverageMatrix,
        *,
        exception_ids: Sequence[str],
        rationale: str,
    ) -> ResearchGateDecision:
        provided = tuple(_text(item, "exception_id") for item in exception_ids)
        if not provided:
            raise ResearchValidationError("degraded approval requires exception IDs")
        strict_blocking = matrix.blocking_cells()
        non_degraded = tuple(
            cell_id
            for cell_id in strict_blocking
            if matrix.cells[cell_id].status != CoverageStatus.UNAVAILABLE.value
        )
        if non_degraded:
            raise ResearchValidationError(
                "degraded approval cannot bypass pending, stale, or contradictory cells"
            )
        unavailable = {
            cell_id
            for cell_id in strict_blocking
            if matrix.cells[cell_id].status == CoverageStatus.UNAVAILABLE.value
        }
        unknown = set(provided).difference(unavailable)
        missing = unavailable.difference(provided)
        if unknown or missing:
            detail = []
            if unknown:
                detail.append(f"unknown exceptions: {sorted(unknown)}")
            if missing:
                detail.append(f"unlisted unavailable cells: {sorted(missing)}")
            raise ResearchValidationError(
                "degraded approval must name every unavailable cell exactly ("
                + "; ".join(detail)
                + ")"
            )
        if not rationale.strip():
            raise ResearchValidationError("degraded approval requires a rationale")
        return ResearchGateDecision(
            "approved_degraded",
            "Research approved with explicit degraded exceptions.",
            tuple(
                cell_id
                for cell_id in strict_blocking
                if matrix.cells[cell_id].status == CoverageStatus.UNAVAILABLE.value
            ),
            provided,
            rationale.strip(),
        )
