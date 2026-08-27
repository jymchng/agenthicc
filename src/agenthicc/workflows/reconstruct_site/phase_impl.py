"""reconstruct_site — reconstruct a reference website as a modern Next.js app.

A 39-phase (including a controlled repeated per-route PAGE phase) orchestration
workflow:
discovery, deep research, analysis, architecture, design-system extraction,
project bootstrap, global shell, shared components, per-page implementation,
data layer, responsive pass, visual/interaction validation, accessibility,
performance, final fidelity pass, and final validation — then COMPLETE or
BLOCKED. All phase transitions happen only through explicit @tool_control
transition-tool calls; prose never advances the workflow. Later validation
phases can send the run back to any earlier phase via a `target_phase`
argument on their rejection tools (controlled re-entry).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable
from enum import Enum, auto
from typing import TYPE_CHECKING

from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

#: Bounded retries per phase — never loop forever waiting for a tool call.
_MAX_ATTEMPTS = 5

# Stable workflow policy.  Keep phase-specific instructions and current
# artifacts in the dynamic ``system_prompt`` argument to ``run_phase``.
CACHE_CONTRACT = """\
[CACHE-STABLE WORKFLOW POLICY]
Keep this workflow contract unchanged across phases. Ask the user a focused
clarifying question through the existing `ask_user` tool whenever required
information is missing, ambiguous, or would materially change the result.
Wait for the answer; do not guess. Use the parent session's
`WorkflowConfig.workspace_scope` and `WorkflowConfig.workspace_access` policy
for every filesystem, mention, Git, browser, and command-working-directory
access; never construct a second workspace scope, allow-list, or unrestricted
sandbox inside this workflow. Actual questions and answers, phase state, and
artifacts are dynamic context and do not belong here.
""".strip()


class ReconstructState(Enum):
    """Every state this workflow can be in."""

    INIT = auto()
    RECON = auto()
    VISUAL_RESEARCH = auto()
    INTERACTION_ANALYSIS = auto()
    CONTENT_ASSETS = auto()
    ARCHITECTURE = auto()
    DESIGN_SYSTEM = auto()
    BOOTSTRAP = auto()
    GLOBAL_SHELL = auto()
    COMPONENT_SYSTEM = auto()
    PAGE = auto()  # dynamic — re-entered once per discovered route
    DATA_LAYER = auto()
    RESPONSIVE_PASS = auto()
    VISUAL_VALIDATION = auto()
    INTERACTION_VALIDATION = auto()
    ACCESSIBILITY = auto()
    PERFORMANCE = auto()
    FIDELITY_PASS = auto()
    # --- infrastructure extension (production architecture) ---
    SQLITE_DB = auto()
    VERIFY_SQLITE = auto()
    PRISMA = auto()
    VERIFY_PRISMA = auto()
    TANSTACK_QUERY = auto()
    VERIFY_TANSTACK = auto()
    ENV_CONFIG = auto()
    VERIFY_ENV = auto()
    DOCKER = auto()
    VERIFY_DOCKER = auto()
    NETLIFY = auto()
    VERIFY_NETLIFY = auto()
    CADDY = auto()
    VERIFY_CADDY = auto()
    PACKAGE_COMMANDS = auto()
    VERIFY_PACKAGE = auto()
    SCRIPTS = auto()
    VERIFY_SCRIPTS = auto()
    DOCS = auto()
    VERIFY_DOCS = auto()
    FINAL_VALIDATION = auto()
    COMPLETE = auto()  # terminal
    BLOCKED = auto()  # terminal
    FAILED = auto()  # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (
            ReconstructState.COMPLETE,
            ReconstructState.BLOCKED,
            ReconstructState.FAILED,
        )


@dataclasses.dataclass
class ReconstructContext:
    """Data carried across every phase of one run."""

    intent: str
    run_id: str
    state: ReconstructState
    phase_iteration: int = 0
    fail_reason: str = ""

    # Workflow inputs
    target_url: str = ""
    target_directory: str = ""

    # Persistent workflow state (the "state file" the design demands)
    completed_phases: list[str] = dataclasses.field(default_factory=list)
    failed_phases: list[str] = dataclasses.field(default_factory=list)
    blocked_phases: list[str] = dataclasses.field(default_factory=list)
    skipped_phases: list[str] = dataclasses.field(default_factory=list)

    # Discovery outputs
    route_inventory: list[dict[str, object]] = dataclasses.field(default_factory=list)
    pages_to_implement: list[str] = dataclasses.field(default_factory=list)
    page_index: int = 0
    asset_inventory: list[dict[str, object]] = dataclasses.field(default_factory=list)
    component_inventory: list[dict[str, object]] = dataclasses.field(default_factory=list)
    design_tokens: dict[str, object] = dataclasses.field(default_factory=dict)
    architecture: str = ""
    interaction_inventory: list[dict[str, object]] = dataclasses.field(default_factory=list)

    # Status + issue tracking
    implementation_status: dict[str, str] = dataclasses.field(default_factory=dict)
    validation_status: dict[str, str] = dataclasses.field(default_factory=dict)
    known_issues: list[dict[str, object]] = dataclasses.field(default_factory=list)
    visual_discrepancies: list[dict[str, object]] = dataclasses.field(default_factory=list)
    interaction_discrepancies: list[dict[str, object]] = dataclasses.field(default_factory=list)
    last_transition: str = ""
    # Infrastructure extension status (sqlite/prisma/tanstack/env/docker/netlify/
    # caddy/package/scripts/docs + their verification results)
    infra_status: dict[str, str] = dataclasses.field(default_factory=dict)

    # Artifact paths (route_inventory.md, visual_spec.md, architecture.md, ...)
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)

    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)


# ── phase tool factories (discovery / research / analysis) ───────────────────


def _make_init_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the init phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_initial_state(
        reference_url: str,
        target_directory: str,
        constraints: str,
        desired_routes: str,
        auth_required: bool = False,
        reference_is_static: bool = True,
        reproduce_api_or_mock: str = "mock",
    ) -> dict[str, object]:
        """Record the initial workflow state and advance to reconnaissance.

        Args:
            reference_url: The reference website URL to reconstruct.
            target_directory: Directory where the Next.js app will be created.
            constraints: Project constraints (stack, conventions, exclusions).
            desired_routes: Comma-separated routes the user wants (may be empty).
            auth_required: Whether the reference has authentication UI.
            reference_is_static: Whether the reference is static or dynamic.
            reproduce_api_or_mock: 'reproduce' or 'mock' for API behaviour.
        """
        if not reference_url.strip():
            return {
                "ok": False,
                "error": "reference_url must not be empty.",
                "fix": "Provide the reference website URL.",
            }
        if not target_directory.strip():
            return {
                "ok": False,
                "error": "target_directory must not be empty.",
                "fix": "Provide the target application directory.",
            }
        data["reference_url"] = reference_url.strip()
        data["target_directory"] = target_directory.strip()
        data["constraints"] = constraints.strip()
        data["desired_routes"] = desired_routes.strip()
        data["auth_required"] = bool(auth_required)
        data["reference_is_static"] = bool(reference_is_static)
        data["reproduce_api_or_mock"] = reproduce_api_or_mock.strip() or "mock"
        event.set()
        return {
            "ok": True,
            "message": (
                f"Initial state recorded for {reference_url} → "
                f"{target_directory}. Reconnaissance starts next."
            ),
        }

    return [submit_initial_state]


def _make_recon_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends reconnaissance with the route inventory."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_route_inventory(
        routes: list[dict[str, object]],
        summary: str,
    ) -> dict[str, object]:
        """Record the discovered route inventory and advance to visual research.

        Args:
            routes: One entry per discovered route with keys: route, purpose,
                layout, components, interactions, data_requirements,
                responsive_considerations.
            summary: Site-level reconnaissance summary (nav, footer, menus,
                modals, forms, search, filters, tabs, cards, tables, pagination,
                loading/error/empty states, auth UI, responsive behaviour).
        """
        if not routes:
            return {
                "ok": False,
                "error": "routes must not be empty.",
                "fix": "Inventory every discoverable major route first.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Summarise the site-level reconnaissance.",
            }
        data["routes"] = routes
        data["summary"] = summary.strip()
        event.set()
        return {
            "ok": True,
            "message": f"Route inventory recorded ({len(routes)} routes). Visual research next.",
        }

    return [submit_route_inventory]


def _make_visual_research_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends visual research with concrete design observations."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_visual_spec(
        design_tokens: dict[str, object],
        summary: str,
    ) -> dict[str, object]:
        """Record the quantified visual design inventory and advance.

        Args:
            design_tokens: Concrete measured tokens (typography, spacing,
                containers, grids, colors, borders, shadows, radii, icons,
                image treatment, breakpoints). Values must be measured, not
                vague ("clean modern design" is not acceptable).
            summary: Narrative of the visual spec with concrete observations.
        """
        if not design_tokens:
            return {
                "ok": False,
                "error": "design_tokens must not be empty.",
                "fix": "Extract concrete measured visual tokens from the reference.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the visual spec with concrete observations.",
            }
        data["design_tokens"] = design_tokens
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Visual spec recorded. Interaction analysis next."}

    return [submit_visual_spec]


def _make_interaction_analysis_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends interaction analysis."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_interaction_inventory(
        interactions: list[dict[str, object]],
        summary: str,
    ) -> dict[str, object]:
        """Record the interaction/behaviour catalogue and advance.

        Args:
            interactions: One entry per interaction with keys: interaction,
                trigger, expected_behaviour, visual_state, data_dependency,
                url_or_state_change.
            summary: Narrative of how the site actually behaves (hover/focus/
                active, dropdowns, transitions, modals, drawers, accordions,
                tabs, carousels, forms+validation, loading, API requests,
                infinite scroll, pagination, URL/query state, animations,
                keyboard).
        """
        if not interactions:
            return {
                "ok": False,
                "error": "interactions must not be empty.",
                "fix": "Catalogue every observable interaction of the reference.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Summarise the interaction analysis.",
            }
        data["interactions"] = interactions
        data["summary"] = summary.strip()
        event.set()
        return {
            "ok": True,
            "message": "Interaction inventory recorded. Content/assets next.",
        }

    return [submit_interaction_inventory]


def _make_content_assets_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the content/asset inventory."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_asset_inventory(
        assets: list[dict[str, object]],
        summary: str,
    ) -> dict[str, object]:
        """Record the asset inventory and advance.

        Args:
            assets: One entry per asset with keys: name, type, dimensions,
                format, usage, reuse_or_recreate, source.
            summary: Reuse-vs-recreate strategy and legal note.
        """
        if not assets:
            return {
                "ok": False,
                "error": "assets must not be empty.",
                "fix": "Inventory logos, icons, images, illustrations, fonts, videos, SVGs.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the reuse/recreate strategy.",
            }
        data["assets"] = assets
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Asset inventory recorded. Architecture next."}

    return [submit_asset_inventory]


def _make_architecture_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the architecture phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_architecture(architecture: str) -> dict[str, object]:
        """Record the technical architecture document and advance.

        Args:
            architecture: Full architecture doc — App Router structure,
                server/client component split, layouts, loading/error
                boundaries, data-fetching architecture, query keys, API
                abstraction, state management, reusable components, shared
                UI primitives.
        """
        if not architecture.strip():
            return {
                "ok": False,
                "error": "architecture must not be empty.",
                "fix": "Design the target Next.js architecture before implementation.",
            }
        data["architecture"] = architecture.strip()
        event.set()
        return {"ok": True, "message": "Architecture recorded. Design system next."}

    return [submit_architecture]


def _make_design_system_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the design-system extraction phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_design_system(
        design_tokens: dict[str, object],
        component_map: dict[str, object],
        summary: str,
    ) -> dict[str, object]:
        """Record the design tokens + shadcn/ui component mapping and advance.

        Args:
            design_tokens: Typography/spacing/color/border/radius/shadow/
                breakpoints/container scales and button/input/card/nav variants.
            component_map: Reference UI pattern -> customized shadcn/ui
                primitive mapping (never default shadcn styling).
            summary: How the reference design maps through tokens → Tailwind /
                CSS variables → shadcn primitives → app components.
        """
        if not design_tokens or not component_map:
            return {
                "ok": False,
                "error": "design_tokens and component_map must not be empty.",
                "fix": "Extract the full design system and map patterns to shadcn/ui.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the token → Tailwind → shadcn → component pipeline.",
            }
        data["design_tokens"] = design_tokens
        data["component_map"] = component_map
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Design system recorded. Bootstrap next."}

    return [submit_design_system]


def _make_bootstrap_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the project-bootstrap phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def confirm_bootstrap_healthy(summary: str) -> dict[str, object]:
        """Confirm the Next.js foundation is healthy and advance.

        Args:
            summary: Evidence that TypeScript, Tailwind, shadcn/ui, TanStack
                Query, lint, format, dev server, and production build all work.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verified healthy foundation.",
            }
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Foundation healthy. Global shell next."}

    return [confirm_bootstrap_healthy]


def _make_global_shell_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the global-shell phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def confirm_global_shell(summary: str) -> dict[str, object]:
        """Confirm the global shell and advance.

        Args:
            summary: Root layout, header/nav/footer, global container,
                typography, theme, responsive navigation, global loading/error
                handling — visually close to the reference.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implemented global shell.",
            }
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Global shell confirmed. Component system next."}

    return [confirm_global_shell]


def _make_component_system_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the shared-component-system phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def confirm_component_system(summary: str) -> dict[str, object]:
        """Confirm the shared component library and advance.

        Args:
            summary: Repeated UI patterns implemented as reusable components
                over customized shadcn/ui primitives (Button, Card, Badge,
                Input, Select, Dialog, Dropdown, Tabs, Table, Pagination,
                Navbar, Sidebar, Breadcrumb, EmptyState, LoadingState,
                ErrorState, ...).
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the shared component library.",
            }
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Component system confirmed. Pages next."}

    return [confirm_component_system]


def _make_page_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tools that end one dynamic page-implementation phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def complete_page(page_route: str, summary: str) -> dict[str, object]:
        """Mark the current page route as implemented and advance to the next.

        Args:
            page_route: The exact route that was implemented.
            summary: Layout, content, responsive, interactions, data fetching,
                loading/error states implemented; discrepancies fixed.
        """
        if not page_route.strip() or not summary.strip():
            return {
                "ok": False,
                "error": "page_route and summary must not be empty.",
                "fix": "Implement the page fully, then call complete_page(route, summary).",
            }
        data["page_route"] = page_route.strip()
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": f"Page {page_route} complete. Next page or data layer."}

    return [complete_page]


