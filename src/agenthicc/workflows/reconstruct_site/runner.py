"""Optimized compatibility implementation of ``reconstruct_site``.

The original runner remains importable for downstream code that imported its
private phase helpers.  The built-in registry points at this module, which
adds the PRD-177 execution boundary around those helpers: one dispatch path
for fresh and resumed runs, profile-aware progress, durable evidence, compact
checkpoint context, validated re-entry, and a deterministic screenshot-link
tool.  The actual browser/provider authority is still owned by the existing
session configuration and ``CodePlanRunner`` contracts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tools.base import ToolLike
from agenthicc.tools.sandbox import WorkspaceView
from agenthicc.workflows.phase_lifecycle import (
    PhaseAnnotation,
    checkpoint_phase_boundary,
    publish_phase_annotation,
    reconcile_phase_cursor,
)
from .evidence import (
    ArtifactRecord,
    EvidenceIntegrityError,
    ReconstructEvidenceStore,
)
from .evidence_plan import (
    LEGACY_PHASE_PLAN_VERSION,
    LEGACY_RECONSTRUCT_PHASE_PLAN,
    PHASE_PLAN_VERSION,
    ActiveReconstructPlan,
    PhasePlanError,
    ReconstructProfile,
    RECONSTRUCT_PHASE_PLAN,
)
from .phase_impl import (
    CACHE_CONTRACT,
    ReconstructContext as PhaseContext,
    ReconstructSiteParams as PhaseParams,
    ReconstructSiteRunner as PhaseRunner,
    ReconstructSiteWorkflow as PhaseWorkflow,
    ReconstructState,
)
from .phase_impl import (
    _make_accessibility_tools,
    _make_architecture_tools,
    _make_bootstrap_tools,
    _make_caddy_tools,
    _make_component_system_tools,
    _make_content_assets_tools,
    _make_data_layer_tools,
    _make_design_system_tools,
    _make_docs_tools,
    _make_docker_tools,
    _make_env_tools,
    _make_fidelity_pass_tools,
    _make_final_validation_tools,
    _make_global_shell_tools,
    _make_init_tools,
    _make_interaction_analysis_tools,
    _make_interaction_validation_tools,
    _make_netlify_tools,
    _make_page_tools,
    _make_package_tools,
    _make_performance_tools,
    _make_prisma_tools,
    _make_recon_tools,
    _make_research_gate_tools,
    _make_responsive_pass_tools,
    _make_responsive_research_tools,
    _make_scripts_tools,
    _make_sqlite_tools,
    _make_tanstack_tools,
    _make_verify_caddy_tools,
    _make_verify_docs_tools,
    _make_verify_docker_tools,
    _make_verify_env_tools,
    _make_verify_netlify_tools,
    _make_verify_package_tools,
    _make_verify_prisma_tools,
    _make_verify_scripts_tools,
    _make_verify_sqlite_tools,
    _make_verify_tanstack_tools,
    _make_visual_research_tools,
    _make_visual_validation_tools,
)
from agenthicc.workflows.plugin import WorkflowPlugin
from agenthicc.workflows.reconstruct_site.research import (
    DEFAULT_VIEWPORTS,
    CoverageMatrix,
    CoverageStatus,
    FidelityBaseline,
    ResearchGate,
    ObservationReceipt,
    ResearchValidationError,
    ViewportSpec,
    build_coverage_matrix,
)

if TYPE_CHECKING:
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

__all__ = [
    "CACHE_CONTRACT",
    "PHASE_PLAN_VERSION",
    "ReconstructContext",
    "ReconstructSiteParams",
    "ReconstructSiteRunner",
    "ReconstructSiteWorkflow",
    "ReconstructState",
    "CoverageMatrix",
    "FidelityBaseline",
    "ResearchGate",
    "ResearchValidationError",
    "build_coverage_matrix",
    "_make_accessibility_tools",
    "_make_architecture_tools",
    "_make_bootstrap_tools",
    "_make_caddy_tools",
    "_make_component_system_tools",
    "_make_content_assets_tools",
    "_make_data_layer_tools",
    "_make_design_system_tools",
    "_make_docs_tools",
    "_make_docker_tools",
    "_make_env_tools",
    "_make_fidelity_pass_tools",
    "_make_final_validation_tools",
    "_make_global_shell_tools",
    "_make_init_tools",
    "_make_interaction_analysis_tools",
    "_make_interaction_validation_tools",
    "_make_netlify_tools",
    "_make_page_tools",
    "_make_package_tools",
    "_make_performance_tools",
    "_make_prisma_tools",
    "_make_recon_tools",
    "_make_responsive_pass_tools",
    "_make_responsive_research_tools",
    "_make_scripts_tools",
    "_make_sqlite_tools",
    "_make_tanstack_tools",
    "_make_verify_caddy_tools",
    "_make_verify_docs_tools",
    "_make_verify_docker_tools",
    "_make_verify_env_tools",
    "_make_verify_netlify_tools",
    "_make_verify_package_tools",
    "_make_verify_prisma_tools",
    "_make_verify_scripts_tools",
    "_make_verify_sqlite_tools",
    "_make_verify_tanstack_tools",
    "_make_visual_research_tools",
    "_make_visual_validation_tools",
]


@dataclasses.dataclass
class ReconstructContext(PhaseContext):
    """Phase context plus compact evidence and profile state."""

    plan_version: str = PHASE_PLAN_VERSION
    profile: str = ReconstructProfile.STATIC.value
    phase_attempt: int = 0
    phase_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    artifact_manifest_path: str = ""
    artifact_manifest_revision: int = 0
    required_artifact_ids: list[str] = dataclasses.field(default_factory=list)
    stale_artifact_ids: list[str] = dataclasses.field(default_factory=list)
    screenshot_ids: list[str] = dataclasses.field(default_factory=list)
    reentry_count: int = 0
    reentry_history: list[dict[str, object]] = dataclasses.field(default_factory=list)
    phase_digest: str = ""
    skipped_reasons: dict[str, str] = dataclasses.field(default_factory=dict)
    screenshot_status: str = "pending"
    research_receipt_ids: list[str] = dataclasses.field(default_factory=list)
    research_gate_decision: dict[str, object] = dataclasses.field(default_factory=dict)
    conversation_id: str = ""
    browser_backend: str = ""
    browser_capability_status: str = "unknown"
    research_cell_id: str = ""
    research_metrics: dict[str, int] = dataclasses.field(default_factory=dict)
    research_viewports: list[dict[str, object]] = dataclasses.field(default_factory=list)
    cache_epoch: int = 0
    cache_epoch_reason: str = "initial"
    stable_tool_bundle_fingerprint: str = ""
    # Resume reconciliation is durable diagnostic state, not prompt content.
    resume_resolution_source: str = ""
    resume_resolution_reason: str = ""
    resume_reconciled: bool = False

    @property
    def page_progress(self) -> dict[str, int]:
        """Return route progress separately from the static phase counter."""
        total = len(self.pages_to_implement)
        return {
            "completed": min(max(self.page_index, 0), total),
            "total": total,
            "current": min(self.page_index + 1, total) if total else 0,
        }


@dataclasses.dataclass
class ReconstructSiteParams(PhaseParams):
    """Typed PRD-177 controls in addition to phase model overrides."""

    profile: str = ""
    custom_phases: tuple[str, ...] = ()
    max_reentries: int = 3
    phase_models: dict[str, str] = dataclasses.field(default_factory=dict)
    viewports: tuple[ViewportSpec, ...] = DEFAULT_VIEWPORTS

    def get_phase_models(self) -> dict[str, str]:
        result = super().get_phase_models()
        result.update({str(key): str(value) for key, value in self.phase_models.items() if value})
        return result


class ReconstructSiteRunner(PhaseRunner):
    """Run the phase implementations through one authoritative plan."""

    workflow_name = "reconstruct_site"
    # This is a compatibility fallback for callers that inspect the class
    # before a profile is selected.  The active value is published from the
    # selected plan in ``_publish_phase``.
    total_phases = len(RECONSTRUCT_PHASE_PLAN.definitions)

    def __init__(self, config: "WorkflowConfig", mode_manager: "ModeManager | None" = None) -> None:
        super().__init__(config, mode_manager)
        RECONSTRUCT_PHASE_PLAN.validate(type(self))
        self._active_context: ReconstructContext | None = None
        self._active_plan: ActiveReconstructPlan | None = None
        self._evidence: ReconstructEvidenceStore | None = None
        self._pending_reentry: tuple[str, str, str] | None = None
        self._reconstruct_tool_cache: list[ToolLike] | None = None
        self._active_phase_name = ""

    def _params(self) -> ReconstructSiteParams:
        params = getattr(self._cfg, "params", None)
        return params if isinstance(params, ReconstructSiteParams) else ReconstructSiteParams()

    def _phase_model(self, phase_name: str) -> str:
        """Resolve a phase model even for minimal compatibility test configs."""
        params = getattr(self._cfg, "params", None)
        if params is not None:
            model_for_phase = getattr(params, "model_for_phase", None)
            if callable(model_for_phase):
                value = model_for_phase(phase_name, "")
                if value:
                    return str(value)
        return str(getattr(self, f"{phase_name}_model", "") or "")

    def invalidate_tool_bundle_cache(self, *, reason: str = "configuration_changed") -> None:
        """Invalidate both the inherited stable bundle and reconstruct tools."""
        super().invalidate_tool_bundle_cache(reason=reason)
        self._reconstruct_tool_cache = None

    def _select_profile(self, context: ReconstructContext) -> ActiveReconstructPlan:
        self._restore_persisted_plan_version(context)
        configured = self._params().profile.strip().lower()
        if context.artifact_manifest_path and context.profile:
            # A resumed run is bound to the profile recorded in its manifest;
            # changing configuration cannot silently alter its phase graph.
            profile = context.profile.strip().lower()
        elif configured:
            profile = configured
        elif not hasattr(self._cfg, "params"):
            # Very small downstream adapters historically supplied only cfg
            # and monkey-patched run_phase. Preserve their all-phases driver
            # semantics while real WorkflowConfig instances use the explicit
            # application default below.
            profile = ReconstructProfile.PRODUCTION.value
        elif "static=True" in context.artifacts.get("initial_state", ""):
            profile = ReconstructProfile.STATIC.value
        else:
            profile = ReconstructProfile.APPLICATION.value
        plan_source = (
            LEGACY_RECONSTRUCT_PHASE_PLAN
            if context.plan_version == LEGACY_PHASE_PLAN_VERSION
            else RECONSTRUCT_PHASE_PLAN
        )
        if context.plan_version not in {PHASE_PLAN_VERSION, LEGACY_PHASE_PLAN_VERSION}:
            raise PhasePlanError(
                f"unsupported reconstruct phase-plan version {context.plan_version!r}"
            )
        plan = plan_source.active(
            profile,
            self._params().custom_phases,
            require_research_gate=plan_source is RECONSTRUCT_PHASE_PLAN,
        )
        plan = dataclasses.replace(plan, version=context.plan_version)
        context.profile = plan.profile.value
        if not context.research_viewports:
            context.research_viewports = [item.to_dict() for item in self._params().viewports]
        context.plan_version = plan.version
        context.skipped_reasons = {item.name: item.reason for item in plan.skipped}
        context.skipped_phases = list(context.skipped_reasons)
        return plan

    def _restore_persisted_plan_version(self, context: ReconstructContext) -> None:
        """Recover a legacy plan version before selecting its phase graph.

        Older checkpoints did not carry the plan-version field, but did carry
        the evidence manifest path.  Reading only that manifest identity lets
        those runs resume against their original graph without importing or
        trusting any artifact body.
        """
        if not context.artifact_manifest_path or context.plan_version != PHASE_PLAN_VERSION:
            return
        try:
            path = self._workspace().resolve(context.artifact_manifest_path)
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        stored = raw.get("plan_version") if isinstance(raw, Mapping) else None
        if isinstance(stored, str) and stored.strip():
            context.plan_version = stored.strip()

    def _workspace(self) -> WorkspaceView:
        scope = getattr(self._cfg, "workspace_scope", None)
        root = scope.primary_root if scope is not None else Path.cwd()
        return WorkspaceView(root)

    def _ensure_evidence(self, context: ReconstructContext) -> ReconstructEvidenceStore:
        evidence = getattr(self, "_evidence", None)
        if evidence is None:
            if context.artifact_manifest_path:
                try:
                    if not self._workspace().resolve(context.artifact_manifest_path).is_file():
                        raise EvidenceIntegrityError(
                            "reconstruct evidence manifest is missing; resume is recoverable only "
                            "after restoring the recorded artifact directory"
                        )
                except (OSError, PermissionError) as exc:
                    raise EvidenceIntegrityError(
                        "reconstruct evidence manifest is unavailable"
                    ) from exc
            evidence = ReconstructEvidenceStore(
                self._workspace(),
                context.run_id,
                plan_version=context.plan_version,
                profile=context.profile,
            )
            self._evidence = evidence
            context.artifact_manifest_path = evidence.manifest_relative_path
            if evidence.manifest.profile != context.profile:
                evidence.set_metadata(profile=context.profile)
            elif evidence.manifest.revision == 0:
                evidence.set_metadata()
            if (
                getattr(self._cfg, "browser_manager", None) is None
                and context.screenshot_status == "pending"
            ):
                evidence.record_degraded_screenshot(
                    role="reference",
                    route="*",
                    viewport="all",
                    backend="unavailable",
                    reason="No browser integration is configured for this session.",
                )
                context.screenshot_status = "degraded"
            context.browser_capability_status = (
                "unavailable"
                if getattr(self._cfg, "browser_manager", None) is None
                else "available"
            )
            context.browser_backend = (
                "unavailable"
                if getattr(self._cfg, "browser_manager", None) is None
                else type(self._cfg.browser_manager).__name__
            )
        return evidence

    async def _init(  # type: ignore[override]
        self, context: ReconstructContext, memory: object
    ) -> ReconstructState:
        state = await super()._init(context, memory)
        self._active_plan = self._select_profile(context)
        self._ensure_evidence(context).set_skipped(self._active_plan_skips())
        return state

    async def _research_gate(  # type: ignore[override]
        self, context: ReconstructContext, memory: object
    ) -> ReconstructState:
        """Require validated baseline approval before entering bootstrap."""
        from agenthicc.workflows.reconstruct_site.phase_impl import _MAX_ATTEMPTS

        gate_started = time.monotonic()
        store = self._ensure_evidence(context)
        matrix = self._coverage(context)
        context.unresolved_research = list(matrix.blocking_cells())
        # Publishing before the first gate turn gives the agent and the user a
        # durable report to inspect. Repeated attempts are idempotent unless
        # research changed or a re-entry invalidated the baseline.
        self._publish_baseline(context, store, max(1, context.phase_attempt))
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event = asyncio.Event()
            data: dict[str, object] = {}
            matrix = self._coverage(context)
            context.unresolved_research = list(matrix.blocking_cells())
            report = json.dumps(matrix.compact_digest(), sort_keys=True)
            await self.run_phase(
                intent=context.intent,
                text=(
                    "Review the research coverage report and approve the current baseline, "
                    "approve explicit degraded exceptions, or reject with the earliest "
                    "phase that can resolve the findings.\n\n" + report
                    if attempt == 1
                    else "Call an appropriate research-gate transition tool now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the RESEARCH_GATE phase of reconstruct_site. This is a "
                    "hard boundary before implementation. Review every route, viewport, "
                    "visual state, interaction trace, responsive observation, asset, and "
                    "measurement in the coverage report. Do not write application code. "
                    "If complete, call approve_research_baseline(summary, "
                    "baseline_artifact_id). If only explicitly unavailable cells remain, "
                    "call approve_degraded_research(exception_ids, rationale, "
                    "baseline_artifact_id). Otherwise call "
                    "reject_research_baseline(findings, target_phase). Prose never "
                    "advances this gate."
                ),
                mode="Yolo",
                max_turns=15,
                shared_memory=memory,
                tools=_make_research_gate_tools(event, data),
            )
            if not event.is_set():
                continue
            action = str(data.get("action", "")).strip()
            baseline_id = str(data.get("baseline_artifact_id", "")).strip()
            if action in {"approve", "approve_degraded"}:
                if baseline_id != context.research_baseline_id:
                    context.fail_reason = "research approval referenced a stale baseline"
                    context.known_issues.append(
                        {
                            "phase": "research_gate",
                            "issue": context.fail_reason,
                            "severity": "high",
                        }
                    )
                    continue
                gate = ResearchGate(context.profile)
                try:
                    if action == "approve":
                        decision = gate.approve(
                            matrix,
                            baseline_artifact_id=baseline_id,
                            summary=str(data.get("summary", "")),
                        )
                    else:
                        raw_exceptions = data.get("exception_ids", [])
                        exception_ids = (
                            [str(item) for item in raw_exceptions if isinstance(item, str)]
                            if isinstance(raw_exceptions, list)
                            else []
                        )
                        decision = gate.approve_degraded(
                            matrix,
                            exception_ids=exception_ids,
                            rationale=str(data.get("rationale", "")),
                        )
                except ResearchValidationError as exc:
                    context.fail_reason = str(exc)
                    context.known_issues.append(
                        {
                            "phase": "research_gate",
                            "issue": str(exc),
                            "severity": "high",
                        }
                    )
                    continue
                decision = dataclasses.replace(decision, baseline_artifact_id=baseline_id)
                context.research_gate_status = decision.status
                context.research_gate_decision = decision.to_dict()
                context.research_exceptions = [
                    {"exception_id": item, "status": "accepted"} for item in decision.exception_ids
                ]
                context.unresolved_research = list(decision.blocking_cell_ids)
                context.last_transition = action
                context.research_metrics["gate_duration_ms"] = int(
                    (time.monotonic() - gate_started) * 1000
                )
                return ReconstructState.BOOTSTRAP
            if action == "reject":
                raw_findings = data.get("findings", [])
                if isinstance(raw_findings, list):
                    context.known_issues.extend(
                        {
                            "phase": "research_gate",
                            "issue": str(item),
                            "severity": "high",
                        }
                        for item in raw_findings
                    )
                target = str(data.get("target_phase", "")).strip()
                try:
                    context.research_gate_status = "rejected"
                    context.last_transition = "reject_research_baseline"
                    return self._reentry_state(target)
                except PhasePlanError as exc:
                    context.fail_reason = str(exc)
                    context.known_issues.append(
                        {
                            "phase": "research_gate",
                            "issue": str(exc),
                            "severity": "high",
                        }
                    )
                    continue
        context.fail_reason = "research_gate did not receive a valid gate decision"
        context.research_metrics["gate_duration_ms"] = int((time.monotonic() - gate_started) * 1000)
        return ReconstructState.FAILED

    def _coverage(self, context: ReconstructContext) -> CoverageMatrix:
        """Load the typed coverage matrix held in the resumable context."""
        if (
            context.coverage_matrix
            and isinstance(context.coverage_matrix.get("cells"), list)
            and context.coverage_matrix.get("cells")
        ):
            try:
                return CoverageMatrix.from_dict(context.coverage_matrix)
            except ResearchValidationError as exc:
                raise EvidenceIntegrityError("research coverage matrix is malformed") from exc
        if not context.route_inventory:
            raise ResearchValidationError("route inventory is required before research coverage")
        matrix = build_coverage_matrix(
            context.profile, context.route_inventory, self._viewports(context)
        )
        context.coverage_matrix = matrix.to_dict()
        return matrix

    def _viewports(self, context: ReconstructContext) -> tuple[ViewportSpec, ...]:
        """Return the run-pinned viewport matrix, validating old checkpoints."""
        if context.research_viewports:
            try:
                viewports = tuple(
                    ViewportSpec.from_dict(item) for item in context.research_viewports
                )
            except ResearchValidationError as exc:
                raise EvidenceIntegrityError("research viewport matrix is malformed") from exc
        else:
            viewports = self._params().viewports
        if not viewports:
            raise ResearchValidationError("at least one research viewport is required")
        return viewports

    @staticmethod
    def _observation_cell_ids(
        matrix: CoverageMatrix,
        observation: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Resolve an observation to cells without guessing across surfaces."""
        explicit = str(observation.get("cell_id", "")).strip()
        if explicit:
            return (explicit,) if explicit in matrix.cells else ()
        surface = str(observation.get("surface_id", observation.get("route", ""))).strip()
        viewport = str(observation.get("viewport_id", observation.get("viewport", ""))).strip()
        state = str(observation.get("visual_state", observation.get("state", "loaded"))).strip()
        cluster = str(
            observation.get("interaction_cluster", observation.get("interaction", ""))
        ).strip()
        result: list[str] = []
        for cell in matrix.cells.values():
            if surface and cell.surface_id != surface:
                continue
            if viewport and cell.viewport_id != viewport:
                continue
            if state and cell.visual_state != state:
                continue
            if cluster and cluster != "page" and cell.interaction_cluster != cluster:
                continue
            result.append(cell.cell_id)
        return tuple(result)

    def _source_cells_for_phase(
        self, context: ReconstructContext, phase_name: str
    ) -> tuple[str, ...]:
        """Return the explicit coverage links declared by a research phase."""
        if phase_name == "visual_research":
            observations: Sequence[Mapping[str, object]] = context.visual_observations
        elif phase_name == "interaction_analysis":
            observations = context.interaction_traces
        elif phase_name == "responsive_research":
            observations = context.responsive_inventory
        else:
            return ()
        try:
            matrix = self._coverage(context)
        except ResearchValidationError:
            return ()
        cells = {
            cell_id
            for observation in observations
            for cell_id in self._observation_cell_ids(matrix, observation)
        }
        return tuple(sorted(cells))

    @staticmethod
    def _observation_status(observation: Mapping[str, object], current: str) -> str | None:
        """Normalize an optional observation status without hiding bad input."""
        raw = str(observation.get("status", "")).strip().lower()
        if not raw:
            return None
        valid = {item.value for item in CoverageStatus}
        if raw not in valid:
            raise ResearchValidationError(f"invalid observation status {raw!r}")
        # Completion is derived from typed evidence IDs below. A model cannot
        # turn a pending or unavailable observation into a success merely by
        # labelling its payload ``complete``.
        if raw == CoverageStatus.COMPLETE.value:
            return None
        return raw

    def _record_observation_receipt(
        self,
        context: ReconstructContext,
        matrix: CoverageMatrix,
        *,
        phase_name: str,
        artifact_id: str,
        cell_id: str,
        observation: Mapping[str, object],
        evidence_ids: tuple[str, ...],
        measured: bool,
    ) -> None:
        """Persist a deterministic receipt and link it back to one cell."""
        store = self._ensure_evidence(context)
        current = matrix.cells[cell_id]
        status = self._observation_status(observation, current.status)
        limitations: tuple[str, ...] = ()
        if status in {
            CoverageStatus.UNAVAILABLE.value,
            CoverageStatus.WAIVED.value,
            CoverageStatus.NOT_APPLICABLE.value,
            CoverageStatus.STALE.value,
            CoverageStatus.CONTRADICTORY.value,
        }:
            reason = str(observation.get("reason", "observation was not complete")).strip()
            limitations = (reason or "observation was not complete",)
        matrix.record(
            cell_id,
            artifact_ids=evidence_ids,
            status=status,
            limitations=limitations,
        )
        observed_at_value = observation.get("observed_at")
        try:
            observed_at = float(str(observed_at_value)) if observed_at_value is not None else 0.0
        except (TypeError, ValueError):
            observed_at = 0.0
        if observed_at <= 0:
            observed_at = next(
                (
                    item.created_at
                    for item in store.manifest.artifacts
                    if item.artifact_id == artifact_id
                ),
                0.0,
            )
        receipt_id = f"{phase_name}:{artifact_id}:{cell_id}"
        receipt = ObservationReceipt(
            receipt_id=receipt_id,
            cell_id=cell_id,
            phase=phase_name,
            status=matrix.cells[cell_id].status,
            artifact_ids=evidence_ids,
            source_revision=str(observation.get("source_revision", context.target_url)),
            measured=measured,
            limitations=limitations,
            observed_at=observed_at,
        )
        receipt_record = store.put_json(
            "observation_receipt",
            receipt.to_dict(),
            phase=phase_name,
            attempt=context.phase_attempt,
            source_cells=(cell_id,),
        )
        matrix.record(
            cell_id,
            artifact_ids=(f"observation_receipt:{receipt_record.artifact_id}",),
            receipt_id=receipt_id,
        )
        context.required_artifact_ids = [
            *dict.fromkeys([*context.required_artifact_ids, receipt_record.artifact_id])
        ]
        context.research_receipt_ids = [
            *dict.fromkeys([*context.research_receipt_ids, receipt_record.artifact_id])
        ]

    @staticmethod
    def _coverage_artifact_kind(artifact_id: str) -> str:
        """Map compact cell evidence labels to plan artifact kinds."""
        aliases = {
            "measurement": "visual_measurements",
            "interaction_trace": "interaction_trace_catalog",
            "responsive_observation": "responsive_behavior_matrix",
            "observation_receipt": "observation_receipt",
        }
        prefix = artifact_id.split(":", 1)[0]
        return aliases.get(prefix, prefix)

    def _invalidate_research_coverage(
        self,
        context: ReconstructContext,
        invalidated_kinds: frozenset[str],
        target_phase: str,
    ) -> None:
        """Stale only cells backed by evidence owned by a re-entered phase."""
        try:
            matrix = self._coverage(context)
        except ResearchValidationError:
            return
        for cell_id, cell in tuple(matrix.cells.items()):
            if not any(
                self._coverage_artifact_kind(item) in invalidated_kinds
                for item in cell.artifact_ids
            ):
                continue
            retained = tuple(
                item
                for item in cell.artifact_ids
                if self._coverage_artifact_kind(item) not in invalidated_kinds
            )
            matrix.upsert(
                dataclasses.replace(
                    cell,
                    status=CoverageStatus.STALE.value,
                    artifact_ids=retained,
                    observation_receipt_id="",
                    limitations=tuple(
                        dict.fromkeys(
                            (*cell.limitations, f"research re-entry requested at {target_phase}")
                        )
                    ),
                    observed_at=time.time(),
                )
            )
        context.coverage_matrix = matrix.to_dict()

    def _apply_research_coverage(
        self,
        context: ReconstructContext,
        phase_name: str,
        artifact_id: str,
    ) -> None:
        """Merge phase observations into coverage and retain a compact copy."""
        matrix = self._coverage(context)
        if phase_name == "visual_research":
            observations = context.visual_observations
            for observation in observations:
                targets = self._observation_cell_ids(matrix, observation)
                for cell_id in targets:
                    self._record_observation_receipt(
                        context,
                        matrix,
                        phase_name=phase_name,
                        artifact_id=artifact_id,
                        cell_id=cell_id,
                        observation=observation,
                        evidence_ids=(
                            f"measurement:{artifact_id}",
                            *self._screenshot_ids_for_cell(context, cell_id),
                        ),
                        measured=True,
                    )
        elif phase_name == "interaction_analysis":
            for trace in context.interaction_traces:
                for cell_id in self._observation_cell_ids(matrix, trace):
                    self._record_observation_receipt(
                        context,
                        matrix,
                        phase_name=phase_name,
                        artifact_id=artifact_id,
                        cell_id=cell_id,
                        observation=trace,
                        evidence_ids=(f"interaction_trace:{artifact_id}",),
                        measured=False,
                    )
        elif phase_name == "responsive_research":
            for observation in context.responsive_inventory:
                for cell_id in self._observation_cell_ids(matrix, observation):
                    self._record_observation_receipt(
                        context,
                        matrix,
                        phase_name=phase_name,
                        artifact_id=artifact_id,
                        cell_id=cell_id,
                        observation=observation,
                        evidence_ids=(f"responsive_observation:{artifact_id}",),
                        measured=True,
                    )
        context.coverage_matrix = matrix.to_dict()

    def _screenshot_ids_for_cell(
        self, context: ReconstructContext, cell_id: str
    ) -> tuple[str, ...]:
        """Return typed screenshot IDs matching a reference coverage cell."""
        store = self._ensure_evidence(context)
        try:
            surface, viewport, state, _cluster, role = cell_id.split("|", 4)
        except ValueError:
            return ()
        return tuple(
            f"screenshot:{item.screenshot_id}"
            for item in store.manifest.screenshots
            if item.role == role
            and item.route == surface
            and item.viewport == viewport
            and item.page_state in {state, "default"}
            and item.status == "complete"
        )

    def _attach_screenshot_provenance(self, context: ReconstructContext) -> None:
        """Link browser screenshots to matching typed coverage cells.

        The screenshot tool accepts explicit cell IDs, but the browser can
        also capture a valid reference before the model has emitted the
        corresponding observation.  Matching the stable route, viewport,
        visual state, and role here keeps the manifest evidence-complete in
        both cases.  It only updates metadata; browser-owned files remain
        untouched.
        """
        store = self._ensure_evidence(context)
        try:
            matrix = self._coverage(context)
        except ResearchValidationError:
            return
        for screenshot in store.manifest.screenshots:
            if screenshot.status != "complete" or not screenshot.artifact_id:
                continue
            source_cells = tuple(
                cell.cell_id
                for cell in matrix.cells.values()
                if cell.role == screenshot.role
                and cell.surface_id == screenshot.route
                and cell.viewport_id == screenshot.viewport
                and screenshot.page_state in {cell.visual_state, "default"}
            )
            if source_cells:
                store.attach_artifact_source_cells(screenshot.artifact_id, source_cells)

    def _mark_browser_unavailable(self, context: ReconstructContext) -> None:
        """Turn a missing browser into explicit unavailable cells."""
        if getattr(self._cfg, "browser_manager", None) is not None:
            return
        try:
            matrix = self._coverage(context)
        except ResearchValidationError:
            return
        for cell_id, cell in tuple(matrix.cells.items()):
            if cell.status in {
                CoverageStatus.WAIVED.value,
                CoverageStatus.NOT_APPLICABLE.value,
            }:
                continue
            matrix.mark_unavailable(
                cell_id, "No browser integration is configured for this session."
            )
        context.coverage_matrix = matrix.to_dict()

    def _active_plan_skips(self) -> tuple[tuple[str, str], ...]:
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            return ()
        return tuple((item.name, item.reason) for item in plan.skipped)

    def _plan_for(self, context: ReconstructContext) -> ActiveReconstructPlan:
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            self._active_plan = self._select_profile(context)
            plan = self._active_plan
        return plan

    def _state_after_profile(
        self, state: ReconstructState, plan: ActiveReconstructPlan
    ) -> ReconstructState:
        name = state.name.lower()
        if state.is_terminal:
            return state
        if name in plan.names:
            return state
        all_names = RECONSTRUCT_PHASE_PLAN.names
        try:
            position = all_names.index(name)
        except ValueError as exc:
            raise PhasePlanError(f"state {name!r} is absent from the phase plan") from exc
        for candidate in plan.definitions:
            if all_names.index(candidate.name) > position:
                return ReconstructState[candidate.state_name]
        return ReconstructState.FINAL_VALIDATION

    def _publish_phase(
        self, context: ReconstructContext, phase_name: str, plan: ActiveReconstructPlan
    ) -> None:
        index = plan.index[phase_name]
        model = self._phase_model(phase_name) or getattr(self, "_model_id", "")
        context.cache_epoch = getattr(self, "_tool_cache_epoch", 0)
        context.cache_epoch_reason = getattr(self, "_tool_cache_epoch_reason", "initial")
        display_name = phase_name
        if phase_name == "page":
            progress = context.page_progress
            display_name = f"page {progress['current']}/{progress['total']}"
        publish_phase_annotation(
            self._cfg,
            PhaseAnnotation(
                workflow_name=self.workflow_name,
                phase_name=phase_name,
                phase_index=index,
                total_phases=plan.total_phases,
                run_id=context.run_id,
                intent=context.intent,
                model_id=model,
                phase_iteration=context.phase_iteration,
                phase_attempt=context.phase_attempt,
                plan_version=plan.version,
            ),
            context,
            display_name=display_name,
        )

    def _summary_for(self, context: ReconstructContext, phase_name: str) -> str:
        value = context.artifacts.get(phase_name, "")
        if value and not value.endswith((".md", ".json")):
            return value
        return context.last_transition or f"{phase_name} completed"

    def _json_artifact_value(
        self,
        context: ReconstructContext,
        phase_name: str,
        artifact_kind: str = "",
    ) -> object:
        if artifact_kind == "research_scope":
            return context.research_scope or {
                "reference_url": context.target_url,
                "target_directory": context.target_directory,
            }
        if artifact_kind in {"route_surface_inventory", "route_inventory"}:
            return context.route_inventory
        if artifact_kind == "viewport_environment_matrix":
            return [item.to_dict() for item in self._viewports(context)]
        if artifact_kind == "visual_state_inventory":
            return [
                {
                    "route": item.get("route", item.get("surface_id", "")),
                    "visual_states": item.get("visual_states", item.get("states", ["loaded"])),
                }
                for item in context.route_inventory
            ]
        if artifact_kind == "visual_measurements":
            return context.visual_observations
        if artifact_kind == "interaction_trace_catalog":
            return context.interaction_traces
        if artifact_kind == "interaction_state_graph":
            return [
                {
                    "trace_id": item.get("trace_id", ""),
                    "cell_id": item.get("cell_id", ""),
                    "visible_outcome": item.get("visible_outcome", ""),
                    "navigation_effect": item.get("navigation_effect", {}),
                }
                for item in context.interaction_traces
            ]
        if artifact_kind == "content_asset_inventory":
            return context.asset_inventory
        if artifact_kind == "font_icon_inventory":
            return [
                item
                for item in context.asset_inventory
                if str(item.get("type", "")).lower() in {"font", "icon", "svg"}
            ]
        if artifact_kind == "responsive_behavior_matrix":
            return {
                "observations": context.responsive_inventory,
                "breakpoints": context.responsive_breakpoints,
            }
        if artifact_kind == "research_gate_receipt":
            return context.research_gate_decision or {
                "status": context.research_gate_status,
                "baseline_artifact_id": context.research_baseline_id,
            }
        if phase_name == "recon":
            return context.route_inventory
        if phase_name == "visual_research":
            return {
                "design_tokens": context.design_tokens,
                "observations": context.visual_observations,
            }
        if phase_name == "interaction_analysis":
            return {
                "interactions": context.interaction_inventory,
                "traces": context.interaction_traces,
            }
        if phase_name == "content_assets":
            return context.asset_inventory
        if phase_name == "responsive_research":
            return {
                "observations": context.responsive_inventory,
                "breakpoints": context.responsive_breakpoints,
            }
        if phase_name == "design_system":
            return {
                "design_tokens": context.design_tokens,
                "components": context.component_inventory,
            }
        if phase_name == "architecture":
            return context.architecture
        if phase_name == "research_gate":
            return context.research_gate_decision or {
                "status": context.research_gate_status,
                "baseline_artifact_id": context.research_baseline_id,
            }
        return {
            "phase": phase_name,
            "summary": self._summary_for(context, phase_name),
            "transition": context.last_transition,
        }

    def _persist_evidence(self, context: ReconstructContext, phase_name: str) -> None:
        store = self._ensure_evidence(context)
        attempt = context.phase_attempt
        plan = self._plan_for(context)
        definition = plan.definition(phase_name)
        source_cells = self._source_cells_for_phase(context, phase_name)
        records: dict[str, ArtifactRecord] = {}
        if definition.artifact_kinds:
            for kind in definition.artifact_kinds:
                if kind in {"screenshot", "phase_receipt"}:
                    continue
                if kind == "architecture":
                    record = store.put(
                        kind,
                        str(self._json_artifact_value(context, phase_name, kind)),
                        phase=phase_name,
                        attempt=attempt,
                        media_type="text/markdown",
                        suffix=".md",
                        source_cells=source_cells,
                    )
                elif kind == "fidelity_baseline":
                    self._publish_baseline(context, store, attempt)
                    baseline_record = next(
                        (
                            item
                            for item in store.manifest.artifacts
                            if item.kind == kind
                            and item.artifact_id == context.research_baseline_id
                        ),
                        None,
                    )
                    if baseline_record is None:
                        raise EvidenceIntegrityError(
                            "published research baseline is not in manifest"
                        )
                    record = baseline_record
                else:
                    record = store.put_json(
                        kind,
                        self._json_artifact_value(context, phase_name, kind),
                        phase=phase_name,
                        attempt=attempt,
                        source_cells=source_cells,
                    )
                records[kind] = record
                context.required_artifact_ids = [
                    *dict.fromkeys([*context.required_artifact_ids, record.artifact_id])
                ]
                context.artifacts[kind] = record.relative_path
        if phase_name == "recon":
            # Route discovery defines the matrix. Browser-unavailable cells are
            # explicit so degraded approval can be deliberate rather than an
            # accidental gate bypass.
            matrix = build_coverage_matrix(
                context.profile, context.route_inventory, self._viewports(context)
            )
            context.coverage_matrix = matrix.to_dict()
            self._mark_browser_unavailable(context)
        self._attach_screenshot_provenance(context)
        primary_record = records.get(definition.artifact_kinds[0])
        if primary_record is not None and phase_name in {
            "visual_research",
            "interaction_analysis",
            "responsive_research",
        }:
            self._apply_research_coverage(context, phase_name, primary_record.artifact_id)
        receipt = store.write_phase_receipt(
            phase_name,
            attempt,
            self._summary_for(context, phase_name),
            transition=context.last_transition,
            source_cells=source_cells,
        )
        context.required_artifact_ids = [
            *dict.fromkeys([*context.required_artifact_ids, receipt.artifact_id])
        ]
        if phase_name in {
            "recon",
            "visual_research",
            "interaction_analysis",
            "content_assets",
            "responsive_research",
            "architecture",
            "design_system",
            "research_gate",
        }:
            context.research_receipt_ids = [
                *dict.fromkeys([*context.research_receipt_ids, receipt.artifact_id])
            ]
        context.artifact_manifest_revision = store.manifest.revision
        context.screenshot_ids = [item.screenshot_id for item in store.manifest.screenshots]
        try:
            cell_count = len(self._coverage(context).cells)
        except ResearchValidationError:
            cell_count = 0
        context.research_metrics.update(
            {
                "route_count": len(context.route_inventory),
                "cell_count": cell_count,
                "browser_calls": len(store.manifest.screenshots),
                "artifact_bytes": sum(
                    item.byte_count
                    for item in store.manifest.artifacts
                    if item.status == "complete"
                ),
                "llm_turns": max(0, int(getattr(self._cfg, "completed_turns", 0))),
            }
        )
        digest = json.dumps(store.checkpoint_digest(), sort_keys=True, separators=(",", ":"))
        context.phase_digest = hashlib.sha256(digest.encode()).hexdigest()[:32]

    def _publish_baseline(
        self, context: ReconstructContext, store: ReconstructEvidenceStore, attempt: int
    ) -> object:
        """Publish the normalized baseline and coverage report once per attempt."""
        matrix = self._coverage(context)
        source_cells = tuple(sorted(matrix.cells))
        coverage_record = store.put_json(
            "research_coverage_report",
            matrix.to_dict(),
            phase="research_gate",
            attempt=attempt,
            source_cells=source_cells,
        )
        context.required_artifact_ids = [
            *dict.fromkeys([*context.required_artifact_ids, coverage_record.artifact_id])
        ]
        baseline = FidelityBaseline(
            profile=context.profile,
            scope={
                "reference_url": context.target_url,
                "target_directory": context.target_directory,
                "intent": context.intent,
            },
            route_inventory=tuple(context.route_inventory),
            viewports=self._viewports(context),
            coverage=matrix,
            artifact_ids=tuple(
                dict.fromkeys(
                    [
                        item_id
                        for item_id in context.required_artifact_ids
                        if item_id != context.research_baseline_id
                    ]
                    + [
                        coverage_record.artifact_id,
                    ]
                )
            ),
            unresolved_questions=tuple(context.unresolved_research),
            exceptions=tuple(context.research_exceptions),
            source_revision=context.target_url,
            manifest_revision=store.manifest.revision,
        )
        baseline_record = store.put_json(
            "fidelity_baseline",
            baseline.to_dict(),
            phase="research_gate",
            attempt=attempt,
            source_cells=source_cells,
        )
        context.research_baseline = baseline.to_dict()
        context.research_baseline_id = baseline_record.artifact_id
        context.required_artifact_ids = [
            *dict.fromkeys([*context.required_artifact_ids, baseline_record.artifact_id])
        ]
        context.artifact_manifest_revision = store.manifest.revision
        context.artifacts["research_coverage_report"] = coverage_record.relative_path
        context.artifacts["fidelity_baseline"] = baseline_record.relative_path
        return baseline_record

    def _rehydrate_evidence(self, context: ReconstructContext) -> None:
        if not context.artifact_manifest_path:
            return
        store = self._ensure_evidence(context)
        errors = store.verify()
        if errors:
            self._recover_corrupt_evidence(context, errors)
        loaders: tuple[tuple[str, str, type[object]], ...] = (
            ("research_scope", "init", dict),
            ("route_inventory", "route_inventory", list),
            ("viewport_environment_matrix", "recon", list),
            ("visual_spec", "visual_research", dict),
            ("interaction_inventory", "interaction_analysis", list),
            ("asset_inventory", "content_assets", list),
            ("design_system", "design_system", dict),
            ("responsive_research", "responsive_research", dict),
            ("research_coverage_report", "research_gate", dict),
            ("fidelity_baseline", "research_gate", dict),
            ("research_gate_receipt", "research_gate", dict),
        )
        for kind, _phase, _expected in loaders:
            raw = store.read_kind(kind)
            if raw is None:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if kind == "research_scope":
                if isinstance(value, dict):
                    context.research_scope = value
            elif kind == "route_inventory":
                if not isinstance(value, list):
                    continue
                routes = [item for item in value if isinstance(item, dict)]
                context.route_inventory = routes
                context.pages_to_implement = [
                    str(item.get("route", "")).strip() for item in routes if item.get("route")
                ]
            elif kind == "visual_spec":
                if isinstance(value, dict):
                    tokens = value.get("design_tokens", value)
                    if isinstance(tokens, dict):
                        context.design_tokens = tokens
                    observations = value.get("observations", [])
                    if isinstance(observations, list):
                        context.visual_observations = [
                            item for item in observations if isinstance(item, dict)
                        ]
            elif kind == "interaction_inventory":
                if isinstance(value, list):
                    context.interaction_inventory = [
                        item for item in value if isinstance(item, dict)
                    ]
                elif isinstance(value, dict):
                    interactions = value.get("interactions", [])
                    traces = value.get("traces", [])
                    if isinstance(interactions, list):
                        context.interaction_inventory = [
                            item for item in interactions if isinstance(item, dict)
                        ]
                    if isinstance(traces, list):
                        context.interaction_traces = [
                            item for item in traces if isinstance(item, dict)
                        ]
            elif kind == "asset_inventory":
                if isinstance(value, list):
                    context.asset_inventory = [item for item in value if isinstance(item, dict)]
            elif kind == "design_system":
                if not isinstance(value, dict):
                    continue
                tokens = value.get("design_tokens", context.design_tokens)
                if isinstance(tokens, dict):
                    context.design_tokens = tokens
                components = value.get("components", [])
                if isinstance(components, list):
                    context.component_inventory = [
                        item for item in components if isinstance(item, dict)
                    ]
            elif kind == "responsive_research":
                if isinstance(value, dict):
                    observations = value.get("observations", [])
                    breakpoints = value.get("breakpoints", [])
                    if isinstance(observations, list):
                        context.responsive_inventory = [
                            item for item in observations if isinstance(item, dict)
                        ]
                    if isinstance(breakpoints, list):
                        context.responsive_breakpoints = [
                            item for item in breakpoints if isinstance(item, dict)
                        ]
            elif kind == "viewport_environment_matrix":
                if not isinstance(value, list) or not all(
                    isinstance(item, Mapping) for item in value
                ):
                    continue
                try:
                    context.research_viewports = [
                        ViewportSpec.from_dict(item).to_dict() for item in value
                    ]
                    if not context.research_viewports:
                        raise ResearchValidationError("viewport matrix is empty")
                except ResearchValidationError as exc:
                    raise EvidenceIntegrityError(
                        "research viewport environment artifact is malformed"
                    ) from exc
            elif kind == "research_coverage_report":
                if isinstance(value, dict):
                    try:
                        matrix = CoverageMatrix.from_dict(value)
                    except ResearchValidationError:
                        context.coverage_matrix = {}
                        context.research_gate_status = "pending"
                        context.research_baseline = {}
                        context.research_baseline_id = ""
                        context.known_issues.append(
                            {
                                "phase": "research_gate",
                                "issue": "persisted research coverage is malformed",
                                "severity": "high",
                            }
                        )
                        self._move_to_recovery_phase(context, "visual_research")
                        continue
                    context.coverage_matrix = matrix.to_dict()
                    context.unresolved_research = list(matrix.blocking_cells())
            elif kind == "fidelity_baseline":
                if isinstance(value, dict):
                    try:
                        baseline = FidelityBaseline.from_dict(value)
                    except ResearchValidationError:
                        context.research_baseline = {}
                        context.research_baseline_id = ""
                        context.research_gate_status = "pending"
                        context.known_issues.append(
                            {
                                "phase": "research_gate",
                                "issue": "persisted fidelity baseline is malformed",
                                "severity": "high",
                            }
                        )
                        self._move_to_recovery_phase(context, "research_gate")
                        continue
                    context.research_baseline = baseline.to_dict()
                    baseline_records = [
                        item
                        for item in store.manifest.artifacts
                        if item.kind == kind and item.status == "complete"
                    ]
                    baseline_record = max(
                        baseline_records,
                        key=lambda item: (item.created_at, item.artifact_id),
                        default=None,
                    )
                    if baseline_record is not None:
                        context.research_baseline_id = baseline_record.artifact_id
            elif kind == "research_gate_receipt":
                if isinstance(value, dict):
                    context.research_gate_decision = value
                    status = str(value.get("status", "")).strip()
                    if status:
                        context.research_gate_status = status
        context.artifact_manifest_revision = store.manifest.revision
        context.screenshot_ids = [item.screenshot_id for item in store.manifest.screenshots]

    def _reconcile_resume_cursor(
        self, context: ReconstructContext, plan: ActiveReconstructPlan
    ) -> None:
        """Resolve a restored cursor against verified phase receipts.

        Checkpoint context is the normal source of truth, but it can be older
        than the manifest when a process stopped between the phase transition
        and the next checkpoint write.  Receipt evidence is therefore allowed
        to advance an older cursor only when it proves a contiguous canonical
        prefix.  This method runs after evidence rehydration and before
        ``_run_context`` can construct a phase prompt or call a provider.
        """

        current_name = context.state.name.lower()
        if context.state.is_terminal:
            context.resume_resolution_source = "checkpoint_cursor"
            return

        receipt_phases: tuple[str, ...] = ()
        journal_phases: tuple[str, ...] = ()
        store = getattr(self, "_evidence", None)
        if store is not None:
            try:
                receipts = store.read_phase_receipts()
                receipt_phases = tuple(
                    str(item["phase"]) for item in receipts if isinstance(item.get("phase"), str)
                )
            except EvidenceIntegrityError as exc:
                # Integrity failures were already narrowed by _rehydrate_evidence
                # where possible.  Do not infer later progress from a malformed
                # receipt; retain the validated checkpoint cursor and expose a
                # bounded diagnostic for the normal recovery path.
                context.resume_resolution_source = "checkpoint_cursor"
                context.resume_resolution_reason = (
                    f"phase receipt reconciliation unavailable: {type(exc).__name__}: {exc}"
                )[:512]
                context.known_issues.append(
                    {
                        "phase": current_name,
                        "issue": context.resume_resolution_reason,
                        "severity": "high",
                    }
                )

        handle = self._cfg.workflow_handle
        if handle is not None:
            try:
                conversation = object.__getattribute__(handle, "conversation")
                journal = object.__getattribute__(conversation, "journal")
                fold_boundaries = object.__getattribute__(journal, "fold_workflow_phase_boundaries")
                journal_records = fold_boundaries(context.run_id, self.workflow_name)
                journal_phases = tuple(
                    str(item["completed_phase"])
                    for item in journal_records
                    if isinstance(item.get("completed_phase"), str)
                )
            except (AttributeError, TypeError):
                journal_phases = ()

        preserve_current = False
        if context.reentry_history:
            latest = context.reentry_history[-1]
            preserve_current = str(latest.get("target_phase", "")).strip().lower() == current_name
        resolution = reconcile_phase_cursor(
            plan.names,
            current_name,
            completed_phases=context.completed_phases,
            receipt_phases=receipt_phases,
            journal_phases=journal_phases,
            terminal_phase="complete",
            preserve_current=preserve_current,
        )
        context.resume_resolution_source = resolution.source
        context.resume_resolution_reason = resolution.diagnostic[:512]
        context.resume_reconciled = resolution.reconciled

        for phase_name in resolution.completed_phases:
            if phase_name not in context.completed_phases:
                context.completed_phases.append(phase_name)

        if resolution.phase_name == "complete":
            resolved_state = ReconstructState.COMPLETE
        else:
            resolved_state = ReconstructState[resolution.phase_name.upper()]
        if resolved_state is context.state:
            return

        context.state = resolved_state
        context.last_transition = "resume_reconciled"
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
            # This is a reconciliation checkpoint, not a phase-entry write.
            # It must exist before _run_context publishes a prompt for the
            # selected phase.
            handle.update_phase(
                None if resolved_state.is_terminal else resolution.phase_name,
                resolution.phase_index,
                context.phase_iteration,
                persist=False,
            )
            handle.save_checkpoint(reason="resume_reconciled")

    def _recover_corrupt_evidence(
        self, context: ReconstructContext, errors: Sequence[Mapping[str, object]]
    ) -> None:
        """Make corrupt evidence resumable by staling it and rewinding narrowly."""
        store = self._ensure_evidence(context)
        artifact_ids = tuple(
            str(item.get("artifact_id", "")).strip()
            for item in errors
            if str(item.get("artifact_id", "")).strip()
        )
        affected = store.mark_stale_artifacts(
            artifact_ids,
            reason="integrity verification failed during resume",
        )
        context.stale_artifact_ids = [*dict.fromkeys([*context.stale_artifact_ids, *affected])]
        error_targets = tuple(
            (
                str(item.get("artifact_id", "")).strip(),
                self._recovery_phase_for_kind(str(item.get("kind", "")).strip()),
            )
            for item in errors
            if str(item.get("artifact_id", "")).strip()
        )
        context.known_issues.extend(
            {
                "phase": phase_name,
                "issue": f"evidence artifact {artifact_id} failed integrity verification",
                "severity": "high",
            }
            for artifact_id, phase_name in error_targets
        )
        context.research_gate_status = "pending"
        self._move_to_recovery_phase(
            context,
            min(
                (phase_name for _artifact_id, phase_name in error_targets),
                key=lambda item: RECONSTRUCT_PHASE_PLAN.names.index(item),
                default="research_gate",
            ),
        )

    @staticmethod
    def _recovery_phase_for_kind(kind: str) -> str:
        if kind in {"research_scope", "initial_state"}:
            return "init"
        if kind in {
            "route_inventory",
            "route_surface_inventory",
            "viewport_environment_matrix",
            "visual_state_inventory",
        }:
            return "recon"
        if kind in {"visual_spec", "visual_measurements", "screenshot"}:
            return "visual_research"
        if kind in {
            "interaction_inventory",
            "interaction_trace_catalog",
            "interaction_state_graph",
        }:
            return "interaction_analysis"
        if kind in {"asset_inventory", "content_asset_inventory", "font_icon_inventory"}:
            return "content_assets"
        if kind in {"responsive_research", "responsive_behavior_matrix"}:
            return "responsive_research"
        if kind == "research_coverage_report":
            return "visual_research"
        if kind in {"fidelity_baseline", "research_gate_receipt"}:
            return "research_gate"
        return "research_gate"

    def _move_to_recovery_phase(self, context: ReconstructContext, phase_name: str) -> None:
        """Rewind only when the persisted state is downstream of recovery."""
        plan = self._plan_for(context)
        if phase_name not in plan.names:
            phase_name = "visual_research" if "visual_research" in plan.names else plan.names[0]
        current_name = context.state.name.lower()
        if current_name not in plan.names or plan.index[phase_name] < plan.index[current_name]:
            context.state = ReconstructState[phase_name.upper()]

    async def _run_context(self, context: ReconstructContext, memory: object) -> ReconstructContext:
        self._active_context = context
        plan = self._plan_for(context)
        state = self._state_after_profile(context.state, plan)
        while not state.is_terminal:
            phase_name = state.name.lower()
            if phase_name not in plan.names:
                state = self._state_after_profile(state, plan)
                continue
            if (
                phase_name == "bootstrap"
                and "research_gate" in plan.names
                and context.research_gate_status
                not in {
                    "approved",
                    "approved_degraded",
                }
            ):
                # A hand-edited or old checkpoint must not jump over the hard
                # research boundary. Resume at the gate and let it reconcile
                # missing coverage before any target files are changed.
                state = ReconstructState.RESEARCH_GATE
                context.state = state
                continue
            self._active_phase_name = phase_name
            if phase_name in {
                "recon",
                "visual_research",
                "interaction_analysis",
                "content_assets",
                "responsive_research",
                "architecture",
                "design_system",
                "research_gate",
            }:
                try:
                    context.research_cell_id = next(
                        iter(self._coverage(context).blocking_cells()), ""
                    )
                except ResearchValidationError:
                    context.research_cell_id = ""
            context.state = state
            context.phase_iteration += 1
            context.phase_attempt = context.phase_attempts.get(phase_name, 0) + 1
            context.phase_attempts[phase_name] = context.phase_attempt
            page_index_before = context.page_index
            self._publish_phase(context, phase_name, plan)
            handler = getattr(
                self,
                RECONSTRUCT_PHASE_PLAN.definitions[
                    RECONSTRUCT_PHASE_PLAN.names.index(phase_name)
                ].handler,
            )
            try:
                next_state = await handler(context, memory)
            except (PhasePlanError, ResearchValidationError, EvidenceIntegrityError) as exc:
                # Invalid agent-controlled re-entry is recoverable. Keep the
                # current phase unchanged so the next turn can correct it.
                context.fail_reason = str(exc)
                context.known_issues.append(
                    {"phase": phase_name, "issue": str(exc), "severity": "high"}
                )
                next_state = state
            boundary_requested = self.__dict__.get("_pending_reentry") is not None
            phase_transitioned = (
                next_state is not state
                or (phase_name == "page" and context.page_index != page_index_before)
                or boundary_requested
            )
            if phase_transitioned:
                try:
                    if phase_name not in context.completed_phases:
                        context.completed_phases.append(phase_name)
                    self._persist_evidence(context, phase_name)
                except (ResearchValidationError, EvidenceIntegrityError) as exc:
                    # A malformed observation or interrupted publication must
                    # leave the phase retryable; never checkpoint a transition
                    # whose evidence receipt was not durable.
                    context.fail_reason = str(exc)
                    context.known_issues.append(
                        {"phase": phase_name, "issue": str(exc), "severity": "high"}
                    )
                    if context.completed_phases and context.completed_phases[-1] == phase_name:
                        context.completed_phases.pop()
                    next_state = state
                    phase_transitioned = False
            pending_reentry = self.__dict__.get("_pending_reentry")
            if pending_reentry is not None:
                source, target, reason = pending_reentry
                affected = self._ensure_evidence(context).invalidate(
                    plan.invalidated_kinds(target),
                    source_phase=source,
                    target_phase=target,
                    reason=reason,
                )
                context.reentry_count += 1
                context.stale_artifact_ids = [
                    *dict.fromkeys([*context.stale_artifact_ids, *affected])
                ]
                context.reentry_history.append(
                    {
                        "source_phase": source,
                        "target_phase": target,
                        "reason": reason,
                        "artifact_ids": list(affected),
                    }
                )
                if target in {
                    "recon",
                    "visual_research",
                    "interaction_analysis",
                    "content_assets",
                    "responsive_research",
                    "architecture",
                    "design_system",
                    "research_gate",
                }:
                    self._invalidate_research_coverage(
                        context, plan.invalidated_kinds(target), target
                    )
                    context.research_gate_status = "pending"
                self._pending_reentry = None
            state = self._state_after_profile(next_state, plan)
            context.state = state
            context.artifact_manifest_revision = self._ensure_evidence(context).manifest.revision
            handle = self._cfg.workflow_handle
            if handle is not None:
                handle.attach_context(context)
                if phase_transitioned:
                    next_phase = None if state.is_terminal else state.name.lower()
                    boundary_index = (
                        plan.index[next_phase] if next_phase is not None else plan.index[phase_name]
                    )
                    checkpoint_phase_boundary(
                        self._cfg,
                        context,
                        completed_phase=phase_name,
                        next_phase=next_phase,
                        phase_index=boundary_index,
                        phase_iteration=context.phase_iteration,
                        outcome=("terminal" if state.is_terminal else "completed"),
                    )
            log.info("reconstruct_site[%s] → %s", context.profile, state.name)
        context.state = state
        evidence = getattr(self, "_evidence", None)
        if evidence is not None:
            evidence.set_metadata(
                status="complete" if state is ReconstructState.COMPLETE else "failed"
            )
            context.artifact_manifest_revision = evidence.manifest.revision
            context.screenshot_ids = [item.screenshot_id for item in evidence.manifest.screenshots]
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
        return context

    async def run(self, intent: str) -> ReconstructContext:  # type: ignore[override]
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = (
            handle.run_id
            if handle is not None
            else getattr(self, "_run_id", "") or uuid.uuid4().hex
        )
        self._run_id = run_id
        memory = self._cfg.session_memory or ShortTermMemory(
            max_tokens=self._cfg.cfg.execution.effective_usable_budget()
        )
        context = ReconstructContext(
            intent=intent,
            run_id=run_id,
            state=ReconstructState.INIT,
            shared_memory=memory,
            conversation_id=str(getattr(self._cfg, "conversation_id", "")),
        )
        return await self._run_context(context, memory)

    async def resume(self, context: object) -> ReconstructContext:  # type: ignore[override]
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, ReconstructContext):
            if not isinstance(context, PhaseContext):
                raise TypeError("reconstruct_site resume requires ReconstructContext")
            context = _upgrade_context(context)
        memory = (
            self._cfg.session_memory
            or context.shared_memory
            or ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        context.shared_memory = memory
        context.conversation_id = context.conversation_id or str(
            getattr(self._cfg, "conversation_id", "")
        )
        self._active_plan = self._select_profile(context)
        self._rehydrate_evidence(context)
        # This is intentionally before _run_context: its first operation for
        # a non-terminal state is phase prompt construction.  A stale INIT
        # checkpoint must therefore be reconciled before any provider call.
        self._reconcile_resume_cursor(context, self._active_plan)
        return await self._run_context(context, memory)

    async def run_phase(self, **kwargs: object) -> None:
        prompt = str(kwargs.get("system_prompt", ""))
        context = getattr(self, "_active_context", None)
        active_phase = getattr(self, "_active_phase_name", "")
        plan = getattr(self, "_active_plan", None)
        if plan is not None and active_phase:
            # PhasePlan is the source of truth for the turn budget. The
            # historical phase methods still pass their old literal values,
            # but cannot accidentally exceed the selected plan.
            kwargs["max_turns"] = plan.definition(active_phase).max_turns
        if context is not None and context.phase_digest:
            research_digest = ""
            try:
                research_digest = json.dumps(
                    self._coverage(context).compact_digest(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except ResearchValidationError:
                pass
            kwargs["system_prompt"] = (
                f"{prompt}\n\n[EVIDENCE DIGEST]\n"
                f"Manifest: {context.artifact_manifest_path}; revision: {context.artifact_manifest_revision}; "
                f"phase digest: {context.phase_digest}; profile: {context.profile}; "
                f"current research cell: {context.research_cell_id or 'none'}; "
                f"coverage: {research_digest or 'unavailable'}. "
                f"Baseline: {context.research_baseline_id or 'not published'}; "
                f"exceptions: {context.research_exceptions or 'none'}. "
                "Reuse verified artifacts by reference and cite their cell IDs."
            )
        stable_system_prompt = str(kwargs.pop("stable_system_prompt", ""))
        await super().run_phase(
            stable_system_prompt=stable_system_prompt,
            **kwargs,  # type: ignore[arg-type]
        )

    def _reentry_state(self, target_phase: str) -> ReconstructState:  # type: ignore[override]
        context = getattr(self, "_active_context", None)
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            plan = RECONSTRUCT_PHASE_PLAN.active(ReconstructProfile.PRODUCTION)
            self._active_plan = plan
        target = plan.resolve_reentry(target_phase.strip())
        if context is None:
            # Direct phase-method callers still receive strict target
            # validation, but there is no active run to mutate yet.
            return ReconstructState[target.state_name]
        if context.reentry_count >= self._params().max_reentries:
            raise PhasePlanError(f"re-entry budget exhausted ({self._params().max_reentries})")
        self._pending_reentry = (
            self._active_phase_name,
            target.name,
            f"validation requested re-entry to {target.name}",
        )
        return ReconstructState[target.state_name]

    def _validate_reentry_target(self, target_phase: str) -> str | None:
        """Return a model-safe error for invalid target arguments."""
        plan = getattr(self, "_active_plan", None)
        if plan is None:
            plan = RECONSTRUCT_PHASE_PLAN.active(ReconstructProfile.PRODUCTION)
            self._active_plan = plan
        try:
            plan.resolve_reentry(target_phase)
        except PhasePlanError as exc:
            return str(exc)
        active_context = getattr(self, "_active_context", None)
        if (
            active_context is not None
            and active_context.reentry_count >= self._params().max_reentries
        ):
            return f"re-entry budget exhausted ({self._params().max_reentries})"
        return None

    def _base_tools(self) -> list[ToolLike]:
        tool_cache = getattr(self, "_reconstruct_tool_cache", None)
        if tool_cache is not None:
            return list(tool_cache)
        tools = list(super()._base_tools())
        from lauren_ai._tools import tool
        from agenthicc.tools.capabilities import tool_write

        @tool_write
        @tool(name="record_reconstruct_screenshot")
        async def record_reconstruct_screenshot(
            browser_artifact_id: str,
            browser_artifact_path: str,
            role: str,
            route: str,
            url: str,
            viewport: str,
            width: int,
            height: int,
            device_scale: float = 1.0,
            page_state: str = "default",
            backend: str = "unknown",
            source_cells: list[str] | None = None,
            source_revision: str = "",
            fonts_loaded: bool | None = None,
            images_loaded: bool | None = None,
            network_complete: bool | None = None,
            redaction_status: str = "not_reported",
        ) -> dict[str, object]:
            """Link an existing browser screenshot to reconstruct evidence.

            The screenshot must already have been written by Playwright or
            CloakBrowser. This tool never accepts raw bytes and never creates
            a second browser client or a second workspace boundary.
            """
            context = self._active_context
            if context is None:
                return {"ok": False, "error": "no active reconstruct context"}
            record = self._ensure_evidence(context).record_screenshot(
                {"artifact_id": browser_artifact_id, "path": browser_artifact_path},
                role=role,
                route=route,
                url=url,
                viewport=viewport,
                width=width,
                height=height,
                device_scale=device_scale,
                page_state=page_state,
                backend=backend,
                phase=self._active_phase_name,
                attempt=context.phase_attempt,
                source_cells=source_cells or (),
                source_revision=source_revision,
                fonts_loaded=fonts_loaded,
                images_loaded=images_loaded,
                network_complete=network_complete,
                redaction_status=redaction_status,
            )
            context.screenshot_status = "complete"
            context.screenshot_ids = [
                *dict.fromkeys([*context.screenshot_ids, record.screenshot_id])
            ]
            context.artifact_manifest_revision = self._ensure_evidence(context).manifest.revision
            return {
                "ok": True,
                "screenshot_id": record.screenshot_id,
                "artifact_id": record.artifact_id,
            }

        self._reconstruct_tool_cache = [*tools, record_reconstruct_screenshot]
        if self._active_context is not None:
            tool_names = [
                str(getattr(item, "__name__", getattr(item, "name", "")))
                for item in self._reconstruct_tool_cache
            ]
            self._active_context.stable_tool_bundle_fingerprint = hashlib.sha256(
                "\n".join(tool_names).encode()
            ).hexdigest()[:32]
        return list(self._reconstruct_tool_cache)


def _upgrade_context(context: PhaseContext) -> ReconstructContext:
    values = {
        field.name: getattr(context, field.name)
        for field in dataclasses.fields(context)
        if field.name != "shared_memory"
    }
    values["shared_memory"] = context.shared_memory
    return ReconstructContext(**values)


class ReconstructSiteWorkflow(WorkflowPlugin):
    """Registry plugin for the PRD-177 runner."""

    name = PhaseWorkflow.name
    description = PhaseWorkflow.description
    mode_bindings = list(PhaseWorkflow.mode_bindings)
    phases = PhaseWorkflow.phases

    @classmethod
    def build_runner(
        cls, config: "WorkflowConfig", mode_manager: "ModeManager | None"
    ) -> ReconstructSiteRunner:
        return ReconstructSiteRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> ReconstructSiteParams:
        inherited_names = (
            "init_model",
            "recon_model",
            "visual_model",
            "interaction_model",
            "assets_model",
            "architecture_model",
            "design_model",
            "bootstrap_model",
            "shell_model",
            "components_model",
            "page_model",
            "data_model",
            "responsive_model",
            "visual_validation_model",
            "interaction_validation_model",
            "accessibility_model",
            "performance_model",
            "fidelity_model",
            "final_model",
        )
        raw_custom = source.get("custom_phases", ())
        if isinstance(raw_custom, str):
            custom = tuple(item.strip() for item in raw_custom.split(",") if item.strip())
        elif isinstance(raw_custom, (list, tuple)):
            custom = tuple(str(item).strip() for item in raw_custom if str(item).strip())
        else:
            custom = ()
        raw_models = source.get("phase_models", {})
        phase_models = (
            {str(key): str(value) for key, value in raw_models.items()}
            if isinstance(raw_models, Mapping)
            else {}
        )
        phase_models.update(
            {
                name.removesuffix("_model"): str(source[name])
                for name in source
                if name.endswith("_model") and name not in inherited_names
            }
        )
        try:
            max_reentries = max(0, int(str(source.get("max_reentries", 3))))
        except (TypeError, ValueError):
            max_reentries = 3
        return ReconstructSiteParams(
            **{name: str(source.get(name, "") or "") for name in inherited_names},
            profile=str(source.get("profile", "") or ""),
            custom_phases=custom,
            max_reentries=max_reentries,
            phase_models=phase_models,
            viewports=_parse_viewports(source.get("viewports", source.get("research_viewports"))),
        )

    @classmethod
    def create_initial_context(
        cls, intent: str, run_id: str, memory: object | None = None
    ) -> ReconstructContext:
        return ReconstructContext(
            intent=intent,
            run_id=run_id,
            state=ReconstructState.INIT,
            shared_memory=memory,  # type: ignore[arg-type]
        )

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        if isinstance(context, ReconstructContext):
            payload = PhaseWorkflow.checkpoint_context_to_payload(context)
            payload.update(
                {
                    "plan_version": context.plan_version,
                    "profile": context.profile,
                    "phase_attempt": context.phase_attempt,
                    "phase_attempts": dict(context.phase_attempts),
                    "artifact_manifest_path": context.artifact_manifest_path,
                    "artifact_manifest_revision": context.artifact_manifest_revision,
                    "required_artifact_ids": list(context.required_artifact_ids),
                    "stale_artifact_ids": list(context.stale_artifact_ids),
                    "screenshot_ids": list(context.screenshot_ids),
                    "research_receipt_ids": list(context.research_receipt_ids),
                    "research_gate_decision": dict(context.research_gate_decision),
                    "reentry_count": context.reentry_count,
                    "reentry_history": list(context.reentry_history),
                    "phase_digest": context.phase_digest,
                    "skipped_reasons": dict(context.skipped_reasons),
                    "screenshot_status": context.screenshot_status,
                    "conversation_id": context.conversation_id,
                    "browser_backend": context.browser_backend,
                    "browser_capability_status": context.browser_capability_status,
                    "research_cell_id": context.research_cell_id,
                    "research_metrics": dict(context.research_metrics),
                    "research_viewports": list(context.research_viewports),
                    "cache_epoch": context.cache_epoch,
                    "cache_epoch_reason": context.cache_epoch_reason,
                    "stable_tool_bundle_fingerprint": context.stable_tool_bundle_fingerprint,
                    "resume_resolution_source": context.resume_resolution_source,
                    "resume_resolution_reason": context.resume_resolution_reason,
                    "resume_reconciled": context.resume_reconciled,
                }
            )
            # Once an evidence manifest exists, the large research bodies are
            # recoverable from disk and must not be duplicated in checkpoints.
            if context.artifact_manifest_path:
                for field in (
                    "route_inventory",
                    "asset_inventory",
                    "interaction_inventory",
                    "visual_observations",
                    "interaction_traces",
                    "responsive_inventory",
                    "responsive_breakpoints",
                ):
                    payload[field] = []
                for field in ("design_tokens", "component_inventory"):
                    payload[field] = {}
                payload["architecture"] = ""
                try:
                    payload["coverage_matrix"] = CoverageMatrix.from_dict(
                        context.coverage_matrix
                    ).compact_digest()
                except ResearchValidationError:
                    payload["coverage_matrix"] = {}
                payload["research_baseline"] = {
                    "artifact_id": context.research_baseline_id,
                    "manifest_revision": context.artifact_manifest_revision,
                }
            return payload
        if isinstance(context, PhaseContext):
            return PhaseWorkflow.checkpoint_context_to_payload(context)
        raise TypeError("reconstruct_site checkpoint requires ReconstructContext")

    @classmethod
    def checkpoint_context_from_payload(
        cls, payload: dict[str, object], memory: object | None = None
    ) -> ReconstructContext:
        base = PhaseWorkflow.checkpoint_context_from_payload(payload, memory)
        values: dict[str, object] = {
            field.name: getattr(base, field.name)
            for field in dataclasses.fields(base)
            if field.name != "shared_memory"
        }
        values["shared_memory"] = memory
        for name, default in (
            ("plan_version", PHASE_PLAN_VERSION),
            ("profile", ReconstructProfile.STATIC.value),
            ("phase_digest", ""),
            ("artifact_manifest_path", ""),
            ("screenshot_status", "pending"),
            ("conversation_id", ""),
            ("browser_backend", ""),
            ("browser_capability_status", "unknown"),
            ("research_cell_id", ""),
            ("resume_resolution_source", ""),
            ("resume_resolution_reason", ""),
        ):
            values[name] = str(payload.get(name, default) or default)
        values["resume_reconciled"] = bool(payload.get("resume_reconciled", False))
        for name in ("phase_attempt", "artifact_manifest_revision", "reentry_count"):
            try:
                values[name] = max(0, int(str(payload.get(name, 0))))
            except (TypeError, ValueError):
                values[name] = 0
        try:
            values["cache_epoch"] = max(0, int(str(payload.get("cache_epoch", 0))))
        except (TypeError, ValueError):
            values["cache_epoch"] = 0
        values["cache_epoch_reason"] = str(payload.get("cache_epoch_reason", "") or "")
        values["stable_tool_bundle_fingerprint"] = str(
            payload.get("stable_tool_bundle_fingerprint", "") or ""
        )
        raw_metrics = payload.get("research_metrics", {})
        if isinstance(raw_metrics, Mapping):
            metrics: dict[str, int] = {}
            for key, metric_value in raw_metrics.items():
                try:
                    metrics[str(key)] = max(0, int(str(metric_value)))
                except (TypeError, ValueError):
                    continue
            values["research_metrics"] = metrics
        else:
            values["research_metrics"] = {}
        raw_viewports = payload.get("research_viewports", [])
        values["research_viewports"] = (
            [dict(item) for item in raw_viewports if isinstance(item, Mapping)]
            if isinstance(raw_viewports, list)
            else []
        )
        raw_receipts = payload.get("research_receipt_ids", [])
        values["research_receipt_ids"] = (
            [str(item) for item in raw_receipts if isinstance(item, str)]
            if isinstance(raw_receipts, list)
            else []
        )
        raw_gate = payload.get("research_gate_decision", {})
        values["research_gate_decision"] = (
            {str(key): item for key, item in raw_gate.items()}
            if isinstance(raw_gate, Mapping)
            else {}
        )
        for name in ("phase_attempts", "skipped_reasons"):
            raw = payload.get(name, {})
            values[name] = (
                {
                    str(key): int(value) if name == "phase_attempts" else str(value)
                    for key, value in raw.items()
                }
                if isinstance(raw, Mapping)
                else {}
            )
        for name in ("required_artifact_ids", "stale_artifact_ids", "screenshot_ids"):
            raw = payload.get(name, [])
            values[name] = (
                [str(item) for item in raw if isinstance(item, str)]
                if isinstance(raw, list)
                else []
            )
        raw_history = payload.get("reentry_history", [])
        values["reentry_history"] = (
            [dict(item) for item in raw_history if isinstance(item, Mapping)]
            if isinstance(raw_history, list)
            else []
        )
        return ReconstructContext(**values)  # type: ignore[arg-type]


def _parse_viewports(value: object) -> tuple[ViewportSpec, ...]:
    """Parse optional TOML/CLI viewport definitions and reject ambiguity."""
    if value is None or value == "":
        return DEFAULT_VIEWPORTS
    raw: object = value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("reconstruct_site.viewports must be a JSON array") from exc
    if isinstance(raw, Mapping):
        raw_items: list[object] = [raw]
    elif isinstance(raw, (list, tuple)):
        raw_items = list(raw)
    else:
        raise ValueError("reconstruct_site.viewports must be an array of objects")
    if not raw_items:
        raise ValueError("reconstruct_site.viewports must not be empty")
    viewports: list[ViewportSpec] = []
    for item in raw_items:
        if isinstance(item, ViewportSpec):
            viewports.append(item)
        elif isinstance(item, Mapping):
            viewports.append(ViewportSpec.from_dict(item))
        else:
            raise ValueError("each reconstruct_site viewport must be an object")
    ids = [item.viewport_id for item in viewports]
    if len(ids) != len(set(ids)):
        raise ValueError("reconstruct_site.viewports must have unique viewport_id values")
    return tuple(viewports)


# Keep the historical prompt-bearing registry for compatibility, but fail
# closed if a future edit changes its executable topology independently of the
# plan used by the runner.
RECONSTRUCT_PHASE_PLAN.validate_phase_specs(ReconstructSiteWorkflow.phases)
