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

import dataclasses
import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tools.base import ToolLike
from agenthicc.tools.sandbox import WorkspaceView
from .evidence import (
    EvidenceIntegrityError,
    ReconstructEvidenceStore,
)
from .evidence_plan import (
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
    _make_responsive_pass_tools,
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
    cache_epoch: int = 0
    cache_epoch_reason: str = "initial"
    stable_tool_bundle_fingerprint: str = ""

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
        plan = RECONSTRUCT_PHASE_PLAN.active(profile, self._params().custom_phases)
        context.profile = plan.profile.value
        context.plan_version = plan.version
        context.skipped_reasons = {item.name: item.reason for item in plan.skipped}
        context.skipped_phases = list(context.skipped_reasons)
        return plan

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
        return evidence

    async def _init(  # type: ignore[override]
        self, context: ReconstructContext, memory: object
    ) -> ReconstructState:
        state = await super()._init(context, memory)
        self._active_plan = self._select_profile(context)
        self._ensure_evidence(context).set_skipped(self._active_plan_skips())
        return state

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
        app_state = getattr(self._cfg, "app_state", None)
        update_phase = getattr(app_state, "update_workflow_phase", None)
        if callable(update_phase):
            update_phase(
                workflow_name=self.workflow_name,
                phase_name=display_name,
                phase_index=index,
                total_phases=plan.total_phases,
                run_id=context.run_id,
                intent=context.intent,
                model_id=model,
            )
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
            handle.update_phase(phase_name, index, context.phase_iteration)

    def _summary_for(self, context: ReconstructContext, phase_name: str) -> str:
        value = context.artifacts.get(phase_name, "")
        if value and not value.endswith((".md", ".json")):
            return value
        return context.last_transition or f"{phase_name} completed"

    def _json_artifact_value(self, context: ReconstructContext, phase_name: str) -> object:
        if phase_name == "recon":
            return context.route_inventory
        if phase_name == "visual_research":
            return context.design_tokens
        if phase_name == "interaction_analysis":
            return context.interaction_inventory
        if phase_name == "content_assets":
            return context.asset_inventory
        if phase_name == "design_system":
            return {
                "design_tokens": context.design_tokens,
                "components": context.component_inventory,
            }
        if phase_name == "architecture":
            return context.architecture
        return {
            "phase": phase_name,
            "summary": self._summary_for(context, phase_name),
            "transition": context.last_transition,
        }

    def _persist_evidence(self, context: ReconstructContext, phase_name: str) -> None:
        store = self._ensure_evidence(context)
        attempt = context.phase_attempt
        definition = RECONSTRUCT_PHASE_PLAN.definitions[
            RECONSTRUCT_PHASE_PLAN.names.index(phase_name)
        ]
        if definition.artifact_kinds:
            primary = definition.artifact_kinds[0]
            if primary not in {"screenshot", "phase_receipt"}:
                if primary == "architecture":
                    record = store.put(
                        primary,
                        str(self._json_artifact_value(context, phase_name)),
                        phase=phase_name,
                        attempt=attempt,
                        media_type="text/markdown",
                        suffix=".md",
                    )
                else:
                    record = store.put_json(
                        primary,
                        self._json_artifact_value(context, phase_name),
                        phase=phase_name,
                        attempt=attempt,
                    )
                context.required_artifact_ids = [
                    *dict.fromkeys([*context.required_artifact_ids, record.artifact_id])
                ]
                context.artifacts[primary] = record.relative_path
        receipt = store.write_phase_receipt(
            phase_name,
            attempt,
            self._summary_for(context, phase_name),
            transition=context.last_transition,
        )
        context.required_artifact_ids = [
            *dict.fromkeys([*context.required_artifact_ids, receipt.artifact_id])
        ]
        context.artifact_manifest_revision = store.manifest.revision
        context.screenshot_ids = [item.screenshot_id for item in store.manifest.screenshots]
        digest = json.dumps(store.checkpoint_digest(), sort_keys=True, separators=(",", ":"))
        context.phase_digest = hashlib.sha256(digest.encode()).hexdigest()[:32]

    def _rehydrate_evidence(self, context: ReconstructContext) -> None:
        if not context.artifact_manifest_path:
            return
        store = self._ensure_evidence(context)
        errors = store.verify()
        if errors:
            raise EvidenceIntegrityError(
                "reconstruct evidence is not resumable: "
                + ", ".join(str(item.get("artifact_id", "unknown")) for item in errors[:8])
            )
        loaders: tuple[tuple[str, str, type[object]], ...] = (
            ("route_inventory", "route_inventory", list),
            ("visual_spec", "visual_research", dict),
            ("interaction_inventory", "interaction_analysis", list),
            ("asset_inventory", "content_assets", list),
            ("design_system", "design_system", dict),
        )
        for kind, _phase, _expected in loaders:
            raw = store.read_kind(kind)
            if raw is None:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if kind == "route_inventory":
                if not isinstance(value, list):
                    continue
                routes = [item for item in value if isinstance(item, dict)]
                context.route_inventory = routes
                context.pages_to_implement = [
                    str(item.get("route", "")).strip() for item in routes if item.get("route")
                ]
            elif kind == "visual_spec":
                if isinstance(value, dict):
                    context.design_tokens = value
            elif kind == "interaction_inventory":
                if isinstance(value, list):
                    context.interaction_inventory = [
                        item for item in value if isinstance(item, dict)
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

    async def _run_context(self, context: ReconstructContext, memory: object) -> ReconstructContext:
        self._active_context = context
        plan = self._plan_for(context)
        state = self._state_after_profile(context.state, plan)
        while not state.is_terminal:
            phase_name = state.name.lower()
            if phase_name not in plan.names:
                state = self._state_after_profile(state, plan)
                continue
            self._active_phase_name = phase_name
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
            except PhasePlanError as exc:
                # Invalid agent-controlled re-entry is recoverable. Keep the
                # current phase unchanged so the next turn can correct it.
                context.fail_reason = str(exc)
                context.known_issues.append(
                    {"phase": phase_name, "issue": str(exc), "severity": "high"}
                )
                next_state = state
            phase_transitioned = next_state not in {
                ReconstructState.FAILED,
                ReconstructState.BLOCKED,
            } and (
                next_state is not state
                or (phase_name == "page" and context.page_index != page_index_before)
            )
            if phase_transitioned:
                self._persist_evidence(context, phase_name)
            pending_reentry = getattr(self, "_pending_reentry", None)
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
                self._pending_reentry = None
            state = self._state_after_profile(next_state, plan)
            context.state = state
            context.artifact_manifest_revision = self._ensure_evidence(context).manifest.revision
            handle = self._cfg.workflow_handle
            if handle is not None:
                handle.attach_context(context)
                if not state.is_terminal:
                    handle.persist_context_transition(reason="phase_transition")
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
        self._active_plan = self._select_profile(context)
        self._rehydrate_evidence(context)
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
            kwargs["system_prompt"] = (
                f"{prompt}\n\n[EVIDENCE DIGEST]\n"
                f"Manifest: {context.artifact_manifest_path}; revision: {context.artifact_manifest_revision}; "
                f"phase digest: {context.phase_digest}. Reuse verified artifacts by reference."
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
                    "reentry_count": context.reentry_count,
                    "reentry_history": list(context.reentry_history),
                    "phase_digest": context.phase_digest,
                    "skipped_reasons": dict(context.skipped_reasons),
                    "screenshot_status": context.screenshot_status,
                    "cache_epoch": context.cache_epoch,
                    "cache_epoch_reason": context.cache_epoch_reason,
                    "stable_tool_bundle_fingerprint": context.stable_tool_bundle_fingerprint,
                }
            )
            # Once an evidence manifest exists, the large research bodies are
            # recoverable from disk and must not be duplicated in checkpoints.
            if context.artifact_manifest_path:
                for field in ("route_inventory", "asset_inventory", "interaction_inventory"):
                    payload[field] = []
                for field in ("design_tokens", "component_inventory"):
                    payload[field] = {}
                payload["architecture"] = ""
            return payload
        if isinstance(context, PhaseContext):
            return PhaseWorkflow.checkpoint_context_to_payload(context)
        raise TypeError("reconstruct_site checkpoint requires ReconstructContext")

    @classmethod
    def checkpoint_context_from_payload(
        cls, payload: dict[str, object], memory: object | None = None
    ) -> ReconstructContext:
        base = PhaseWorkflow.checkpoint_context_from_payload(payload, memory)
        values = {
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
        ):
            values[name] = str(payload.get(name, default) or default)
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
        return ReconstructContext(**values)


# Keep the historical prompt-bearing registry for compatibility, but fail
# closed if a future edit changes its executable topology independently of the
# plan used by the runner.
RECONSTRUCT_PHASE_PLAN.validate_phase_specs(ReconstructSiteWorkflow.phases)