def _make_data_layer_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the data-layer phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def confirm_data_layer(summary: str) -> dict[str, object]:
        """Confirm the TanStack Query data layer and advance.

        Args:
            summary: Query functions/keys/mutations/cache/loading/error/
                invalidation/pagination/infinite; data separated from
                presentation; clean mock/data abstraction when backend
                unavailable.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implemented data layer.",
            }
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Data layer confirmed. Responsive pass next."}

    return [confirm_data_layer]


def _make_responsive_pass_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the responsive pass."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def confirm_responsive(summary: str) -> dict[str, object]:
        """Confirm responsive behaviour across breakpoints and advance.

        Args:
            summary: Evidence that mobile/tablet/desktop/large-desktop behave
                correctly (nav, grids, typography, spacing, cards, tables,
                forms, images, overlays, menus) — not a shrunken desktop.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verified responsive behaviour.",
            }
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Responsive behaviour confirmed. Visual validation next."}

    return [confirm_responsive]


def _make_visual_validation_tools(
    event: asyncio.Event,
    data: dict[str, object],
    validator: Callable[[str], str | None] | None = None,
) -> list[Callable[..., object]]:
    """Return approve/reject tools with controlled re-entry for visual validation."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def visual_approved(summary: str) -> dict[str, object]:
        """Signal that the implementation visually matches the reference.

        Args:
            summary: Screenshot-comparison evidence per major route.
        """
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def visual_rejected(
        discrepancies: list[dict[str, object]],
        target_phase: str = "",
    ) -> dict[str, object]:
        """Signal that visual discrepancies were found and re-enter a phase.

        Args:
            discrepancies: One entry per discrepancy with keys: page, issue,
                severity, suggested_fix.
            target_phase: Earlier phase to re-enter (e.g. 'design_system',
                'global_shell', 'page'); empty = stay in this phase and retry.
        """
        target = target_phase.strip()
        if validator is not None and target:
            error = validator(target)
            if error is not None:
                return {
                    "ok": False,
                    "error": error,
                    "fix": "Use a valid phase from the active profile.",
                }
        data["action"] = "reject"
        data["discrepancies"] = discrepancies
        data["target_phase"] = target
        event.set()
        return {"ok": True}

    return [visual_approved, visual_rejected]


def _make_interaction_validation_tools(
    event: asyncio.Event,
    data: dict[str, object],
    validator: Callable[[str], str | None] | None = None,
) -> list[Callable[..., object]]:
    """Return approve/reject tools with controlled re-entry for interaction validation."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def interaction_approved(summary: str) -> dict[str, object]:
        """Signal that major user flows behave like the reference.

        Args:
            summary: Evidence for navigation, search, filtering, forms,
                dropdowns, dialogs, tabs, pagination, auth, data loading,
                error handling, responsive menu.
        """
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def interaction_rejected(
        discrepancies: list[dict[str, object]],
        target_phase: str = "",
    ) -> dict[str, object]:
        """Signal that interaction discrepancies were found and re-enter a phase.

        Args:
            discrepancies: One entry per discrepancy with keys: flow, issue,
                severity, suggested_fix.
            target_phase: Earlier phase to re-enter (e.g. 'data_layer',
                'page'); empty = stay in this phase and retry.
        """
        target = target_phase.strip()
        if validator is not None and target:
            error = validator(target)
            if error is not None:
                return {
                    "ok": False,
                    "error": error,
                    "fix": "Use a valid phase from the active profile.",
                }
        data["action"] = "reject"
        data["discrepancies"] = discrepancies
        data["target_phase"] = target
        event.set()
        return {"ok": True}

    return [interaction_approved, interaction_rejected]


def _make_accessibility_tools(
    event: asyncio.Event,
    data: dict[str, object],
    validator: Callable[[str], str | None] | None = None,
) -> list[Callable[..., object]]:
    """Return approve/reject tools with controlled re-entry for accessibility."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def a11y_approved(summary: str) -> dict[str, object]:
        """Signal that accessibility checks passed.

        Args:
            summary: Semantic HTML, keyboard nav, focus states, labels, ARIA,
                contrast, heading hierarchy, button/link semantics, form and
                dialog accessibility.
        """
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def a11y_rejected(
        issues: list[dict[str, object]],
        target_phase: str = "",
    ) -> dict[str, object]:
        """Signal that accessibility issues were found and re-enter a phase.

        Args:
            issues: One entry per issue with keys: page, issue, severity,
                suggested_fix.
            target_phase: Earlier phase to re-enter; empty = retry this phase.
        """
        target = target_phase.strip()
        if validator is not None and target:
            error = validator(target)
            if error is not None:
                return {
                    "ok": False,
                    "error": error,
                    "fix": "Use a valid phase from the active profile.",
                }
        data["action"] = "reject"
        data["issues"] = issues
        data["target_phase"] = target
        event.set()
        return {"ok": True}

    return [a11y_approved, a11y_rejected]


def _make_performance_tools(
    event: asyncio.Event,
    data: dict[str, object],
    validator: Callable[[str], str | None] | None = None,
) -> list[Callable[..., object]]:
    """Return approve/reject tools with controlled re-entry for performance."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def perf_approved(summary: str) -> dict[str, object]:
        """Signal that performance checks passed.

        Args:
            summary: Unnecessary client components/JS, image optimisation,
                caching, query behaviour, rendering perf, bundle size, layout
                shifts; Next.js features used appropriately.
        """
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def perf_rejected(
        issues: list[dict[str, object]],
        target_phase: str = "",
    ) -> dict[str, object]:
        """Signal that performance issues were found and re-enter a phase.

        Args:
            issues: One entry per issue with keys: area, issue, severity,
                suggested_fix.
            target_phase: Earlier phase to re-enter; empty = retry this phase.
        """
        target = target_phase.strip()
        if validator is not None and target:
            error = validator(target)
            if error is not None:
                return {
                    "ok": False,
                    "error": error,
                    "fix": "Use a valid phase from the active profile.",
                }
        data["action"] = "reject"
        data["issues"] = issues
        data["target_phase"] = target
        event.set()
        return {"ok": True}

    return [perf_approved, perf_rejected]


def _make_fidelity_pass_tools(
    event: asyncio.Event,
    data: dict[str, object],
    validator: Callable[[str], str | None] | None = None,
) -> list[Callable[..., object]]:
    """Return approve/reject tools with controlled re-entry for the fidelity pass."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def fidelity_approved(summary: str) -> dict[str, object]:
        """Signal that the final fidelity pass found no blocking discrepancies.

        Args:
            summary: Side-by-side polish check (2-4px spacing, font weights,
                line heights, radii, icon sizes, container widths, breakpoints,
                hover/loading states, variant consistency).
        """
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def fidelity_rejected(
        discrepancies: list[dict[str, object]],
        target_phase: str = "",
    ) -> dict[str, object]:
        """Signal that fidelity discrepancies remain and re-enter a phase.

        Args:
            discrepancies: One entry per discrepancy with keys: page, issue,
                severity, suggested_fix.
            target_phase: Earlier phase to re-enter; empty = retry this phase.
        """
        target = target_phase.strip()
        if validator is not None and target:
            error = validator(target)
            if error is not None:
                return {
                    "ok": False,
                    "error": error,
                    "fix": "Use a valid phase from the active profile.",
                }
        data["action"] = "reject"
        data["discrepancies"] = discrepancies
        data["target_phase"] = target
        event.set()
        return {"ok": True}

    return [fidelity_approved, fidelity_rejected]


def _make_final_validation_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tools that end final validation: COMPLETE or BLOCKED."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def final_approved(summary: str) -> dict[str, object]:
        """Signal that the reconstructed website passed final validation.

        Args:
            summary: Typecheck, lint, tests, production build, route
                validation, visual + interaction validation evidence.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the final validation evidence.",
            }
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Final validation passed. Workflow COMPLETED."}

    @tool_control
    @tool()
    async def final_blocked(issue: str) -> dict[str, object]:
        """Signal that a material blocker prevents completion.

        Args:
            issue: The concrete blocking issue that prevents completion.
        """
        if not issue.strip():
            return {
                "ok": False,
                "error": "issue must not be empty.",
                "fix": "Document the material blocker precisely.",
            }
        data["action"] = "block"
        data["issue"] = issue.strip()
        event.set()
        return {"ok": True, "message": "Blocker recorded. Workflow BLOCKED."}

    return [final_approved, final_blocked]


# ── infrastructure phase tool factories (sqlite → docs) ──────────


def _make_sqlite_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the sqlite phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_sqlite(summary: str) -> dict[str, object]:
        """Signal that the sqlite phase work is complete.

        Args:
            summary: What was implemented (schema init, deterministic seed data, reset/reseed workflow, application consumption, and no hard-coded records in components) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "sqlite phase submitted. Verification starts next."}

    return [submit_sqlite]


def _make_verify_sqlite_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_sqlite phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def sqlite_verified(summary: str) -> dict[str, object]:
        """Signal that sqlite verification passed.

        Args:
            summary: Verification evidence (Verify the SQLite database: schema init, deterministic seed, reset/reseed, application consumption, no hard-coded DB records in components, and tests/typecheck/lint/build.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "sqlite verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def sqlite_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that sqlite verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify the SQLite database: schema init, deterministic seed, reset/reseed, application consumption, no hard-coded DB records in components, and tests/typecheck/lint/build.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "sqlite rejected. Fix the issues and retry this phase."}

    return [sqlite_verified, sqlite_rejected]


def _make_prisma_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the prisma phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_prisma(summary: str) -> dict[str, object]:
        """Signal that the prisma phase work is complete.

        Args:
            summary: What was implemented (schema, client generation, migrations, seed, reset, relations, and types) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "prisma phase submitted. Verification starts next."}

    return [submit_prisma]


def _make_verify_prisma_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_prisma phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def prisma_verified(summary: str) -> dict[str, object]:
        """Signal that prisma verification passed.

        Args:
            summary: Verification evidence (Verify Prisma schema validation, client generation, migrations (fresh + existing), seed, reset, relations, types, integration, and build/typecheck/tests.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "prisma verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def prisma_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that prisma verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify Prisma schema validation, client generation, migrations (fresh + existing), seed, reset, relations, types, integration, and build/typecheck/tests.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "prisma rejected. Fix the issues and retry this phase."}

    return [prisma_verified, prisma_rejected]


def _make_tanstack_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the tanstack phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_tanstack(summary: str) -> dict[str, object]:
        """Signal that the tanstack phase work is complete.

        Args:
            summary: What was implemented (QueryClient/provider wiring, query + mutation functions, query keys, cache invalidation, loading/error/empty/mutation states, and Prisma kept server-only) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "tanstack phase submitted. Verification starts next."}

    return [submit_tanstack]


def _make_verify_tanstack_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_tanstack phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def tanstack_verified(summary: str) -> dict[str, object]:
        """Signal that tanstack verification passed.

        Args:
            summary: Verification evidence (Verify QueryClient/provider/query functions/mutations/keys/invalidation/loading/error/empty states, client/server boundaries, Prisma excluded from client bundles, and build/typecheck/tests.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "tanstack verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def tanstack_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that tanstack verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify QueryClient/provider/query functions/mutations/keys/invalidation/loading/error/empty states, client/server boundaries, Prisma excluded from client bundles, and build/typecheck/tests.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "tanstack rejected. Fix the issues and retry this phase."}

    return [tanstack_verified, tanstack_rejected]


def _make_env_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the env phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_env(summary: str) -> dict[str, object]:
        """Signal that the env phase work is complete.

        Args:
            summary: What was implemented (every variable documented (purpose, scope, how to generate/obtain, format, local vs prod) and no real secrets committed) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "env phase submitted. Verification starts next."}

    return [submit_env]


def _make_verify_env_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_env phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def env_verified(summary: str) -> dict[str, object]:
        """Signal that env verification passed.

        Args:
            summary: Verification evidence (Verify all four .env.* files exist, every variable is documented, no secrets, .gitignore behavior, names match code references, and build/startup works with the documented local config.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "env verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def env_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that env verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify all four .env.* files exist, every variable is documented, no secrets, .gitignore behavior, names match code references, and build/startup works with the documented local config.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "env rejected. Fix the issues and retry this phase."}

    return [env_verified, env_rejected]


def _make_docker_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the docker phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_docker(summary: str) -> dict[str, object]:
        """Signal that the docker phase work is complete.

        Args:
            summary: What was implemented (Dockerfile, compose files, .dockerignore, and docs/ Docker guide) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "docker phase submitted. Verification starts next."}

    return [submit_docker]


def _make_verify_docker_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_docker phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def docker_verified(summary: str) -> dict[str, object]:
        """Signal that docker verification passed.

        Args:
            summary: Verification evidence (Verify Docker/compose syntax, image build where available, startup, accessibility, logs, health checks, env, Prisma init, database/volume behavior, no secrets baked in, and that the documented commands work.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "docker verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def docker_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that docker verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify Docker/compose syntax, image build where available, startup, accessibility, logs, health checks, env, Prisma init, database/volume behavior, no secrets baked in, and that the documented commands work.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "docker rejected. Fix the issues and retry this phase."}

    return [docker_verified, docker_rejected]


def _make_netlify_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the netlify phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_netlify(summary: str) -> dict[str, object]:
        """Signal that the netlify phase work is complete.

        Args:
            summary: What was implemented (netlify.toml build/publish/redirects/headers/functions and the documented SQLite production limitation) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "netlify phase submitted. Verification starts next."}

    return [submit_netlify]


def _make_verify_netlify_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_netlify phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def netlify_verified(summary: str) -> dict[str, object]:
        """Signal that netlify verification passed.

        Args:
            summary: Verification evidence (Verify netlify.toml, build command, publish dir, redirects, headers, functions, plugins, env vars, prisma generation, production build, deployment compatibility, and the documented SQLite production limitation.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "netlify verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def netlify_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that netlify verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify netlify.toml, build command, publish dir, redirects, headers, functions, plugins, env vars, prisma generation, production build, deployment compatibility, and the documented SQLite production limitation.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "netlify rejected. Fix the issues and retry this phase."}

    return [netlify_verified, netlify_rejected]


def _make_caddy_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the caddy phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_caddy(summary: str) -> dict[str, object]:
        """Signal that the caddy phase work is complete.

        Args:
            summary: What was implemented (Caddyfile and the docs/ Caddy guide) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "caddy phase submitted. Verification starts next."}

    return [submit_caddy]


def _make_verify_caddy_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_caddy phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def caddy_verified(summary: str) -> dict[str, object]:
        """Signal that caddy verification passed.

        Args:
            summary: Verification evidence (Verify Caddy syntax, upstream address, ports, reverse proxy behavior, WebSocket behavior, Docker networking, HTTPS config, and docs.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "caddy verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def caddy_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that caddy verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify Caddy syntax, upstream address, ports, reverse proxy behavior, WebSocket behavior, Docker networking, HTTPS config, and docs.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "caddy rejected. Fix the issues and retry this phase."}

    return [caddy_verified, caddy_rejected]


def _make_package_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the package phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_package(summary: str) -> dict[str, object]:
        """Signal that the package phase work is complete.

        Args:
            summary: What was implemented (the new package.json scripts grouped by development, quality, database, Docker, and deployment) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "package phase submitted. Verification starts next."}

    return [submit_package]


def _make_verify_package_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_package phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def package_verified(summary: str) -> dict[str, object]:
        """Signal that package verification passed.

        Args:
            summary: Verification evidence (Verify every new command's executable exists, run non-destructive commands, lint, typecheck, tests, production build, Prisma commands, Docker commands, deployment prep, and package manager usage.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "package verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def package_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that package verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Verify every new command's executable exists, run non-destructive commands, lint, typecheck, tests, production build, Prisma commands, Docker commands, deployment prep, and package manager usage.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "package rejected. Fix the issues and retry this phase."}

    return [package_verified, package_rejected]


def _make_scripts_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the scripts phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_scripts(summary: str) -> dict[str, object]:
        """Signal that the scripts phase work is complete.

        Args:
            summary: What was implemented (the scripts created, what each does, and the docs/scripts.md reference) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "scripts phase submitted. Verification starts next."}

    return [submit_scripts]


def _make_verify_scripts_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_scripts phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def scripts_verified(summary: str) -> dict[str, object]:
        """Signal that scripts verification passed.

        Args:
            summary: Verification evidence (Run 'bash -n scripts/*.sh', verify executable permissions, run relevant scripts, test from different working dirs, test missing-prereq behavior, verify destructive safeguards, search for secrets, and verify docs.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "scripts verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def scripts_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that scripts verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Run 'bash -n scripts/*.sh', verify executable permissions, run relevant scripts, test from different working dirs, test missing-prereq behavior, verify destructive safeguards, search for secrets, and verify docs.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "scripts rejected. Fix the issues and retry this phase."}

    return [scripts_verified, scripts_rejected]


def _make_docs_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the docs phase with the implementation summary."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_docs(summary: str) -> dict[str, object]:
        """Signal that the docs phase work is complete.

        Args:
            summary: What was implemented (the docs tree written and the real commands/paths used) plus how to run/verify it locally.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the implementation before advancing.",
            }
        data["action"] = "submit"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "docs phase submitted. Verification starts next."}

    return [submit_docs]


def _make_verify_docs_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return verified/rejected tools for the verify_docs phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def docs_verified(summary: str) -> dict[str, object]:
        """Signal that docs verification passed.

        Args:
            summary: Verification evidence (Audit the docs: enumerate source/config/infra files, compare against docs, find undocumented systems, outdated commands, incorrect paths, missing env vars, missing scripts, missing deployment config; verify commands and paths exist; check contradictions; re-read as a new developer.)
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the verification evidence before advancing.",
            }
        data["action"] = "verified"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "docs verified. Moving to the next phase."}

    @tool_control
    @tool()
    async def docs_rejected(issues: list[str]) -> dict[str, object]:
        """Signal that docs verification found issues to fix before retrying.

        Args:
            issues: Concrete problems found (Audit the docs: enumerate source/config/infra files, compare against docs, find undocumented systems, outdated commands, incorrect paths, missing env vars, missing scripts, missing deployment config; verify commands and paths exist; check contradictions; re-read as a new developer.)
        """
        if not issues:
            return {
                "ok": False,
                "error": "issues must not be empty.",
                "fix": "List the concrete verification failures.",
            }
        data["action"] = "rejected"
        data["issues"] = issues
        event.set()
        return {"ok": True, "message": "docs rejected. Fix the issues and retry this phase."}

    return [docs_verified, docs_rejected]


class ReconstructSiteRunner(CodePlanRunner):
    """State-machine runner for reconstruct_site.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none
    of code_plan's own phases execute — this runner owns the whole flow.
    """

    workflow_name = "reconstruct_site"
    total_phases = 39

    async def run(self, intent: str) -> ReconstructContext:
        """Drive the 39-phase graph with controlled PAGE repetition."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        ctx = ReconstructContext(
            intent=intent,
            run_id=run_id,
            state=ReconstructState.INIT,
            shared_memory=memory,
        )
        if handle is not None:
            handle.attach_context(ctx)
        state = ctx.state

        while not state.is_terminal:
            ctx.state = state
            ctx.phase_iteration += 1
            if handle is not None:
                handle.attach_context(ctx)
                handle.update_phase(
                    state.name.lower(), self._phase_index(state), ctx.phase_iteration
                )
            match state:
                case ReconstructState.INIT:
                    state = await self._init(ctx, memory)
                case ReconstructState.RECON:
                    state = await self._recon(ctx, memory)
                case ReconstructState.VISUAL_RESEARCH:
                    state = await self._visual_research(ctx, memory)
                case ReconstructState.INTERACTION_ANALYSIS:
                    state = await self._interaction_analysis(ctx, memory)
                case ReconstructState.CONTENT_ASSETS:
                    state = await self._content_assets(ctx, memory)
                case ReconstructState.ARCHITECTURE:
                    state = await self._architecture(ctx, memory)
                case ReconstructState.DESIGN_SYSTEM:
                    state = await self._design_system(ctx, memory)
                case ReconstructState.BOOTSTRAP:
                    state = await self._bootstrap(ctx, memory)
                case ReconstructState.GLOBAL_SHELL:
                    state = await self._global_shell(ctx, memory)
                case ReconstructState.COMPONENT_SYSTEM:
                    state = await self._component_system(ctx, memory)
                case ReconstructState.PAGE:
                    state = await self._page(ctx, memory)
                case ReconstructState.DATA_LAYER:
                    state = await self._data_layer(ctx, memory)
                case ReconstructState.RESPONSIVE_PASS:
                    state = await self._responsive_pass(ctx, memory)
                case ReconstructState.VISUAL_VALIDATION:
                    state = await self._visual_validation(ctx, memory)
                case ReconstructState.INTERACTION_VALIDATION:
                    state = await self._interaction_validation(ctx, memory)
                case ReconstructState.ACCESSIBILITY:
                    state = await self._accessibility(ctx, memory)
                case ReconstructState.PERFORMANCE:
                    state = await self._performance(ctx, memory)
                case ReconstructState.FIDELITY_PASS:
                    state = await self._fidelity_pass(ctx, memory)
                case ReconstructState.SQLITE_DB:
                    state = await self._sqlite_db(ctx, memory)
                case ReconstructState.VERIFY_SQLITE:
                    state = await self._verify_sqlite(ctx, memory)
                case ReconstructState.PRISMA:
                    state = await self._prisma(ctx, memory)
                case ReconstructState.VERIFY_PRISMA:
                    state = await self._verify_prisma(ctx, memory)
                case ReconstructState.TANSTACK_QUERY:
                    state = await self._tanstack_query(ctx, memory)
                case ReconstructState.VERIFY_TANSTACK:
                    state = await self._verify_tanstack(ctx, memory)
                case ReconstructState.ENV_CONFIG:
                    state = await self._env_config(ctx, memory)
                case ReconstructState.VERIFY_ENV:
                    state = await self._verify_env(ctx, memory)
                case ReconstructState.DOCKER:
                    state = await self._docker(ctx, memory)
                case ReconstructState.VERIFY_DOCKER:
                    state = await self._verify_docker(ctx, memory)
                case ReconstructState.NETLIFY:
                    state = await self._netlify(ctx, memory)
                case ReconstructState.VERIFY_NETLIFY:
                    state = await self._verify_netlify(ctx, memory)
                case ReconstructState.CADDY:
                    state = await self._caddy(ctx, memory)
                case ReconstructState.VERIFY_CADDY:
                    state = await self._verify_caddy(ctx, memory)
                case ReconstructState.PACKAGE_COMMANDS:
                    state = await self._package_commands(ctx, memory)
                case ReconstructState.VERIFY_PACKAGE:
                    state = await self._verify_package(ctx, memory)
                case ReconstructState.SCRIPTS:
                    state = await self._scripts(ctx, memory)
                case ReconstructState.VERIFY_SCRIPTS:
                    state = await self._verify_scripts(ctx, memory)
                case ReconstructState.DOCS:
                    state = await self._docs(ctx, memory)
                case ReconstructState.VERIFY_DOCS:
                    state = await self._verify_docs(ctx, memory)
                case ReconstructState.FINAL_VALIDATION:
                    state = await self._final_validation(ctx, memory)
            log.info("reconstruct_site → %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> ReconstructContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, ReconstructContext):
            raise TypeError("reconstruct_site resume requires ReconstructContext")
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else context.shared_memory
        )
        if memory is None:
            memory = ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        context.shared_memory = memory
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
        state = context.state
        while not state.is_terminal:
            context.state = state
            context.phase_iteration += 1
            if handle is not None:
                handle.attach_context(context)
                handle.update_phase(
                    state.name.lower(), self._phase_index(state), context.phase_iteration
                )
            match state:
                case ReconstructState.INIT:
                    state = await self._init(context, memory)
                case ReconstructState.RECON:
                    state = await self._recon(context, memory)
                case ReconstructState.VISUAL_RESEARCH:
                    state = await self._visual_research(context, memory)
                case ReconstructState.INTERACTION_ANALYSIS:
                    state = await self._interaction_analysis(context, memory)
                case ReconstructState.CONTENT_ASSETS:
                    state = await self._content_assets(context, memory)
                case ReconstructState.ARCHITECTURE:
                    state = await self._architecture(context, memory)
                case ReconstructState.DESIGN_SYSTEM:
                    state = await self._design_system(context, memory)
                case ReconstructState.BOOTSTRAP:
                    state = await self._bootstrap(context, memory)
                case ReconstructState.GLOBAL_SHELL:
                    state = await self._global_shell(context, memory)
                case ReconstructState.COMPONENT_SYSTEM:
                    state = await self._component_system(context, memory)
                case ReconstructState.PAGE:
                    state = await self._page(context, memory)
                case ReconstructState.DATA_LAYER:
                    state = await self._data_layer(context, memory)
                case ReconstructState.RESPONSIVE_PASS:
                    state = await self._responsive_pass(context, memory)
                case ReconstructState.VISUAL_VALIDATION:
                    state = await self._visual_validation(context, memory)
                case ReconstructState.INTERACTION_VALIDATION:
                    state = await self._interaction_validation(context, memory)
                case ReconstructState.ACCESSIBILITY:
                    state = await self._accessibility(context, memory)
                case ReconstructState.PERFORMANCE:
                    state = await self._performance(context, memory)
                case ReconstructState.FIDELITY_PASS:
                    state = await self._fidelity_pass(context, memory)
                case ReconstructState.SQLITE_DB:
                    state = await self._sqlite_db(context, memory)
                case ReconstructState.VERIFY_SQLITE:
                    state = await self._verify_sqlite(context, memory)
                case ReconstructState.PRISMA:
                    state = await self._prisma(context, memory)
                case ReconstructState.VERIFY_PRISMA:
                    state = await self._verify_prisma(context, memory)
                case ReconstructState.TANSTACK_QUERY:
                    state = await self._tanstack_query(context, memory)
                case ReconstructState.VERIFY_TANSTACK:
                    state = await self._verify_tanstack(context, memory)
                case ReconstructState.ENV_CONFIG:
                    state = await self._env_config(context, memory)
                case ReconstructState.VERIFY_ENV:
                    state = await self._verify_env(context, memory)
                case ReconstructState.DOCKER:
                    state = await self._docker(context, memory)
                case ReconstructState.VERIFY_DOCKER:
                    state = await self._verify_docker(context, memory)
                case ReconstructState.NETLIFY:
                    state = await self._netlify(context, memory)
                case ReconstructState.VERIFY_NETLIFY:
                    state = await self._verify_netlify(context, memory)
                case ReconstructState.CADDY:
                    state = await self._caddy(context, memory)
                case ReconstructState.VERIFY_CADDY:
                    state = await self._verify_caddy(context, memory)
                case ReconstructState.PACKAGE_COMMANDS:
                    state = await self._package_commands(context, memory)
                case ReconstructState.VERIFY_PACKAGE:
                    state = await self._verify_package(context, memory)
                case ReconstructState.SCRIPTS:
                    state = await self._scripts(context, memory)
                case ReconstructState.VERIFY_SCRIPTS:
                    state = await self._verify_scripts(context, memory)
                case ReconstructState.DOCS:
                    state = await self._docs(context, memory)
                case ReconstructState.VERIFY_DOCS:
                    state = await self._verify_docs(context, memory)
                case ReconstructState.FINAL_VALIDATION:
                    state = await self._final_validation(context, memory)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: ReconstructState) -> int:
        return {
            ReconstructState.INIT: 0,
            ReconstructState.RECON: 1,
            ReconstructState.VISUAL_RESEARCH: 2,
            ReconstructState.INTERACTION_ANALYSIS: 3,
            ReconstructState.CONTENT_ASSETS: 4,
            ReconstructState.ARCHITECTURE: 5,
            ReconstructState.DESIGN_SYSTEM: 6,
            ReconstructState.BOOTSTRAP: 7,
            ReconstructState.GLOBAL_SHELL: 8,
            ReconstructState.COMPONENT_SYSTEM: 9,
            ReconstructState.PAGE: 10,
            ReconstructState.DATA_LAYER: 11,
            ReconstructState.RESPONSIVE_PASS: 12,
            ReconstructState.VISUAL_VALIDATION: 13,
            ReconstructState.INTERACTION_VALIDATION: 14,
            ReconstructState.ACCESSIBILITY: 15,
            ReconstructState.PERFORMANCE: 16,
            ReconstructState.FIDELITY_PASS: 17,
            ReconstructState.SQLITE_DB: 18,
            ReconstructState.VERIFY_SQLITE: 19,
            ReconstructState.PRISMA: 20,
            ReconstructState.VERIFY_PRISMA: 21,
            ReconstructState.TANSTACK_QUERY: 22,
            ReconstructState.VERIFY_TANSTACK: 23,
            ReconstructState.ENV_CONFIG: 24,
            ReconstructState.VERIFY_ENV: 25,
            ReconstructState.DOCKER: 26,
            ReconstructState.VERIFY_DOCKER: 27,
            ReconstructState.NETLIFY: 28,
            ReconstructState.VERIFY_NETLIFY: 29,
            ReconstructState.CADDY: 30,
            ReconstructState.VERIFY_CADDY: 31,
            ReconstructState.PACKAGE_COMMANDS: 32,
            ReconstructState.VERIFY_PACKAGE: 33,
            ReconstructState.SCRIPTS: 34,
            ReconstructState.VERIFY_SCRIPTS: 35,
            ReconstructState.DOCS: 36,
            ReconstructState.VERIFY_DOCS: 37,
            ReconstructState.FINAL_VALIDATION: 38,
        }.get(state, 0)

    # ── discovery / research / analysis phase methods ────────────────────────

    async def _init(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_initial_state fires; return RECON or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=ctx.intent if attempt == 1 else "Call submit_initial_state(...) now.",
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the INIT phase of reconstruct_site. Collect the initial "
                    "workflow state: reference_url, target_directory, project constraints, "
                    "desired routes, auth requirements, whether the reference is static or "
                    "dynamic, and whether API behaviour should be reproduced or mocked. "
                    "Verify the reference is accessible (browser tools). Ask the user via "
                    "ask_user if required inputs are missing. Then call "
                    "submit_initial_state(reference_url, target_directory, constraints, "
                    "desired_routes, auth_required, reference_is_static, "
                    "reproduce_api_or_mock). Only a successful transition-tool call changes "
                    "phase; prose such as 'done' never advances the workflow. Do NOT start "
                    "coding yet."
                ),
                mode="Yolo",
                max_turns=10,
                shared_memory=memory,
                tools=_make_init_tools(event, data),
            )
            if event.is_set():
                ctx.target_url = str(data.get("reference_url", ""))
                ctx.target_directory = str(data.get("target_directory", ""))
                ctx.artifacts["initial_state"] = (
                    f"url={ctx.target_url}; dir={ctx.target_directory}; "
                    f"static={data.get('reference_is_static')}; "
                    f"api={data.get('reproduce_api_or_mock')}"
                )
                ctx.completed_phases.append("init")
                ctx.last_transition = "submit_initial_state"
                return ReconstructState.RECON

        ctx.fail_reason = "init phase never called submit_initial_state()"
        return ReconstructState.FAILED

    async def _recon(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_route_inventory fires; return VISUAL_RESEARCH or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Reference URL: {ctx.target_url}\n\nPerform reconnaissance."
                    if attempt == 1
                    else "Call submit_route_inventory(routes, summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the RECON phase of reconstruct_site. Thoroughly inspect the "
                    "reference website with the browser tools: homepage, every discoverable "
                    "route, navigation, footer, menus, dropdowns, modals, forms, search, "
                    "filters, tabs, cards, tables, pagination, loading/error/empty states, "
                    "auth UI, responsive behaviour, interactive elements. Build a route "
                    "inventory (route, purpose, layout, major components, interactions, data "
                    "requirements, responsive considerations) and record the site's "
                    "information architecture. Then call "
                    "submit_route_inventory(routes, summary). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow. "
                    "Do NOT start coding — this is discovery only."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_recon_tools(event, data),
            )
            if event.is_set():
                raw_routes = data.get("routes", [])
                if isinstance(raw_routes, list):
                    ctx.route_inventory = [r for r in raw_routes if isinstance(r, dict)]
                    ctx.pages_to_implement = [
                        str(r.get("route", "")).strip()
                        for r in ctx.route_inventory
                        if str(r.get("route", "")).strip()
                    ]
                ctx.artifacts["route_inventory"] = "route_inventory.md"
                ctx.completed_phases.append("recon")
                ctx.last_transition = "submit_route_inventory"
                return ReconstructState.VISUAL_RESEARCH

        ctx.fail_reason = "recon phase never called submit_route_inventory()"
        return ReconstructState.FAILED

    async def _visual_research(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_visual_spec fires; return INTERACTION_ANALYSIS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Reference URL: {ctx.target_url}\n\nCapture screenshots and measure "
                    "the visual design."
                    if attempt == 1
                    else "Call submit_visual_spec(design_tokens, summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VISUAL_RESEARCH phase of reconstruct_site. Capture "
                    "reference screenshots for every major route at mobile, tablet, and "
                    "desktop viewports when browser access is available. After each "
                    "playwright_screenshot or cloakbrowser_screenshot call, pass its returned "
                    "artifact id and path to record_reconstruct_screenshot with role "
                    "'reference', route, viewport, dimensions, and page state. Then analyse "
                    "the reference's typography "
                    "(sizes/weights/line-heights), spacing, containers, grid systems, "
                    "colors, borders, shadows, radii, iconography, image treatment, "
                    "navigation, cards, buttons, forms, tables, overlays, and responsive "
                    "layouts. Extract CONCRETE measured observations (e.g. max-width ≈ X, "
                    "card radius ≈ X, padding ≈ Y) — never vague 'clean modern design'. "
                    "Then call submit_visual_spec(design_tokens, summary). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_visual_research_tools(event, data),
            )
            if event.is_set():
                tokens = data.get("design_tokens", {})
                if isinstance(tokens, dict):
                    ctx.design_tokens = tokens
                ctx.artifacts["visual_spec"] = "visual_spec.md"
                ctx.completed_phases.append("visual_research")
                ctx.last_transition = "submit_visual_spec"
                return ReconstructState.INTERACTION_ANALYSIS

        ctx.fail_reason = "visual_research phase never called submit_visual_spec()"
        return ReconstructState.FAILED

    async def _interaction_analysis(
        self, ctx: ReconstructContext, memory: object
    ) -> ReconstructState:
        """Loop until submit_interaction_inventory fires; return CONTENT_ASSETS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Reference URL: {ctx.target_url}\n\nAnalyse how the site actually behaves."
                    if attempt == 1
                    else "Call submit_interaction_inventory(interactions, summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the INTERACTION_ANALYSIS phase of reconstruct_site. "
                    "Determine how the website actually behaves: hover/focus/active states, "
                    "dropdown behaviour, navigation transitions, modals, drawers, "
                    "accordions, tabs, carousels, forms and validation, loading states, API "
                    "requests, infinite scrolling, pagination, URL state, query parameters, "
                    "animations, keyboard interactions. Document each as: interaction, "
                    "trigger, expected behaviour, visual state, data dependency, "
                    "URL/state change. Reproduce the EXPERIENCE, not just the static "
                    "appearance. Then call submit_interaction_inventory(interactions, "
                    "summary). Only a successful transition-tool call changes phase; prose "
                    "never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_interaction_analysis_tools(event, data),
            )
            if event.is_set():
                raw = data.get("interactions", [])
                if isinstance(raw, list):
                    ctx.interaction_inventory = [i for i in raw if isinstance(i, dict)]
                ctx.artifacts["interaction_inventory"] = "interaction_inventory.md"
                ctx.completed_phases.append("interaction_analysis")
                ctx.last_transition = "submit_interaction_inventory"
                return ReconstructState.CONTENT_ASSETS

        ctx.fail_reason = "interaction_analysis never called submit_interaction_inventory()"
        return ReconstructState.FAILED

    async def _content_assets(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_asset_inventory fires; return ARCHITECTURE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Reference URL: {ctx.target_url}\n\nInventory content and assets."
                    if attempt == 1
                    else "Call submit_asset_inventory(assets, summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the CONTENT_ASSETS phase of reconstruct_site. Inventory "
                    "logos, icons, images, illustrations, fonts, videos, SVGs, backgrounds, "
                    "badges, avatars, and placeholder assets. Measure real dimensions and "
                    "formats — never invent them. Classify each asset as reusable "
                    "legitimately or to be recreated (note the legal basis), and identify an "
                    "equivalent or implementation strategy for assets that must not be "
                    "copied. Then call submit_asset_inventory(assets, summary). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_content_assets_tools(event, data),
            )
            if event.is_set():
                raw = data.get("assets", [])
                if isinstance(raw, list):
                    ctx.asset_inventory = [a for a in raw if isinstance(a, dict)]
                ctx.artifacts["asset_inventory"] = "asset_inventory.md"
                ctx.completed_phases.append("content_assets")
                ctx.last_transition = "submit_asset_inventory"
                return ReconstructState.ARCHITECTURE

        ctx.fail_reason = "content_assets phase never called submit_asset_inventory()"
        return ReconstructState.FAILED

    async def _architecture(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_architecture fires; return DESIGN_SYSTEM or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Design the target Next.js application architecture before implementation."
                    if attempt == 1
                    else "Call submit_architecture(architecture) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the ARCHITECTURE phase of reconstruct_site. Design the "
                    "target Next.js App Router application: server vs client components, "
                    "route structure, layouts, loading boundaries, error boundaries, "
                    "data-fetching architecture, TanStack Query keys, API abstraction, "
                    "state management, reusable components, and shared UI primitives. "
                    "Write the architecture document, then call "
                    "submit_architecture(architecture). Only a successful transition-tool "
                    "call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_architecture_tools(event, data),
            )
            if event.is_set():
                ctx.architecture = str(data.get("architecture", ""))
                ctx.artifacts["architecture"] = "architecture.md"
                ctx.completed_phases.append("architecture")
                ctx.last_transition = "submit_architecture"
                return ReconstructState.DESIGN_SYSTEM

        ctx.fail_reason = "architecture phase never called submit_architecture()"
        return ReconstructState.FAILED

    async def _design_system(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_design_system fires; return BOOTSTRAP or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Translate the reference visual language into a reusable design system."
                    if attempt == 1
                    else "Call submit_design_system(design_tokens, component_map, summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the DESIGN_SYSTEM phase of reconstruct_site. Translate the "
                    "reference website's visual language into a reusable design system: "
                    "typography scale, spacing scale, colors, borders, radius, shadows, "
                    "breakpoints, container widths, button variants, input styles, card "
                    "styles, navigation styles. Map reusable UI elements onto customized "
                    "shadcn/ui primitives (never default styling) so the result matches the "
                    "reference. Follow the pipeline: reference design → design tokens → "
                    "Tailwind configuration / CSS variables → shadcn/ui primitives → "
                    "reusable application components. Then call "
                    "submit_design_system(design_tokens, component_map, summary). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_design_system_tools(event, data),
            )
            if event.is_set():
                tokens = data.get("design_tokens", {})
                if isinstance(tokens, dict):
                    ctx.design_tokens = tokens
                comp_map = data.get("component_map", {})
                if isinstance(comp_map, dict):
                    ctx.component_inventory = [
                        {"pattern": k, "primitive": v} for k, v in comp_map.items()
                    ]
                ctx.artifacts["design_system"] = "design_system.md"
                ctx.completed_phases.append("design_system")
                ctx.last_transition = "submit_design_system"
                return ReconstructState.BOOTSTRAP

        ctx.fail_reason = "design_system phase never called submit_design_system()"
        return ReconstructState.FAILED

    async def _bootstrap(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until confirm_bootstrap_healthy fires; return GLOBAL_SHELL or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Scaffold the Next.js project in {ctx.target_directory or '<target_dir>'}."
                    if attempt == 1
                    else "Call confirm_bootstrap_healthy(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the BOOTSTRAP phase of reconstruct_site. Create or "
                    "configure the Next.js project (App Router, TypeScript, Tailwind CSS, "
                    "shadcn/ui, TanStack Query, lint, format). Verify TypeScript compiles, "
                    "Tailwind works, shadcn/ui is configured, TanStack Query is installed "
                    "and configured, linting and formatting work, the dev server starts, "
                    "and a production build succeeds. Do NOT proceed until the foundation "
                    "is healthy. Then call confirm_bootstrap_healthy(summary). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_bootstrap_tools(event, data),
            )
            if event.is_set():
                ctx.artifacts["bootstrap"] = str(data.get("summary", ""))
                ctx.completed_phases.append("bootstrap")
                ctx.last_transition = "confirm_bootstrap_healthy"
                return ReconstructState.GLOBAL_SHELL

        ctx.fail_reason = "bootstrap phase never called confirm_bootstrap_healthy()"
        return ReconstructState.FAILED

    async def _global_shell(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until confirm_global_shell fires; return COMPONENT_SYSTEM or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Implement the global shell: root layout, header, navigation, footer, "
                    "global container, typography, theme, responsive navigation, global "
                    "loading/error handling."
                    if attempt == 1
                    else "Call confirm_global_shell(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the GLOBAL_SHELL phase of reconstruct_site. Implement the "
                    "root layout, header, navigation, footer, global container, typography, "
                    "theme, responsive navigation, and global loading/error handling. The "
                    "global shell should be visually close to the reference BEFORE "
                    "implementing individual pages. Then call "
                    "confirm_global_shell(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_global_shell_tools(event, data),
            )
            if event.is_set():
                ctx.artifacts["global_shell"] = str(data.get("summary", ""))
                ctx.completed_phases.append("global_shell")
                ctx.last_transition = "confirm_global_shell"
                return ReconstructState.COMPONENT_SYSTEM

        ctx.fail_reason = "global_shell phase never called confirm_global_shell()"
        return ReconstructState.FAILED

    async def _component_system(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until confirm_component_system fires; return PAGE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Implement the shared component system over customized shadcn/ui primitives."
                    if attempt == 1
                    else "Call confirm_component_system(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the COMPONENT_SYSTEM phase of reconstruct_site. Identify "
                    "repeated UI patterns and implement reusable components (Button, Card, "
                    "Badge, Input, Select, Dialog, Dropdown, Tabs, Table, Pagination, "
                    "Navbar, Sidebar, Breadcrumb, EmptyState, LoadingState, ErrorState). "
                    "Prefer customized shadcn/ui primitives; do not create one-off "
                    "implementations for clearly repeating patterns. Then call "
                    "confirm_component_system(summary). Only a successful transition-tool "
                    "call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_component_system_tools(event, data),
            )
            if event.is_set():
                ctx.artifacts["component_system"] = str(data.get("summary", ""))
                ctx.completed_phases.append("component_system")
                ctx.last_transition = "confirm_component_system"
                return ReconstructState.PAGE

        ctx.fail_reason = "component_system phase never called confirm_component_system()"
        return ReconstructState.FAILED

    # ── implementation phase methods ──────────────────────────────────────────

    async def _page(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Implement one route per entry; loop until all pages done → DATA_LAYER.

        Re-enters PAGE once per route in ``pages_to_implement``; ``page_index``
        is the durable cursor so an interrupted run resumes at the exact route.
        """
        if not ctx.pages_to_implement:
            # No routes discovered — nothing to implement.
            ctx.completed_phases.append("page")
            ctx.last_transition = "complete_page(no routes)"
            return ReconstructState.DATA_LAYER

        if ctx.page_index >= len(ctx.pages_to_implement):
            ctx.completed_phases.append("page")
            ctx.last_transition = "complete_page(all routes)"
            return ReconstructState.DATA_LAYER

        route = ctx.pages_to_implement[ctx.page_index]
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Implement route {route} (page {ctx.page_index + 1} of "
                    f"{len(ctx.pages_to_implement)}).\nReference URL: {ctx.target_url}"
                    if attempt == 1
                    else f"Call complete_page('{route}', summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    f"You are in the PAGE phase of reconstruct_site implementing route "
                    f"'{route}'. Re-inspect the corresponding reference page, then "
                    f"implement: layout, content, responsive behaviour, interactions, data "
                    f"fetching, loading states, and error states. Compare visually and fix "
                    f"discrepancies. A page is NOT complete merely because it renders. "
                    f"Then call complete_page(page_route, summary) with page_route='{route}'. "
                    f"Only a successful transition-tool call changes phase; prose never "
                    f"advances the workflow."
                ),
                mode="Yolo",
                max_turns=40,
                shared_memory=memory,
                tools=_make_page_tools(event, data),
            )
            if event.is_set():
                done_route = str(data.get("page_route", route))
                ctx.implementation_status[done_route] = "implemented"
                ctx.page_index += 1
                ctx.last_transition = f"complete_page({done_route})"
                return ReconstructState.PAGE  # re-enter for the next route

        ctx.fail_reason = f"page phase never completed route {route}"
        return ReconstructState.FAILED

    async def _data_layer(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until confirm_data_layer fires; return RESPONSIVE_PASS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Implement the TanStack Query data layer."
                    if attempt == 1
                    else "Call confirm_data_layer(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the DATA_LAYER phase of reconstruct_site. Where the "
                    "reference uses dynamic data, reproduce the behaviour with TanStack "
                    "Query: query functions, query keys, mutations, cache behaviour, "
                    "loading states, error handling, invalidation, pagination/infinite "
                    "queries. Keep data access separate from presentation (UI → TanStack "
                    "Query → API abstraction → backend/API). If the real backend is "
                    "unavailable, create a clean mock/data abstraction rather than "
                    "hardcoding API behaviour into components. Then call "
                    "confirm_data_layer(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_data_layer_tools(event, data),
            )
            if event.is_set():
                ctx.artifacts["data_layer"] = str(data.get("summary", ""))
                ctx.completed_phases.append("data_layer")
                ctx.last_transition = "confirm_data_layer"
                return ReconstructState.RESPONSIVE_PASS

        ctx.fail_reason = "data_layer phase never called confirm_data_layer()"
        return ReconstructState.FAILED

    async def _responsive_pass(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until confirm_responsive fires; return VISUAL_VALIDATION or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Test the implementation across mobile, tablet, desktop, and large "
                    "desktop breakpoints."
                    if attempt == 1
                    else "Call confirm_responsive(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the RESPONSIVE_PASS phase of reconstruct_site. Test the "
                    "implementation across mobile, tablet, desktop, and large desktop. Do "
                    "NOT simply shrink the desktop version — determine how the reference "
                    "actually changes at each breakpoint. Check navigation, grids, "
                    "typography, spacing, cards, tables, forms, images, overlays, and "
                    "menus. Then call confirm_responsive(summary). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_responsive_pass_tools(event, data),
            )
            if event.is_set():
                ctx.artifacts["responsive"] = str(data.get("summary", ""))
                ctx.completed_phases.append("responsive_pass")
                ctx.last_transition = "confirm_responsive"
                return ReconstructState.VISUAL_VALIDATION

        ctx.fail_reason = "responsive_pass phase never called confirm_responsive()"
        return ReconstructState.FAILED

    # ── validation phase methods (with controlled re-entry) ──────────────────

    async def _visual_validation(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until visual_approved/rejected fires; may re-enter an earlier phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Compare the implementation against the reference using screenshots "
                    "for each major route."
                    if attempt == 1
                    else "Call visual_approved(summary) or visual_rejected(discrepancies, target_phase) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VISUAL_VALIDATION phase of reconstruct_site. Compare "
                    "the implementation against the reference using screenshots for each "
                    "major route at the same mobile, tablet, and desktop viewports. Capture "
                    "implementation screenshots with playwright_screenshot or "
                    "cloakbrowser_screenshot and pass each returned artifact id and path to "
                    "record_reconstruct_screenshot with role 'implementation'. Compare "
                    "dimensions, positioning, spacing, typography, colors, "
                    "borders, shadows, images, alignment, responsive behaviour. Use "
                    "screenshot/image-comparison tooling where available; do not rely "
                    "solely on subjective inspection. If everything matches, call "
                    "visual_approved(summary). If discrepancies exist, call "
                    "visual_rejected(discrepancies, target_phase) — set target_phase to "
                    "the earliest phase that should be re-entered (e.g. 'design_system', "
                    "'global_shell', 'page', or '' to retry here). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_visual_validation_tools(event, data, self._validate_reentry_target),
            )
            if event.is_set():
                action = str(data.get("action", "reject"))
                ctx.validation_status["visual"] = "approved" if action == "approve" else "rejected"
                ctx.last_transition = (
                    "visual_approved" if action == "approve" else "visual_rejected"
                )
                if action == "approve":
                    ctx.completed_phases.append("visual_validation")
                    return ReconstructState.INTERACTION_VALIDATION
                raw = data.get("discrepancies", [])
                if isinstance(raw, list):
                    ctx.visual_discrepancies.extend(d for d in raw if isinstance(d, dict))
                target = str(data.get("target_phase", "")).strip()
                if target:
                    ctx.known_issues.append(
                        {
                            "phase": "visual_validation",
                            "issue": f"re-entering {target}",
                            "severity": "high",
                        }
                    )
                    return self._reentry_state(target)
                continue  # retry this phase

        ctx.fail_reason = "visual_validation never reported a verdict"
        return ReconstructState.FAILED

    async def _interaction_validation(
        self, ctx: ReconstructContext, memory: object
    ) -> ReconstructState:
        """Loop until interaction_approved/rejected fires; may re-enter earlier phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Test the major user flows against the reference behaviour."
                    if attempt == 1
                    else "Call interaction_approved(summary) or interaction_rejected(discrepancies, target_phase) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the INTERACTION_VALIDATION phase of reconstruct_site. Test "
                    "the major user flows: navigation, search, filtering, forms, "
                    "dropdowns, dialogs, tabs, pagination, authentication, data loading, "
                    "error handling, responsive menu. Verify each behaves like the "
                    "reference. If all pass, call interaction_approved(summary). If "
                    "discrepancies exist, call interaction_rejected(discrepancies, "
                    "target_phase) — set target_phase to the earliest phase to re-enter "
                    "(e.g. 'data_layer', 'page', or '' to retry here). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_interaction_validation_tools(
                    event, data, self._validate_reentry_target
                ),
            )
            if event.is_set():
                action = str(data.get("action", "reject"))
                ctx.validation_status["interaction"] = (
                    "approved" if action == "approve" else "rejected"
                )
                ctx.last_transition = (
                    "interaction_approved" if action == "approve" else "interaction_rejected"
                )
                if action == "approve":
                    ctx.completed_phases.append("interaction_validation")
                    return ReconstructState.ACCESSIBILITY
                raw = data.get("discrepancies", [])
                if isinstance(raw, list):
                    ctx.interaction_discrepancies.extend(d for d in raw if isinstance(d, dict))
                target = str(data.get("target_phase", "")).strip()
                if target:
                    ctx.known_issues.append(
                        {
                            "phase": "interaction_validation",
                            "issue": f"re-entering {target}",
                            "severity": "high",
                        }
                    )
                    return self._reentry_state(target)
                continue

        ctx.fail_reason = "interaction_validation never reported a verdict"
        return ReconstructState.FAILED

    async def _accessibility(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until a11y_approved/rejected fires; may re-enter earlier phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Check accessibility: semantic HTML, keyboard navigation, focus states, "
                    "labels, ARIA, contrast, heading hierarchy, form and dialog a11y."
                    if attempt == 1
                    else "Call a11y_approved(summary) or a11y_rejected(issues, target_phase) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the ACCESSIBILITY phase of reconstruct_site. Check "
                    "semantic HTML, keyboard navigation, focus states, labels, ARIA where "
                    "necessary, contrast, heading hierarchy, button/link semantics, form "
                    "accessibility, and dialog accessibility. Do NOT sacrifice "
                    "accessibility merely to reproduce visual appearance. If all pass, "
                    "call a11y_approved(summary). If issues exist, call "
                    "a11y_rejected(issues, target_phase) — set target_phase to the "
                    "earliest phase to re-enter (e.g. 'global_shell', 'component_system', "
                    "'page', or '' to retry here). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_accessibility_tools(event, data, self._validate_reentry_target),
            )
            if event.is_set():
                action = str(data.get("action", "reject"))
                ctx.validation_status["accessibility"] = (
                    "approved" if action == "approve" else "rejected"
                )
                ctx.last_transition = "a11y_approved" if action == "approve" else "a11y_rejected"
                if action == "approve":
                    ctx.completed_phases.append("accessibility")
                    return ReconstructState.PERFORMANCE
                target = str(data.get("target_phase", "")).strip()
                if target:
                    ctx.known_issues.append(
                        {
                            "phase": "accessibility",
                            "issue": f"re-entering {target}",
                            "severity": "medium",
                        }
                    )
                    return self._reentry_state(target)
                continue

        ctx.fail_reason = "accessibility phase never reported a verdict"
        return ReconstructState.FAILED

    async def _performance(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until perf_approved/rejected fires; may re-enter earlier phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Inspect performance: unnecessary client components/JS, image "
                    "optimisation, caching, query behaviour, bundle size, layout shifts."
                    if attempt == 1
                    else "Call perf_approved(summary) or perf_rejected(issues, target_phase) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the PERFORMANCE phase of reconstruct_site. Inspect "
                    "unnecessary client components, unnecessary JavaScript, image "
                    "optimisation, caching, query behaviour, rendering performance, bundle "
                    "size, and layout shifts. Use Next.js features appropriately. If all "
                    "checks pass, call perf_approved(summary). If issues exist, call "
                    "perf_rejected(issues, target_phase) — set target_phase to the "
                    "earliest phase to re-enter (e.g. 'page', 'data_layer', "
                    "'component_system', or '' to retry here). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_performance_tools(event, data, self._validate_reentry_target),
            )
            if event.is_set():
                action = str(data.get("action", "reject"))
                ctx.validation_status["performance"] = (
                    "approved" if action == "approve" else "rejected"
                )
                ctx.last_transition = "perf_approved" if action == "approve" else "perf_rejected"
                if action == "approve":
                    ctx.completed_phases.append("performance")
                    return ReconstructState.FIDELITY_PASS
                target = str(data.get("target_phase", "")).strip()
                if target:
                    ctx.known_issues.append(
                        {
                            "phase": "performance",
                            "issue": f"re-entering {target}",
                            "severity": "medium",
                        }
                    )
                    return self._reentry_state(target)
                continue

        ctx.fail_reason = "performance phase never reported a verdict"
        return ReconstructState.FAILED

    async def _fidelity_pass(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until fidelity_approved/rejected fires; may re-enter earlier phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Perform a final side-by-side comparison focusing on small "
                    "discrepancies (2-4px spacing, font weights, line heights, radii, "
                    "icon sizes, container widths, breakpoints, hover/loading states, "
                    "variant consistency)."
                    if attempt == 1
                    else "Call fidelity_approved(summary) or fidelity_rejected(discrepancies, target_phase) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the FIDELITY_PASS phase of reconstruct_site. Perform a "
                    "final side-by-side comparison of the complete application. Look "
                    "specifically for small discrepancies: 2-4px spacing differences, "
                    "wrong font weights, incorrect line heights, incorrect border radii, "
                    "wrong icon sizes, wrong container widths, incorrect breakpoints, "
                    "missing hover states, missing loading states, inconsistent component "
                    "variants. This phase focuses on POLISH, not major architecture "
                    "changes. If no blocking discrepancies remain, call "
                    "fidelity_approved(summary). Otherwise call "
                    "fidelity_rejected(discrepancies, target_phase) — set target_phase to "
                    "the earliest phase to re-enter (e.g. 'design_system', "
                    "'component_system', 'page', or '' to retry here). Only a successful "
                    "transition-tool call changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_fidelity_pass_tools(event, data, self._validate_reentry_target),
            )
            if event.is_set():
                action = str(data.get("action", "reject"))
                ctx.validation_status["fidelity"] = (
                    "approved" if action == "approve" else "rejected"
                )
                ctx.last_transition = (
                    "fidelity_approved" if action == "approve" else "fidelity_rejected"
                )
                if action == "approve":
                    ctx.completed_phases.append("fidelity_pass")
                    return ReconstructState.SQLITE_DB
                raw = data.get("discrepancies", [])
                if isinstance(raw, list):
                    ctx.visual_discrepancies.extend(d for d in raw if isinstance(d, dict))
                target = str(data.get("target_phase", "")).strip()
                if target:
                    ctx.known_issues.append(
                        {
                            "phase": "fidelity_pass",
                            "issue": f"re-entering {target}",
                            "severity": "low",
                        }
                    )
                    return self._reentry_state(target)
                continue

        ctx.fail_reason = "fidelity_pass never reported a verdict"
        return ReconstructState.FAILED

    async def _final_validation(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until final_approved/final_blocked fires; return COMPLETE or BLOCKED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Run the full final validation suite before completing."
                    if attempt == 1
                    else "Call final_approved(summary) or final_blocked(issue) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the FINAL_VALIDATION phase of reconstruct_site. Run type "
                    "checking, linting, tests where available, a production build, route "
                    "validation, visual validation, and interaction validation. Confirm: "
                    "all required routes work; there are no console errors; there are no "
                    "broken images or missing assets; responsive layouts work; TanStack "
                    "Query is functioning correctly; shadcn/ui components are used "
                    "appropriately; Tailwind styling is consistent; Next.js conventions "
                    "are followed. If everything passes, call final_approved(summary) — the "
                    "workflow COMPLETES. If a material blocker prevents completion, call "
                    "final_blocked(issue) — the workflow ends BLOCKED with the issue "
                    "documented. NEVER report success if critical validation failed. Only "
                    "a successful transition-tool call changes phase; prose never "
                    "advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_final_validation_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "reject"))
                ctx.last_transition = "final_approved" if action == "approve" else "final_blocked"
                if action == "approve":
                    ctx.validation_status["final"] = "approved"
                    ctx.completed_phases.append("final_validation")
                    return ReconstructState.COMPLETE
                ctx.fail_reason = str(data.get("issue", "blocked by final validation"))
                ctx.blocked_phases.append("final_validation")
                ctx.validation_status["final"] = "blocked"
                return ReconstructState.BLOCKED

        ctx.fail_reason = "final_validation never reported a verdict"
        return ReconstructState.FAILED

    # ── infrastructure phase methods (sqlite → docs) ────────────────────

    async def _sqlite_db(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_sqlite fires; return VERIFY_SQLITE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Introduce a SQLite database with realistic mocked/seed data for local development."
                    if attempt == 1
                    else "Call submit_sqlite(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the SQLITE_DB phase of reconstruct_site. "
                    "Introduce a SQLite database with realistic mocked/seed data for local development. "
                    "Call submit_sqlite(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_sqlite_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["sqlite"] = "submitted"
                ctx.completed_phases.append("sqlite_db")
                ctx.last_transition = "submit_sqlite"
                return ReconstructState.VERIFY_SQLITE

        ctx.fail_reason = "sqlite_db phase never called submit_sqlite()"
        return ReconstructState.FAILED

    async def _verify_sqlite(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until sqlite_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify the SQLite database: schema init, deterministic seed, reset/reseed, application consumption, no hard-coded DB records in components, tests/typecheck/lint/build."
                    if attempt == 1
                    else "Call sqlite_verified(summary) or sqlite_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_SQLITE phase of reconstruct_site. "
                    "Verify the SQLite database: schema init, deterministic seed, reset/reseed, application consumption, no hard-coded DB records in components, tests/typecheck/lint/build. "
                    "Call sqlite_verified(summary) or sqlite_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_sqlite_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "sqlite_verified" if action == "verified" else "sqlite_rejected"
                )
                if action == "verified":
                    ctx.infra_status["sqlite"] = "verified"
                    ctx.completed_phases.append("verify_sqlite")
                    return ReconstructState.PRISMA
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_sqlite",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["sqlite"] = "rejected"
                continue

        ctx.fail_reason = "verify_sqlite never reported a verdict"
        return ReconstructState.FAILED

    async def _prisma(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_prisma fires; return VERIFY_PRISMA or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Introduce Prisma as the ORM/schema/migration layer over SQLite."
                    if attempt == 1
                    else "Call submit_prisma(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the PRISMA phase of reconstruct_site. "
                    "Introduce Prisma as the ORM/schema/migration layer over SQLite. "
                    "Call submit_prisma(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_prisma_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["prisma"] = "submitted"
                ctx.completed_phases.append("prisma")
                ctx.last_transition = "submit_prisma"
                return ReconstructState.VERIFY_PRISMA

        ctx.fail_reason = "prisma phase never called submit_prisma()"
        return ReconstructState.FAILED

    async def _verify_prisma(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until prisma_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify Prisma schema validation, client generation, migrations (fresh + existing), seed, reset, relations, types, integration, build, typecheck, tests."
                    if attempt == 1
                    else "Call prisma_verified(summary) or prisma_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_PRISMA phase of reconstruct_site. "
                    "Verify Prisma schema validation, client generation, migrations (fresh + existing), seed, reset, relations, types, integration, build, typecheck, tests. "
                    "Call prisma_verified(summary) or prisma_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_prisma_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "prisma_verified" if action == "verified" else "prisma_rejected"
                )
                if action == "verified":
                    ctx.infra_status["prisma"] = "verified"
                    ctx.completed_phases.append("verify_prisma")
                    return ReconstructState.TANSTACK_QUERY
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_prisma",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["prisma"] = "rejected"
                continue

        ctx.fail_reason = "verify_prisma never reported a verdict"
        return ReconstructState.FAILED

    async def _tanstack_query(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_tanstack fires; return VERIFY_TANSTACK or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Introduce TanStack Query as the client data layer: QueryClient, provider, query functions, mutations, query keys, cache invalidation, loading/error/empty/mutation states. UI must consume data through TanStack Query; Prisma must stay server-only."
                    if attempt == 1
                    else "Call submit_tanstack(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the TANSTACK_QUERY phase of reconstruct_site. "
                    "Introduce TanStack Query as the client data layer: QueryClient, provider, query functions, mutations, query keys, cache invalidation, loading/error/empty/mutation states. UI must consume data through TanStack Query; Prisma must stay server-only. "
                    "Call submit_tanstack(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_tanstack_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["tanstack"] = "submitted"
                ctx.completed_phases.append("tanstack_query")
                ctx.last_transition = "submit_tanstack"
                return ReconstructState.VERIFY_TANSTACK

        ctx.fail_reason = "tanstack_query phase never called submit_tanstack()"
        return ReconstructState.FAILED

    async def _verify_tanstack(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until tanstack_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify QueryClient/provider/query functions/mutations/keys/invalidation/loading/error/empty states, client/server boundaries, Prisma excluded from client bundles, build, typecheck, tests."
                    if attempt == 1
                    else "Call tanstack_verified(summary) or tanstack_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_TANSTACK phase of reconstruct_site. "
                    "Verify QueryClient/provider/query functions/mutations/keys/invalidation/loading/error/empty states, client/server boundaries, Prisma excluded from client bundles, build, typecheck, tests. "
                    "Call tanstack_verified(summary) or tanstack_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_tanstack_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "tanstack_verified" if action == "verified" else "tanstack_rejected"
                )
                if action == "verified":
                    ctx.infra_status["tanstack"] = "verified"
                    ctx.completed_phases.append("verify_tanstack")
                    return ReconstructState.ENV_CONFIG
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_tanstack",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["tanstack"] = "rejected"
                continue

        ctx.fail_reason = "verify_tanstack never reported a verdict"
        return ReconstructState.FAILED

    async def _env_config(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_env fires; return VERIFY_ENV or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Create and configure .env.local, .env.example, .env.prod, and .env.netlify and update .gitignore. Every variable must have detailed comments (purpose, scope, how to generate/obtain, format, local vs prod). No real secrets."
                    if attempt == 1
                    else "Call submit_env(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the ENV_CONFIG phase of reconstruct_site. "
                    "Create and configure .env.local, .env.example, .env.prod, and .env.netlify and update .gitignore. Every variable must have detailed comments (purpose, scope, how to generate/obtain, format, local vs prod). No real secrets. "
                    "Call submit_env(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_env_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["env"] = "submitted"
                ctx.completed_phases.append("env_config")
                ctx.last_transition = "submit_env"
                return ReconstructState.VERIFY_ENV

        ctx.fail_reason = "env_config phase never called submit_env()"
        return ReconstructState.FAILED

    async def _verify_env(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until env_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify all four .env.* files exist, every variable is documented, no secrets, .gitignore behavior, names match code references, build/startup works with the documented local config."
                    if attempt == 1
                    else "Call env_verified(summary) or env_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_ENV phase of reconstruct_site. "
                    "Verify all four .env.* files exist, every variable is documented, no secrets, .gitignore behavior, names match code references, build/startup works with the documented local config. "
                    "Call env_verified(summary) or env_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_env_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = "env_verified" if action == "verified" else "env_rejected"
                if action == "verified":
                    ctx.infra_status["env"] = "verified"
                    ctx.completed_phases.append("verify_env")
                    return ReconstructState.DOCKER
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_env",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["env"] = "rejected"
                continue

        ctx.fail_reason = "verify_env never reported a verdict"
        return ReconstructState.FAILED

    async def _docker(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_docker fires; return VERIFY_DOCKER or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Create a Dockerfile (multi-stage where appropriate), docker-compose.yaml, docker-compose-dev.yaml, .dockerignore, and Docker docs under docs/."
                    if attempt == 1
                    else "Call submit_docker(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the DOCKER phase of reconstruct_site. "
                    "Create a Dockerfile (multi-stage where appropriate), docker-compose.yaml, docker-compose-dev.yaml, .dockerignore, and Docker docs under docs/. "
                    "Call submit_docker(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_docker_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["docker"] = "submitted"
                ctx.completed_phases.append("docker")
                ctx.last_transition = "submit_docker"
                return ReconstructState.VERIFY_DOCKER

        ctx.fail_reason = "docker phase never called submit_docker()"
        return ReconstructState.FAILED

    async def _verify_docker(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until docker_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify Docker syntax, compose syntax, image build where available, startup, accessibility, logs, health checks, env, Prisma init, database/volume behavior, no secrets baked in, docs commands work."
                    if attempt == 1
                    else "Call docker_verified(summary) or docker_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_DOCKER phase of reconstruct_site. "
                    "Verify Docker syntax, compose syntax, image build where available, startup, accessibility, logs, health checks, env, Prisma init, database/volume behavior, no secrets baked in, docs commands work. "
                    "Call docker_verified(summary) or docker_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_docker_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "docker_verified" if action == "verified" else "docker_rejected"
                )
                if action == "verified":
                    ctx.infra_status["docker"] = "verified"
                    ctx.completed_phases.append("verify_docker")
                    return ReconstructState.NETLIFY
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_docker",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["docker"] = "rejected"
                continue

        ctx.fail_reason = "verify_docker never reported a verdict"
        return ReconstructState.FAILED

    async def _netlify(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_netlify fires; return VERIFY_NETLIFY or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Create/refine netlify.toml (build command including prisma generate, publish dir, redirects, headers, functions where needed). Document the SQLite production limitation."
                    if attempt == 1
                    else "Call submit_netlify(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the NETLIFY phase of reconstruct_site. "
                    "Create/refine netlify.toml (build command including prisma generate, publish dir, redirects, headers, functions where needed). Document the SQLite production limitation. "
                    "Call submit_netlify(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_netlify_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["netlify"] = "submitted"
                ctx.completed_phases.append("netlify")
                ctx.last_transition = "submit_netlify"
                return ReconstructState.VERIFY_NETLIFY

        ctx.fail_reason = "netlify phase never called submit_netlify()"
        return ReconstructState.FAILED

    async def _verify_netlify(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until netlify_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify netlify.toml, build command, publish dir, redirects, headers, functions, plugins, env vars, prisma generation, production build, deployment compatibility, SQLite production limitation."
                    if attempt == 1
                    else "Call netlify_verified(summary) or netlify_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_NETLIFY phase of reconstruct_site. "
                    "Verify netlify.toml, build command, publish dir, redirects, headers, functions, plugins, env vars, prisma generation, production build, deployment compatibility, SQLite production limitation. "
                    "Call netlify_verified(summary) or netlify_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_netlify_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "netlify_verified" if action == "verified" else "netlify_rejected"
                )
                if action == "verified":
                    ctx.infra_status["netlify"] = "verified"
                    ctx.completed_phases.append("verify_netlify")
                    return ReconstructState.CADDY
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_netlify",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["netlify"] = "rejected"
                continue

        ctx.fail_reason = "verify_netlify never reported a verdict"
        return ReconstructState.FAILED

    async def _caddy(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_caddy fires; return VERIFY_CADDY or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Create a Caddyfile reverse proxy (documented placeholder domain, HTTPS, headers, compression, WebSocket upgrade where needed) and Caddy docs under docs/."
                    if attempt == 1
                    else "Call submit_caddy(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the CADDY phase of reconstruct_site. "
                    "Create a Caddyfile reverse proxy (documented placeholder domain, HTTPS, headers, compression, WebSocket upgrade where needed) and Caddy docs under docs/. "
                    "Call submit_caddy(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_caddy_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["caddy"] = "submitted"
                ctx.completed_phases.append("caddy")
                ctx.last_transition = "submit_caddy"
                return ReconstructState.VERIFY_CADDY

        ctx.fail_reason = "caddy phase never called submit_caddy()"
        return ReconstructState.FAILED

    async def _verify_caddy(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until caddy_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify Caddy syntax, upstream address, ports, reverse proxy behavior, WebSocket behavior, Docker networking, HTTPS config, docs."
                    if attempt == 1
                    else "Call caddy_verified(summary) or caddy_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_CADDY phase of reconstruct_site. "
                    "Verify Caddy syntax, upstream address, ports, reverse proxy behavior, WebSocket behavior, Docker networking, HTTPS config, docs. "
                    "Call caddy_verified(summary) or caddy_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_caddy_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = "caddy_verified" if action == "verified" else "caddy_rejected"
                if action == "verified":
                    ctx.infra_status["caddy"] = "verified"
                    ctx.completed_phases.append("verify_caddy")
                    return ReconstructState.PACKAGE_COMMANDS
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_caddy",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["caddy"] = "rejected"
                continue

        ctx.fail_reason = "verify_caddy never reported a verdict"
        return ReconstructState.FAILED

    async def _package_commands(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_package fires; return VERIFY_PACKAGE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Enhance package.json with development, quality (lint/typecheck/test/check), database (db:generate/migrate/migrate:deploy/seed/reset/studio), Docker, and deployment commands."
                    if attempt == 1
                    else "Call submit_package(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the PACKAGE_COMMANDS phase of reconstruct_site. "
                    "Enhance package.json with development, quality (lint/typecheck/test/check), database (db:generate/migrate/migrate:deploy/seed/reset/studio), Docker, and deployment commands. "
                    "Call submit_package(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_package_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["package"] = "submitted"
                ctx.completed_phases.append("package_commands")
                ctx.last_transition = "submit_package"
                return ReconstructState.VERIFY_PACKAGE

        ctx.fail_reason = "package_commands phase never called submit_package()"
        return ReconstructState.FAILED

    async def _verify_package(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until package_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Verify every new command's executable exists, run non-destructive commands, lint, typecheck, tests, production build, Prisma commands, Docker commands, deployment prep, package manager usage."
                    if attempt == 1
                    else "Call package_verified(summary) or package_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_PACKAGE phase of reconstruct_site. "
                    "Verify every new command's executable exists, run non-destructive commands, lint, typecheck, tests, production build, Prisma commands, Docker commands, deployment prep, package manager usage. "
                    "Call package_verified(summary) or package_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_package_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "package_verified" if action == "verified" else "package_rejected"
                )
                if action == "verified":
                    ctx.infra_status["package"] = "verified"
                    ctx.completed_phases.append("verify_package")
                    return ReconstructState.SCRIPTS
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_package",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["package"] = "rejected"
                continue

        ctx.fail_reason = "verify_package never reported a verdict"
        return ReconstructState.FAILED

    async def _scripts(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_scripts fires; return VERIFY_SCRIPTS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Create purposeful Bash automation under scripts/ (env, database, development, quality, Docker, deployment, diagnostics) and docs/scripts.md. Every script must be robust and safe."
                    if attempt == 1
                    else "Call submit_scripts(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the SCRIPTS phase of reconstruct_site. "
                    "Create purposeful Bash automation under scripts/ (env, database, development, quality, Docker, deployment, diagnostics) and docs/scripts.md. Every script must be robust and safe. "
                    "Call submit_scripts(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_scripts_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["scripts"] = "submitted"
                ctx.completed_phases.append("scripts")
                ctx.last_transition = "submit_scripts"
                return ReconstructState.VERIFY_SCRIPTS

        ctx.fail_reason = "scripts phase never called submit_scripts()"
        return ReconstructState.FAILED

    async def _verify_scripts(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until scripts_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Run 'bash -n scripts/*.sh', verify executable permissions, run relevant scripts, test from different working dirs, test missing-prereq behavior, verify destructive safeguards, search for secrets, verify docs."
                    if attempt == 1
                    else "Call scripts_verified(summary) or scripts_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_SCRIPTS phase of reconstruct_site. "
                    "Run 'bash -n scripts/*.sh', verify executable permissions, run relevant scripts, test from different working dirs, test missing-prereq behavior, verify destructive safeguards, search for secrets, verify docs. "
                    "Call scripts_verified(summary) or scripts_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_scripts_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = (
                    "scripts_verified" if action == "verified" else "scripts_rejected"
                )
                if action == "verified":
                    ctx.infra_status["scripts"] = "verified"
                    ctx.completed_phases.append("verify_scripts")
                    return ReconstructState.DOCS
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_scripts",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["scripts"] = "rejected"
                continue

        ctx.fail_reason = "verify_scripts never reported a verdict"
        return ReconstructState.FAILED

    async def _docs(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until submit_docs fires; return VERIFY_DOCS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Write comprehensive docs/ covering overview, getting started, architecture, source code, components, data model, Prisma, TanStack Query, env variables, development workflow, testing, Docker, Caddy, Netlify, deployment, bash automation, package commands, troubleshooting, security, operations, reconstruction decisions. Use real commands and paths."
                    if attempt == 1
                    else "Call submit_docs(summary) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the DOCS phase of reconstruct_site. "
                    "Write comprehensive docs/ covering overview, getting started, architecture, source code, components, data model, Prisma, TanStack Query, env variables, development workflow, testing, Docker, Caddy, Netlify, deployment, bash automation, package commands, troubleshooting, security, operations, reconstruction decisions. Use real commands and paths. "
                    "Call submit_docs(summary). Only a successful transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                mode="Yolo",
                max_turns=40,
                shared_memory=memory,
                tools=_make_docs_tools(event, data),
            )
            if event.is_set():
                ctx.infra_status["docs"] = "submitted"
                ctx.completed_phases.append("docs")
                ctx.last_transition = "submit_docs"
                return ReconstructState.VERIFY_DOCS

        ctx.fail_reason = "docs phase never called submit_docs()"
        return ReconstructState.FAILED

    async def _verify_docs(self, ctx: ReconstructContext, memory: object) -> ReconstructState:
        """Loop until docs_verified/rejected fires; rejection retries this phase."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    "Audit the docs: enumerate source/config/infra files, compare against docs, find undocumented systems, outdated commands, incorrect paths, missing env vars, missing scripts, missing deployment config; verify commands and paths exist; check contradictions; re-read as a new developer."
                    if attempt == 1
                    else "Call docs_verified(summary) or docs_rejected(issues) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_DOCS phase of reconstruct_site. "
                    "Audit the docs: enumerate source/config/infra files, compare against docs, find undocumented systems, outdated commands, incorrect paths, missing env vars, missing scripts, missing deployment config; verify commands and paths exist; check contradictions; re-read as a new developer. "
                    "Call docs_verified(summary) or docs_rejected(issues). Only a "
                    "successful transition-tool call changes phase; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_verify_docs_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", "rejected"))
                ctx.last_transition = "docs_verified" if action == "verified" else "docs_rejected"
                if action == "verified":
                    ctx.infra_status["docs"] = "verified"
                    ctx.completed_phases.append("verify_docs")
                    return ReconstructState.FINAL_VALIDATION
                raw = data.get("issues", [])
                if isinstance(raw, list):
                    ctx.known_issues.extend(
                        {
                            "phase": "verify_docs",
                            "issue": str(issue),
                            "severity": "medium",
                        }
                        for issue in raw
                    )
                ctx.infra_status["docs"] = "rejected"
                continue

        ctx.fail_reason = "verify_docs never reported a verdict"
        return ReconstructState.FAILED

    @staticmethod
    def _reentry_state(target_phase: str) -> ReconstructState:
        """Map a phase name back to its state for controlled re-entry."""
        mapping = {
            "init": ReconstructState.INIT,
            "recon": ReconstructState.RECON,
            "visual_research": ReconstructState.VISUAL_RESEARCH,
            "interaction_analysis": ReconstructState.INTERACTION_ANALYSIS,
            "content_assets": ReconstructState.CONTENT_ASSETS,
            "architecture": ReconstructState.ARCHITECTURE,
            "design_system": ReconstructState.DESIGN_SYSTEM,
            "bootstrap": ReconstructState.BOOTSTRAP,
            "global_shell": ReconstructState.GLOBAL_SHELL,
            "component_system": ReconstructState.COMPONENT_SYSTEM,
            "page": ReconstructState.PAGE,
            "data_layer": ReconstructState.DATA_LAYER,
            "responsive_pass": ReconstructState.RESPONSIVE_PASS,
        }
        return mapping.get(target_phase, ReconstructState.VISUAL_VALIDATION)


@dataclasses.dataclass
class ReconstructSiteParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.reconstruct_site]."""

    init_model: str = ""
    recon_model: str = ""
    visual_model: str = ""
    interaction_model: str = ""
    assets_model: str = ""
    architecture_model: str = ""
    design_model: str = ""
    bootstrap_model: str = ""
    shell_model: str = ""
    components_model: str = ""
    page_model: str = ""
    data_model: str = ""
    responsive_model: str = ""
    visual_validation_model: str = ""
    interaction_validation_model: str = ""
    accessibility_model: str = ""
    performance_model: str = ""
    fidelity_model: str = ""
    final_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "init": self.init_model,
            "recon": self.recon_model,
            "visual_research": self.visual_model,
            "interaction_analysis": self.interaction_model,
            "content_assets": self.assets_model,
            "architecture": self.architecture_model,
            "design_system": self.design_model,
            "bootstrap": self.bootstrap_model,
            "global_shell": self.shell_model,
            "component_system": self.components_model,
            "page": self.page_model,
            "data_layer": self.data_model,
            "responsive_pass": self.responsive_model,
            "visual_validation": self.visual_validation_model,
            "interaction_validation": self.interaction_validation_model,
            "accessibility": self.accessibility_model,
            "performance": self.performance_model,
            "fidelity_pass": self.fidelity_model,
            "final_validation": self.final_model,
        }


class ReconstructSiteWorkflow(WorkflowPlugin):
    """Reconstruct a reference website as a modern Next.js + Tailwind + shadcn/ui + TanStack Query app."""

    name = "reconstruct_site"
    description = (
        "Reconstruct a reference website as a modern Next.js + Tailwind + shadcn/ui + "
        "TanStack Query app through explicit agent-controlled phases."
    )
    mode_bindings = []  # manual only — invoke with /workflow reconstruct_site
    # Declarative metadata for the registry and the TUI phase counter; the runner
    # above is what actually executes, and it follows exactly this graph. The
    # dynamic PAGE phase re-enters itself once per discovered route.
    phases = [
        PhaseSpec(
            name="init",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=10,
            next="recon",
            on_reject="init",
            system_prompt_override=(
                "You are in the INIT phase of reconstruct_site. Call submit_initial_state("
                "reference_url, target_directory, constraints, desired_routes, "
                "auth_required, reference_is_static, reproduce_api_or_mock). Only a "
                "successful transition-tool call changes phase; prose never advances the "
                "workflow."
            ),
        ),
        PhaseSpec(
            name="recon",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="visual_research",
            on_reject="recon",
            system_prompt_override=(
                "You are in the RECON phase of reconstruct_site. Inventory the reference "
                "website and call submit_route_inventory(routes, summary). Only a "
                "successful transition-tool call changes phase; prose never advances the "
                "workflow. Do NOT start coding."
            ),
        ),
        PhaseSpec(
            name="visual_research",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="interaction_analysis",
            on_reject="visual_research",
            system_prompt_override=(
                "You are in the VISUAL_RESEARCH phase of reconstruct_site. Extract "
                "concrete measured visual tokens and call submit_visual_spec(design_tokens, "
                "summary). Only a successful transition-tool call changes phase; prose "
                "never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="interaction_analysis",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="content_assets",
            on_reject="interaction_analysis",
            system_prompt_override=(
                "You are in the INTERACTION_ANALYSIS phase of reconstruct_site. Catalogue "
                "how the site behaves and call submit_interaction_inventory(interactions, "
                "summary). Only a successful transition-tool call changes phase; prose "
                "never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="content_assets",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="architecture",
            on_reject="content_assets",
            system_prompt_override=(
                "You are in the CONTENT_ASSETS phase of reconstruct_site. Inventory "
                "content/assets with real dimensions and call submit_asset_inventory("
                "assets, summary). Only a successful transition-tool call changes phase; "
                "prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="architecture",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="design_system",
            on_reject="architecture",
            system_prompt_override=(
                "You are in the ARCHITECTURE phase of reconstruct_site. Write the target "
                "Next.js architecture and call submit_architecture(architecture). Only a "
                "successful transition-tool call changes phase; prose never advances the "
                "workflow."
            ),
        ),
        PhaseSpec(
            name="design_system",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="bootstrap",
            on_reject="design_system",
            system_prompt_override=(
                "You are in the DESIGN_SYSTEM phase of reconstruct_site. Extract design "
                "tokens + shadcn/ui component map and call submit_design_system("
                "design_tokens, component_map, summary). Only a successful transition-tool "
                "call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="bootstrap",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="global_shell",
            on_reject="bootstrap",
            system_prompt_override=(
                "You are in the BOOTSTRAP phase of reconstruct_site. Scaffold a healthy "
                "Next.js project and call confirm_bootstrap_healthy(summary). Only a "
                "successful transition-tool call changes phase; prose never advances the "
                "workflow."
            ),
        ),
        PhaseSpec(
            name="global_shell",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="component_system",
            on_reject="global_shell",
            system_prompt_override=(
                "You are in the GLOBAL_SHELL phase of reconstruct_site. Implement the "
                "global shell and call confirm_global_shell(summary). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="component_system",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="page",
            on_reject="component_system",
            system_prompt_override=(
                "You are in the COMPONENT_SYSTEM phase of reconstruct_site. Build the "
                "shared component library over shadcn/ui and call confirm_component_system("
                "summary). Only a successful transition-tool call changes phase; prose "
                "never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="page",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=40,
            next="data_layer",
            on_reject="page",
            system_prompt_override=(
                "You are in the PAGE phase of reconstruct_site. Implement the current "
                "route and call complete_page(page_route, summary). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="data_layer",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="responsive_pass",
            on_reject="data_layer",
            system_prompt_override=(
                "You are in the DATA_LAYER phase of reconstruct_site. Implement the "
                "TanStack Query data layer and call confirm_data_layer(summary). Only a "
                "successful transition-tool call changes phase; prose never advances the "
                "workflow."
            ),
        ),
        PhaseSpec(
            name="responsive_pass",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="visual_validation",
            on_reject="responsive_pass",
            system_prompt_override=(
                "You are in the RESPONSIVE_PASS phase of reconstruct_site. Verify "
                "responsive behaviour at real breakpoints and call confirm_responsive("
                "summary). Only a successful transition-tool call changes phase; prose "
                "never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="visual_validation",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="interaction_validation",
            on_reject="visual_validation",
            system_prompt_override=(
                "You are in the VISUAL_VALIDATION phase of reconstruct_site. Compare "
                "screenshots; call visual_approved(summary) or visual_rejected("
                "discrepancies, target_phase). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="interaction_validation",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="accessibility",
            on_reject="interaction_validation",
            system_prompt_override=(
                "You are in the INTERACTION_VALIDATION phase of reconstruct_site. Test "
                "major user flows; call interaction_approved(summary) or "
                "interaction_rejected(discrepancies, target_phase). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="accessibility",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="performance",
            on_reject="accessibility",
            system_prompt_override=(
                "You are in the ACCESSIBILITY phase of reconstruct_site. Check "
                "accessibility; call a11y_approved(summary) or a11y_rejected(issues, "
                "target_phase). Only a successful transition-tool call changes phase; "
                "prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="performance",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="fidelity_pass",
            on_reject="performance",
            system_prompt_override=(
                "You are in the PERFORMANCE phase of reconstruct_site. Inspect "
                "performance; call perf_approved(summary) or perf_rejected(issues, "
                "target_phase). Only a successful transition-tool call changes phase; "
                "prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="fidelity_pass",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="sqlite_db",
            on_reject="fidelity_pass",
            system_prompt_override=(
                "You are in the FIDELITY_PASS phase of reconstruct_site. Polish small "
                "discrepancies; call fidelity_approved(summary) or fidelity_rejected("
                "discrepancies, target_phase). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="sqlite_db",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="verify_sqlite",
            on_reject="sqlite_db",
            system_prompt_override=(
                "You are in the SQLITE_DB phase of reconstruct_site. Introduce a "
                "SQLite database with realistic mocked/seed data for local "
                "development. Call submit_sqlite(summary). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_sqlite",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="prisma",
            on_reject="verify_sqlite",
            system_prompt_override=(
                "You are in the VERIFY_SQLITE phase of reconstruct_site. Verify the "
                "SQLite database: schema init, deterministic seed, reset/reseed, "
                "application consumption, no hard-coded DB records in components, "
                "tests/typecheck/lint/build. Call sqlite_verified(summary) or "
                "sqlite_rejected(issues). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="prisma",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="verify_prisma",
            on_reject="prisma",
            system_prompt_override=(
                "You are in the PRISMA phase of reconstruct_site. Introduce Prisma as "
                "the ORM/schema/migration layer over SQLite. Call submit_prisma(summary). "
                "Only a successful transition-tool call changes phase; prose never "
                "advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_prisma",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="tanstack_query",
            on_reject="verify_prisma",
            system_prompt_override=(
                "You are in the VERIFY_PRISMA phase of reconstruct_site. Verify Prisma "
                "schema validation, client generation, migrations (fresh + existing), "
                "seed, reset, relations, types, integration, build, typecheck, tests. "
                "Call prisma_verified(summary) or prisma_rejected(issues). Only a "
                "successful transition-tool call changes phase; prose never advances "
                "the workflow."
            ),
        ),
        PhaseSpec(
            name="tanstack_query",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="verify_tanstack",
            on_reject="tanstack_query",
            system_prompt_override=(
                "You are in the TANSTACK_QUERY phase of reconstruct_site. Introduce "
                "TanStack Query as the client data layer (QueryClient, provider, query "
                "functions, mutation functions, query keys, cache invalidation, "
                "loading/error/empty/mutation states). UI must consume data through "
                "TanStack Query; Prisma must stay server-only. Call "
                "submit_tanstack(summary). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_tanstack",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="env_config",
            on_reject="verify_tanstack",
            system_prompt_override=(
                "You are in the VERIFY_TANSTACK phase of reconstruct_site. Verify "
                "QueryClient/provider/query functions/mutations/keys/invalidation/"
                "loading/error/empty states, client/server boundaries, Prisma excluded "
                "from client bundles, build, typecheck, tests. Call "
                "tanstack_verified(summary) or tanstack_rejected(issues). Only a "
                "successful transition-tool call changes phase; prose never advances "
                "the workflow."
            ),
        ),
        PhaseSpec(
            name="env_config",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="verify_env",
            on_reject="env_config",
            system_prompt_override=(
                "You are in the ENV_CONFIG phase of reconstruct_site. Create and "
                "configure .env.local, .env.example, .env.prod, .env.netlify, and "
                "update .gitignore. Every variable must have detailed comments "
                "(purpose, scope, how to generate/obtain, format, local vs prod). No "
                "real secrets. Call submit_env(summary). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_env",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="docker",
            on_reject="verify_env",
            system_prompt_override=(
                "You are in the VERIFY_ENV phase of reconstruct_site. Verify all four "
                ".env.* files exist, every variable is documented, no secrets, "
                ".gitignore behavior, names match code references, build/startup works "
                "with documented local config. Call env_verified(summary) or "
                "env_rejected(issues). Only a successful transition-tool call changes "
                "phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="docker",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="verify_docker",
            on_reject="docker",
            system_prompt_override=(
                "You are in the DOCKER phase of reconstruct_site. Create Dockerfile "
                "(multi-stage where appropriate), docker-compose.yaml, "
                "docker-compose-dev.yaml, .dockerignore, and Docker docs under docs/. "
                "Call submit_docker(summary). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_docker",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="netlify",
            on_reject="verify_docker",
            system_prompt_override=(
                "You are in the VERIFY_DOCKER phase of reconstruct_site. Verify Docker "
                "syntax, compose syntax, image build where available, startup, "
                "accessibility, logs, health checks, env, Prisma init, database/volume "
                "behavior, no secrets baked in, docs commands work. Call "
                "docker_verified(summary) or docker_rejected(issues). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="netlify",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="verify_netlify",
            on_reject="netlify",
            system_prompt_override=(
                "You are in the NETLIFY phase of reconstruct_site. Create/refine "
                "netlify.toml (build command incl. prisma generate, publish dir, "
                "redirects, headers, functions where needed). Document the SQLite "
                "production limitation. Call submit_netlify(summary). Only a "
                "successful transition-tool call changes phase; prose never advances "
                "the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_netlify",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="caddy",
            on_reject="verify_netlify",
            system_prompt_override=(
                "You are in the VERIFY_NETLIFY phase of reconstruct_site. Verify "
                "netlify.toml, build command, publish dir, redirects, headers, "
                "functions, plugins, env vars, prisma generation, production build, "
                "deployment compatibility, SQLite limitation. Call "
                "netlify_verified(summary) or netlify_rejected(issues). Only a "
                "successful transition-tool call changes phase; prose never advances "
                "the workflow."
            ),
        ),
        PhaseSpec(
            name="caddy",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="verify_caddy",
            on_reject="caddy",
            system_prompt_override=(
                "You are in the CADDY phase of reconstruct_site. Create a Caddyfile "
                "reverse proxy (documented placeholder domain, HTTPS, headers, "
                "compression, WebSocket upgrade where needed) and Caddy docs under "
                "docs/. Call submit_caddy(summary). Only a successful transition-tool "
                "call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_caddy",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="package_commands",
            on_reject="verify_caddy",
            system_prompt_override=(
                "You are in the VERIFY_CADDY phase of reconstruct_site. Verify Caddy "
                "syntax, upstream address, ports, reverse proxy behavior, WebSocket "
                "behavior, Docker networking, HTTPS config, docs. Call "
                "caddy_verified(summary) or caddy_rejected(issues). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="package_commands",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="verify_package",
            on_reject="package_commands",
            system_prompt_override=(
                "You are in the PACKAGE_COMMANDS phase of reconstruct_site. Enhance "
                "package.json with development, quality (lint/typecheck/test/check), "
                "database (db:generate/migrate/migrate:deploy/seed/reset/studio), "
                "Docker, and deployment commands. Call submit_package(summary). Only "
                "a successful transition-tool call changes phase; prose never advances "
                "the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_package",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="scripts",
            on_reject="verify_package",
            system_prompt_override=(
                "You are in the VERIFY_PACKAGE phase of reconstruct_site. Verify every "
                "new command's executable exists, run non-destructive commands, lint, "
                "typecheck, tests, production build, Prisma commands, Docker commands, "
                "deployment prep, package manager usage. Call package_verified("
                "summary) or package_rejected(issues). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="scripts",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next="verify_scripts",
            on_reject="scripts",
            system_prompt_override=(
                "You are in the SCRIPTS phase of reconstruct_site. Create purposeful "
                "Bash automation under scripts/ (env, database, development, quality, "
                "Docker, deployment, diagnostics) and docs/scripts.md. Every script "
                "must be robust and safe. Call submit_scripts(summary). Only a "
                "successful transition-tool call changes phase; prose never advances "
                "the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_scripts",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="docs",
            on_reject="verify_scripts",
            system_prompt_override=(
                "You are in the VERIFY_SCRIPTS phase of reconstruct_site. Run "
                "'bash -n scripts/*.sh', verify executable permissions, run relevant "
                "scripts, test from different working dirs, test missing-prereq "
                "behavior, verify destructive safeguards, search for secrets, verify "
                "docs. Call scripts_verified(summary) or scripts_rejected(issues). "
                "Only a successful transition-tool call changes phase; prose never "
                "advances the workflow."
            ),
        ),
        PhaseSpec(
            name="docs",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=40,
            next="verify_docs",
            on_reject="docs",
            system_prompt_override=(
                "You are in the DOCS phase of reconstruct_site. Write comprehensive "
                "docs/ covering overview, getting started, architecture, source code, "
                "components, data model, Prisma, TanStack Query, env variables, "
                "development workflow, testing, Docker, Caddy, Netlify, deployment, "
                "bash automation, package commands, troubleshooting, security, "
                "operations, reconstruction decisions. Use real commands and paths. "
                "Call submit_docs(summary). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="verify_docs",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=30,
            next="final_validation",
            on_reject="verify_docs",
            system_prompt_override=(
                "You are in the VERIFY_DOCS phase of reconstruct_site. Audit the docs: "
                "enumerate source/config/infra files, compare against docs, find "
                "undocumented systems/outdated commands/incorrect paths/missing env "
                "vars/missing scripts/missing deployment config, verify commands and "
                "paths exist, check contradictions, re-read as a new developer. Call "
                "docs_verified(summary) or docs_rejected(issues). Only a successful "
                "transition-tool call changes phase; prose never advances the workflow."
            ),
        ),
        PhaseSpec(
            name="final_validation",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=35,
            next=None,  # terminal on success (COMPLETE) / BLOCKED
            on_reject="final_validation",
            system_prompt_override=(
                "You are in the FINAL_VALIDATION phase of reconstruct_site. Run the full "
                "validation suite; call final_approved(summary) (COMPLETES) or "
                "final_blocked(issue) (BLOCKED). Only a successful transition-tool call "
                "changes phase; prose never advances the workflow. Never report success "
                "if critical validation failed."
            ),
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, ReconstructContext):
            raise TypeError("reconstruct_site checkpoint requires ReconstructContext")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "fail_reason": context.fail_reason,
            "target_url": context.target_url,
            "target_directory": context.target_directory,
            "completed_phases": list(context.completed_phases),
            "failed_phases": list(context.failed_phases),
            "blocked_phases": list(context.blocked_phases),
            "skipped_phases": list(context.skipped_phases),
            "route_inventory": list(context.route_inventory),
            "pages_to_implement": list(context.pages_to_implement),
            "page_index": int(context.page_index),
            "asset_inventory": list(context.asset_inventory),
            "component_inventory": list(context.component_inventory),
            "design_tokens": dict(context.design_tokens),
            "architecture": context.architecture,
            "interaction_inventory": list(context.interaction_inventory),
            "implementation_status": dict(context.implementation_status),
            "validation_status": dict(context.validation_status),
            "known_issues": list(context.known_issues),
            "visual_discrepancies": list(context.visual_discrepancies),
            "interaction_discrepancies": list(context.interaction_discrepancies),
            "last_transition": context.last_transition,
            "artifacts": dict(context.artifacts),
            "infra_status": dict(context.infra_status),
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> ReconstructContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", ReconstructState.INIT.name))
        try:
            state = ReconstructState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown reconstruct_site state: {raw_state}") from exc
        return ReconstructContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=int(payload.get("phase_iteration", 0)),
            fail_reason=str(payload.get("fail_reason", "")),
            target_url=str(payload.get("target_url", "")),
            target_directory=str(payload.get("target_directory", "")),
            completed_phases=_str_list(payload.get("completed_phases")),
            failed_phases=_str_list(payload.get("failed_phases")),
            blocked_phases=_str_list(payload.get("blocked_phases")),
            skipped_phases=_str_list(payload.get("skipped_phases")),
            route_inventory=_dict_list(payload.get("route_inventory")),
            pages_to_implement=_str_list(payload.get("pages_to_implement")),
            page_index=int(payload.get("page_index", 0)),
            asset_inventory=_dict_list(payload.get("asset_inventory")),
            component_inventory=_dict_list(payload.get("component_inventory")),
            design_tokens=_str_dict(payload.get("design_tokens")),
            architecture=str(payload.get("architecture", "")),
            interaction_inventory=_dict_list(payload.get("interaction_inventory")),
            implementation_status=_str_dict(payload.get("implementation_status")),
            validation_status=_str_dict(payload.get("validation_status")),
            known_issues=_dict_list(payload.get("known_issues")),
            visual_discrepancies=_dict_list(payload.get("visual_discrepancies")),
            interaction_discrepancies=_dict_list(payload.get("interaction_discrepancies")),
            last_transition=str(payload.get("last_transition", "")),
            artifacts=_str_dict(payload.get("artifacts")),
            infra_status=_str_dict(payload.get("infra_status")),
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> ReconstructSiteRunner:
        """Return this workflow's own state-machine runner."""
        return ReconstructSiteRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.reconstruct_site]."""
        return ReconstructSiteParams(
            init_model=str(source.get("init_model", "") or ""),
            recon_model=str(source.get("recon_model", "") or ""),
            visual_model=str(source.get("visual_model", "") or ""),
            interaction_model=str(source.get("interaction_model", "") or ""),
            assets_model=str(source.get("assets_model", "") or ""),
            architecture_model=str(source.get("architecture_model", "") or ""),
            design_model=str(source.get("design_model", "") or ""),
            bootstrap_model=str(source.get("bootstrap_model", "") or ""),
            shell_model=str(source.get("shell_model", "") or ""),
            components_model=str(source.get("components_model", "") or ""),
            page_model=str(source.get("page_model", "") or ""),
            data_model=str(source.get("data_model", "") or ""),
            responsive_model=str(source.get("responsive_model", "") or ""),
            visual_validation_model=str(source.get("visual_validation_model", "") or ""),
            interaction_validation_model=str(source.get("interaction_validation_model", "") or ""),
            accessibility_model=str(source.get("accessibility_model", "") or ""),
            performance_model=str(source.get("performance_model", "") or ""),
            fidelity_model=str(source.get("fidelity_model", "") or ""),
            final_model=str(source.get("final_model", "") or ""),
        )


def _str_list(value: object) -> list[str]:
    """Coerce a payload value into a list of strings (bounded, JSON-safe)."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _dict_list(value: object) -> list[dict[str, object]]:
    """Coerce a payload value into a list of dicts (bounded, JSON-safe)."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _str_dict(value: object) -> dict[str, str]:
    """Coerce a payload value into a dict of str -> str (bounded, JSON-safe)."""
    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in value.items() if val is not None}
