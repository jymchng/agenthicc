"""site_imitate — Visit a reference website with Playwright, analyze its
structure, then build a new website for a different use case that mimics
the same design using NextJS, Tailwind CSS, shadCN UI, and TanStack Query.

The runner below is the shape to copy: a typed state enum, a typed context, one
bounded async method per non-terminal state, an explicit
``while not state.is_terminal`` / ``match`` driver, and transitions that happen
only because a phase tool was called.
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


# ---------------------------------------------------------------------------
# NameThatUI integration — a canonical component dictionary for the
# ANALYZE→PLAN→BUILD translation step.  The sibling module name_that_ui.py
# fetches https://namethatui.com/ once, extracts the embedded element catalog,
# and fuzzy-matches sloppy descriptions to canonical names + per-framework API
# symbols (shadcn/ui first) + paste-ready build prompts.  This is strictly an
# enhancement: if the catalog cannot be loaded the workflow proceeds exactly
# as before (the agent describes and builds from scratch).
# ---------------------------------------------------------------------------


def _import_ntui():
    """Import the NameThatUI helper (package copy first, then a sibling)."""
    try:
        from agenthicc.workflows import name_that_ui as _ntui

        return _ntui
    except Exception:  # noqa: BLE001 - helper is optional
        pass
    try:
        import name_that_ui as _ntui  # type: ignore[no-redef]

        return _ntui
    except Exception:  # noqa: BLE001 - helper is optional
        return None


def _load_ntui_catalog() -> list[dict]:
    """Load the cached/fetched NameThatUI catalog; [] when unavailable."""
    ntui = _import_ntui()
    if ntui is None:
        log.info("name_that_ui helper unavailable - proceeding without the dictionary")
        return []
    try:
        catalog = ntui.load_catalog()
        if catalog:
            log.info("name_that_ui catalog loaded: %d elements", len(catalog))
        return catalog or []
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning("name_that_ui catalog load failed: %s", exc)
        return []


def _parse_component_plan(components: object) -> list[dict]:
    """Turn the PLAN phase's component list into structured component specs.

    Each entry is one line:  "NAME | BUILD SPEC | VERIFY CHECK".
    Missing sections fall back to the name alone.
    """
    plan: list[dict] = []
    if not isinstance(components, (list, tuple)):
        return plan
    for item in components:
        line = str(item or "").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        plan.append(
            {
                "name": parts[0] or f"component-{len(plan) + 1}",
                "build": parts[1] if len(parts) > 1 else "",
                "verify": parts[2] if len(parts) > 2 else "",
            }
        )
    return plan


def _component_plan_text(ctx: SiteImitateContext) -> str:
    """Human-readable listing of the component plan for prompts."""
    if not ctx.component_plan:
        return "(no components planned)"
    lines = []
    for i, comp in enumerate(ctx.component_plan):
        lines.append(
            f"{i + 1}. {comp.get('name') or f'component-{i + 1}'}"
            f"\n   build : {comp.get('build') or '(from plan)'}"
            f"\n   verify: {comp.get('verify') or '(compiles and matches the reference)'}"
        )
    return "\n".join(lines)


class SiteImitateState(Enum):
    """Every state this workflow can be in."""

    ANALYZE = auto()
    PLAN = auto()
    SCAFFOLD = auto()
    BUILD = auto()
    VERIFY = auto()
    FINAL_VERIFY = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()  # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (SiteImitateState.COMPLETE, SiteImitateState.FAILED)


@dataclasses.dataclass
class SiteImitateContext:
    """Data carried across every phase of one run."""

    intent: str
    target_url: str = ""
    new_purpose: str = ""
    run_id: str = ""
    state: SiteImitateState = SiteImitateState.ANALYZE
    phase_iteration: int = 0
    fail_reason: str = ""
    analysis: str = ""
    plan: str = ""
    component_inventory: str = ""
    component_plan: list[dict] = dataclasses.field(default_factory=list)
    component_index: int = 0
    catalog: list[dict] = dataclasses.field(default_factory=list, repr=False)
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)


def _make_analyze_tools(
    event: asyncio.Event,
    data: dict[str, str],
    catalog: list[dict],
) -> list[Callable[..., object]]:
    """Return the tools that can end or enrich the analyze phase."""
    from lauren_ai._tools import tool

    @tool()
    async def lookup_component(description: str) -> dict[str, object]:
        """Name a UI element from a sloppy description using the NameThatUI dictionary.

        Args:
            description: How the element looks or where it sits, in plain words
                (e.g. "the three lines that open a side panel", "the gray text
                inside the box that disappears when you type").
        """
        ntui = _import_ntui()
        if ntui is None or not catalog:
            return {
                "ok": False,
                "error": "The NameThatUI dictionary is unavailable.",
                "fix": "Describe this element from scratch in the analysis.",
            }
        matches = ntui.lookup(description, catalog, top=5)
        if not matches:
            return {
                "ok": True,
                "matches": [],
                "message": "Nothing matched that description - describe this element from scratch.",
            }
        return {
            "ok": True,
            "matches": [ntui.match_brief(m) for m in matches],
            "message": ntui.format_matches(matches),
        }

    @tool()
    async def list_components(platform: str = "", query: str = "") -> dict[str, object]:
        """List the component names available in the NameThatUI dictionary.

        Use this to see what canonical components exist before describing an
        element, so the analysis uses the dictionary's own vocabulary.

        Args:
            platform: Optional filter - "web" or "macos". Empty = all.
            query: Optional keyword to narrow the list (matched against the
                name and its aliases), e.g. "menu", "field", "scroll".
        """
        ntui = _import_ntui()
        if ntui is None or not catalog:
            return {
                "ok": False,
                "error": "The NameThatUI dictionary is unavailable.",
                "fix": "Describe observed elements from scratch in the analysis.",
            }
        names = ntui.list_names(catalog, platform=platform, query=query)
        return {
            "ok": True,
            "total": len(names),
            "components": names,
            "message": ntui.format_name_list(names),
        }

    @tool()
    async def submit_analysis(analysis: str, components: str = "") -> dict[str, object]:
        """Record the structured analysis of the reference website and advance.

        Args:
            analysis: Complete description of the reference site: structure,
                pages, layouts, component patterns, navigation, user flows,
                color scheme, typography, and any interactive features.
            components: Optional compact Named Component Inventory - one line
                per element, e.g. "three-line button -> Hamburger Menu (Nav
                Drawer) (Sheet side=\"left\")". Recorded and reused by the
                plan, build, and verify phases.
        """
        if not analysis.strip():
            return {
                "ok": False,
                "error": "The analysis was rejected: it must not be empty.",
                "fix": "Call submit_analysis(analysis) with the site analysis.",
            }
        data["analysis"] = analysis.strip()
        data["components"] = components.strip()
        event.set()
        return {"ok": True, "message": "Analysis recorded. The plan phase starts next."}

    return [list_components, lookup_component, submit_analysis]


def _make_plan_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return tools that can end or redirect the plan phase."""
    from lauren_ai._tools import tool

    @tool()
    async def submit_plan(plan: str, components: list[str] = []) -> dict[str, object]:
        """Record the architecture and build plan, then advance to scaffold.

        Args:
            plan: Complete build plan covering pages, tech stack, data sources,
                directory structure, and ordered implementation steps.
            components: The ordered list of website parts to build, one string
                per part in the format "NAME | BUILD SPEC | VERIFY CHECK".
                Examples:
                  "Nav Drawer | Sheet side=\"left\" with the three-line button (aria-expanded + aria-controls) | button toggles the panel; Sheet primitive used"
                  "Header | sticky top bar: logo, nav links, mobile hamburger | header is sticky; logo and links render"
                The workflow builds and verifies each part in its own phase,
                in the order given. Include the site-wide parts (header,
                sidebar, footer, layout) and every page section.
        """
        if not plan.strip():
            return {
                "ok": False,
                "error": "The plan was rejected: it must not be empty.",
                "fix": "Call submit_plan(plan, components=[...]) with the build plan.",
            }
        if not components:
            return {
                "ok": False,
                "error": "The plan was rejected: it must include at least one component.",
                "fix": "Call submit_plan(plan, components=[...]) with the ordered component list.",
            }
        data["plan"] = plan.strip()
        data["components"] = [str(c) for c in components]
        event.set()
        return {
            "ok": True,
            "message": f"Plan recorded with {len(components)} components. The scaffold phase starts next.",
        }

    @tool()
    async def request_reanalysis(question: str) -> dict[str, object]:
        """Signal that more analysis is needed before a plan can be written.

        Args:
            question: What additional information about the reference site is needed.
        """
        data["action"] = "reanalyze"
        data["question"] = question.strip()
        event.set()
        return {"ok": True, "message": "Re-analysis requested. Returning to analyze phase."}

    return [submit_plan, request_reanalysis]


def _make_scaffold_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the tool that ends the scaffold phase."""
    from lauren_ai._tools import tool

    @tool()
    async def scaffold_complete(path: str) -> dict[str, object]:
        """Signal that the NextJS project has been scaffolded with all deps.

        Args:
            path: Absolute or relative path to the created project directory.
        """
        if not path.strip():
            return {
                "ok": False,
                "error": "The project path must not be empty.",
                "fix": "Call scaffold_complete(path) with the project directory path.",
            }
        data["path"] = path.strip()
        event.set()
        return {"ok": True, "message": "Scaffold complete. The build phase starts next."}

    return [scaffold_complete]


def _make_build_tools(
    event: asyncio.Event,
    data: dict[str, str],
    catalog: list[dict],
) -> list[Callable[..., object]]:
    """Return the tools that can end or enrich the build phase."""
    from lauren_ai._tools import tool

    @tool()
    async def lookup_component(description: str) -> dict[str, object]:
        """Name a UI element from a sloppy description using the NameThatUI dictionary.

        Args:
            description: How the element looks or where it sits, in plain words.
        """
        ntui = _import_ntui()
        if ntui is None or not catalog:
            return {
                "ok": False,
                "error": "The NameThatUI dictionary is unavailable.",
                "fix": "Build this component from the plan and your best judgment.",
            }
        matches = ntui.lookup(description, catalog, top=5)
        if not matches:
            return {
                "ok": True,
                "matches": [],
                "message": "Nothing matched that description - build this component from scratch.",
            }
        return {
            "ok": True,
            "matches": [ntui.match_brief(m) for m in matches],
            "message": ntui.format_matches(matches),
        }

    @tool()
    async def component_built() -> dict[str, object]:
        """Signal that this phase's component has been implemented.

        Call this only after the current component's files have been written.
        """
        data["built"] = "1"
        event.set()
        return {"ok": True, "message": "Component built. The verify phase for this component starts next."}

    return [lookup_component, component_built]


def _make_verify_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the pass/fail decision tools for one component's verify phase."""
    from lauren_ai._tools import tool

    @tool()
    async def component_verified(summary: str) -> dict[str, object]:
        """Signal that the current component passed verification.

        Args:
            summary: What was verified (compile check, canonical anatomy,
                matches the reference) and the result.
        """
        data["action"] = "pass"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Component verified. Advancing to the next component."}

    @tool()
    async def component_verification_failed(errors: str) -> dict[str, object]:
        """Signal that the current component failed verification.

        Args:
            errors: What failed - compile errors, missing canonical anatomy,
                or mismatches against the reference. Must be fixed.
        """
        data["action"] = "fail"
        data["errors"] = errors.strip()
        event.set()
        return {"ok": True, "message": "Errors recorded. Re-entering this component's build phase."}

    return [component_verified, component_verification_failed]


def _make_final_verify_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[Callable[..., object]]:
    """Return the pass/fail decision tools for the final verify phase."""
    from lauren_ai._tools import tool

    @tool()
    async def final_verify_passed(summary: str) -> dict[str, object]:
        """Signal that the whole site builds and all routes compile.

        Args:
            summary: Build output summary showing success.
        """
        data["action"] = "pass"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Final verify passed. The site is complete."}

    @tool()
    async def final_verify_failed(errors: str) -> dict[str, object]:
        """Signal that the whole-site build failed with errors to fix.

        Args:
            errors: The build errors that need to be resolved.
        """
        data["action"] = "fail"
        data["errors"] = errors.strip()
        event.set()
        return {"ok": True, "message": "Errors recorded. Re-entering the last component's build phase."}

    return [final_verify_passed, final_verify_failed]


class SiteImitateRunner(CodePlanRunner):
    """State-machine runner for site_imitate.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none of
    code_plan's own phases execute - this runner owns the whole flow.

    Like make_epub_book / make_pdf_book, the number of phases is DYNAMIC. The
    PLAN phase decides how many parts the site needs (header, sidebar, footer,
    each page section, ...). After scaffolding, the runner drives one BUILD
    phase and one immediately-following VERIFY phase per component, then a
    single FINAL_VERIFY for the whole site. ``total_phases`` becomes
    ``2 * len(component_plan) + 4`` once the plan reveals the component count.
    """

    workflow_name = "site_imitate"
    total_phases = 4  # placeholder; set dynamically after the plan phase

    async def run(self, intent: str) -> SiteImitateContext:
        """Drive analyze -> plan -> scaffold -> (build -> verify) x N -> final_verify."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        # Parse intent - expects format: "TARGET_URL | NEW_PURPOSE"
        parts = intent.split("|", 1)
        target_url = parts[0].strip() if parts else ""
        new_purpose = parts[1].strip() if len(parts) > 1 else ""

        ctx = SiteImitateContext(
            intent=intent,
            target_url=target_url,
            new_purpose=new_purpose,
            run_id=run_id,
            state=SiteImitateState.ANALYZE,
            shared_memory=memory,
        )
        ctx.catalog = _load_ntui_catalog()
        if handle is not None:
            handle.attach_context(ctx)
        state = ctx.state

        while not state.is_terminal:
            ctx.state = state
            ctx.phase_iteration += 1
            if handle is not None:
                handle.attach_context(ctx)
                handle.update_phase(
                    state.name.lower(),
                    self._phase_index(state, ctx),
                    ctx.phase_iteration,
                )
            match state:
                case SiteImitateState.ANALYZE:
                    state = await self._analyze(ctx, memory)
                case SiteImitateState.PLAN:
                    state = await self._plan(ctx, memory)
                case SiteImitateState.SCAFFOLD:
                    state = await self._scaffold(ctx, memory)
                case SiteImitateState.BUILD:
                    state = await self._build_component(ctx, memory)
                case SiteImitateState.VERIFY:
                    state = await self._verify_component(ctx, memory)
                case SiteImitateState.FINAL_VERIFY:
                    state = await self._final_verify(ctx, memory)
            log.info("site_imitate -> %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> SiteImitateContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, SiteImitateContext):
            raise TypeError("site_imitate resume requires SiteImitateContext")
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else context.shared_memory
        )
        if memory is None:
            memory = ShortTermMemory(
                max_tokens=self._cfg.cfg.execution.effective_usable_budget()
            )
        context.shared_memory = memory
        context.catalog = _load_ntui_catalog()
        if context.component_plan:
            self.total_phases = 2 * len(context.component_plan) + 4
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
                    state.name.lower(),
                    self._phase_index(state, context),
                    context.phase_iteration,
                )
            match state:
                case SiteImitateState.ANALYZE:
                    state = await self._analyze(context, memory)
                case SiteImitateState.PLAN:
                    state = await self._plan(context, memory)
                case SiteImitateState.SCAFFOLD:
                    state = await self._scaffold(context, memory)
                case SiteImitateState.BUILD:
                    state = await self._build_component(context, memory)
                case SiteImitateState.VERIFY:
                    state = await self._verify_component(context, memory)
                case SiteImitateState.FINAL_VERIFY:
                    state = await self._final_verify(context, memory)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: SiteImitateState, ctx: SiteImitateContext) -> int:
        """Return the dynamic status-bar position for *state*.

        analyze=0, plan=1, scaffold=2, build=3+2*i, verify=4+2*i (i = component
        index), final_verify=3+2*n. With N components the total is 2N+4.
        """
        n: int = len(ctx.component_plan)
        if state is SiteImitateState.ANALYZE:
            return 0
        if state is SiteImitateState.PLAN:
            return 1
        if state is SiteImitateState.SCAFFOLD:
            return 2
        if state is SiteImitateState.BUILD:
            return 3 + 2 * ctx.component_index
        if state is SiteImitateState.VERIFY:
            return 4 + 2 * ctx.component_index
        if state is SiteImitateState.FINAL_VERIFY:
            return 3 + 2 * n
        return 0


    async def _analyze(self, ctx: SiteImitateContext, memory: object) -> SiteImitateState:
        """Loop until submit_analysis fires; return PLAN or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Visit the reference website and analyze its structure.\n"
                    f"Target URL: {ctx.target_url}\n"
                    f"New purpose: {ctx.new_purpose}\n\n"
                    f"Use playwright_open to visit the URL, then playwright_snapshot "
                    f"and playwright_screenshot to capture the pages. Navigate to key "
                    f"sub-pages. Document the site structure, layouts, components, "
                    f"navigation, color scheme, typography, and user flows.\n\n"
                    f"For every distinct UI element you observe, call "
                    f"lookup_component(description) with your sloppy description of "
                    f"it (how it looks or where it sits) to get its canonical name, "
                    f"API symbols (shadcn/ui first), and a paste-ready build prompt. "
                    f"Then record a compact Named Component Inventory - one line per "
                    f"element, 'observed -> Canonical Name (api symbol)' - and pass "
                    f"it to submit_analysis(analysis, components=...). Elements with "
                    f"no dictionary match are described from scratch."
                    if attempt == 1
                    else "Call submit_analysis(analysis, components=...) now with your complete findings."
                ),
                system_prompt=(
                    "You are in the ANALYZE phase of site_imitate. Your job is to visit "
                    "the reference website using the playwright_* browser tools, explore it "
                    "thoroughly, and document everything: page structure, layouts, components, "
                    "navigation patterns, color scheme, typography, interactive features, and "
                    "user flows. Take multiple snapshots and screenshots.\n\n"
                    "Use the lookup_component(description) tool to name every distinct UI "
                    "element you observe - describe it in sloppy words ('the three lines "
                    "that open a side panel', 'the gray text that disappears when you type') "
                    "and get back its canonical name, API symbols (shadcn/ui first), and a "
                    "build prompt. This gives the plan and build phases a shared, canonical "
                    "vocabulary instead of free-form guesses.\n\n"
                    "Then call submit_analysis(analysis, components=...) with your complete "
                    "structured analysis, and pass the Named Component Inventory as "
                    "components= (one line per element: 'observed -> Canonical Name (symbol)'). "
                    "Only a successful submit_analysis(analysis, components=...) call "
                    "advances ANALYZE to PLAN; prose such as 'done' never advances the "
                    "workflow."
                ),
                max_turns=20,
                shared_memory=memory,
                tools=_make_analyze_tools(event, data, ctx.catalog),
            )
            if event.is_set():
                ctx.analysis = data.get("analysis", "")
                ctx.component_inventory = data.get("components", "")
                ctx.artifacts["analysis"] = ctx.analysis
                if ctx.component_inventory:
                    ctx.artifacts["component_inventory"] = ctx.component_inventory
                return SiteImitateState.PLAN

        ctx.fail_reason = "analyze phase never called submit_analysis()"
        return SiteImitateState.FAILED

    async def _plan(self, ctx: SiteImitateContext, memory: object) -> SiteImitateState:
        """Loop until submit_plan fires; parse the component list; return SCAFFOLD, ANALYZE, or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Reference site analysis:\n{ctx.analysis}\n\n"
                    f"Named Component Inventory:\n"
                    f"{ctx.component_inventory or '(none recorded)'}\n\n"
                    f"New purpose: {ctx.new_purpose}\n\n"
                    f"Design the architecture for the new website and DECIDE THE "
                    f"COMPONENT LIST: break the site into its distinct parts (header, "
                    f"sidebar, footer, and each page section/feature). Every part becomes "
                    f"its own build+verify phase."
                    if attempt == 1
                    else "Call submit_plan(plan, components=[...]) now."
                ),
                system_prompt=(
                    "You are in the PLAN phase of site_imitate. Based on the analysis of the "
                    "reference website, design the architecture for the new website. Include: "
                    "pages/routes, tech stack (NextJS App Router, Tailwind CSS v4, "
                    "shadCN/ui, TanStack Query, Stripe if payments needed), data sources, "
                    "directory structure, and an ordered build plan.\n\n"
                    "Write the plan in terms of the Named Component Inventory from the "
                    "analysis: for each component, name it by its canonical dictionary name "
                    "and use the shadcn/ui symbol when one exists (e.g. Sheet side=\"left\" "
                    "for the nav drawer, <Pagination /> for the page control); when no "
                    "shadcn/ui primitive exists, plan to hand-roll the component with its "
                    "canonical anatomy (the ARIA/HTML/CSS symbols from the inventory).\n\n"
                    "CRITICAL - the components list: call submit_plan(plan, components=[...]) "
                    "where components is the ordered list of distinct website parts, one "
                    "string per part in the format 'NAME | BUILD SPEC | VERIFY CHECK'. "
                    "Examples: 'Nav Drawer | Sheet side=\"left\" with the three-line button "
                    "(aria-expanded + aria-controls) | button toggles the panel and the Sheet "
                    "primitive is used', 'Header | sticky top bar with logo, nav links, mobile "
                    "hamburger | header is sticky and the logo and links render'. Cover every "
                    "site-wide part (header, sidebar, footer, layout shell) and each page "
                    "section. The workflow builds and verifies each part in its own phase, in "
                    "the order given. If you need more information about the reference site, "
                    "call request_reanalysis(question) to branch back to ANALYZE. Call "
                    "submit_plan(plan, components=[...]) to advance to SCAFFOLD. Only a "
                    "successful submit_plan() or request_reanalysis() transition-tool call "
                    "changes phase; prose never advances the workflow."
                ),
                max_turns=15,
                shared_memory=memory,
                tools=_make_plan_tools(event, data),
            )
            if event.is_set():
                action = data.get("action", "")
                if action == "reanalyze":
                    ctx.artifacts["reanalysis_question"] = data.get("question", "")
                    return SiteImitateState.ANALYZE
                ctx.plan = data.get("plan", "")
                ctx.artifacts["plan"] = ctx.plan
                raw_components = data.get("components", "")
                if isinstance(raw_components, str) and raw_components:
                    import json as _json

                    try:
                        raw_components = _json.loads(raw_components)
                    except Exception:  # noqa: BLE001 - fall back to splitting lines
                        raw_components = [ln for ln in raw_components.splitlines() if ln.strip()]
                ctx.component_plan = _parse_component_plan(raw_components)
                if not ctx.component_plan:
                    # No structured list - treat the plan itself as one component.
                    ctx.component_plan = [{"name": "The Website", "build": ctx.plan, "verify": ""}]
                self.total_phases = 2 * len(ctx.component_plan) + 4
                ctx.component_index = 0
                ctx.artifacts["component_plan"] = _component_plan_text(ctx)
                log.info(
                    "site_imitate plan: %d components -> total_phases %d",
                    len(ctx.component_plan),
                    self.total_phases,
                )
                return SiteImitateState.SCAFFOLD

        ctx.fail_reason = "plan phase never called submit_plan() or request_reanalysis()"
        return SiteImitateState.FAILED

    async def _scaffold(self, ctx: SiteImitateContext, memory: object) -> SiteImitateState:
        """Loop until scaffold_complete fires; return BUILD (or FINAL_VERIFY) or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Build plan:\n{ctx.plan}\n\n"
                    f"Component plan:\n{_component_plan_text(ctx)}\n\n"
                    f"Create the NextJS project and install all dependencies."
                    if attempt == 1
                    else "Call scaffold_complete(path) now."
                ),
                system_prompt=(
                    "You are in the SCAFFOLD phase of site_imitate. Create the NextJS project "
                    "(npx create-next-app), install all dependencies: tailwind CSS, shadCN/ui, "
                    "@tanstack/react-query, lucide-react, stripe if needed. Configure Tailwind "
                    "CSS, set up the project directory structure with component folders. "
                    "Call scaffold_complete(path) with the project directory path when done. "
                    "Only a successful scaffold_complete(path) call advances to BUILD (or "
                    "FINAL VERIFY when there are no components); prose never advances the "
                    "workflow."
                ),
                mode="Yolo",
                max_turns=15,
                shared_memory=memory,
                tools=_make_scaffold_tools(event, data),
            )
            if event.is_set():
                project_path = data.get("path", "")
                ctx.artifacts["project_path"] = project_path
                if ctx.component_plan:
                    return SiteImitateState.BUILD
                return SiteImitateState.FINAL_VERIFY

        ctx.fail_reason = "scaffold phase never called scaffold_complete()"
        return SiteImitateState.FAILED

    def _current_component(self, ctx: SiteImitateContext) -> dict:
        """Return the component spec for the current component index."""
        if ctx.component_plan and 0 <= ctx.component_index < len(ctx.component_plan):
            return ctx.component_plan[ctx.component_index]
        return {"name": "component", "build": "", "verify": ""}

    async def _build_component(self, ctx: SiteImitateContext, memory: object) -> SiteImitateState:
        """Build the current component; return VERIFY or FAILED."""
        comp = self._current_component(ctx)
        label = f"{ctx.component_index + 1}/{len(ctx.component_plan)} - {comp.get('name') or 'component'}"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Build plan:\n{ctx.plan}\n\n"
                    f"Project path: {ctx.artifacts.get('project_path', '')}\n\n"
                    f"NOW BUILDING COMPONENT {label}:\n"
                    f"  name : {comp.get('name') or '?'}\n"
                    f"  spec : {comp.get('build') or '(from the plan)'}\n\n"
                    f"Implement ONLY this component's files for the new site. Do not "
                    f"implement the other planned components yet - each has its own phase."
                    if attempt == 1
                    else "Call component_built() now."
                ),
                system_prompt=(
                    "You are in the BUILD phase of site_imitate. You are building ONE "
                    f"component of the new site: '{comp.get('name') or '?'}' (component "
                    f"{ctx.component_index + 1} of {len(ctx.component_plan)}).\n\n"
                    f"Component spec:\n{comp.get('build') or '(follow the plan)'}\n\n"
                    "Build this component using its canonical API symbols and anatomy - when a "
                    "shadcn/ui symbol is listed (e.g. Sheet side=\"left\"), use that primitive; "
                    "otherwise hand-roll the component with its canonical ARIA/HTML/CSS anatomy. "
                    "You may call lookup_component(description) to retrieve the full paste-ready "
                    "build prompt. Write the component's source files into the project created in "
                    "the scaffold phase. Build ONLY this component - the other components have "
                    "their own phases. Call component_built() when done. Only a successful "
                    "component_built() call advances BUILD to VERIFY; prose never advances "
                    "the workflow."
                ),
                mode="Yolo",
                max_turns=40,
                shared_memory=memory,
                tools=_make_build_tools(event, data, ctx.catalog),
            )
            if event.is_set():
                return SiteImitateState.VERIFY

        ctx.fail_reason = f"build phase never called component_built() for {label}"
        return SiteImitateState.FAILED

    async def _verify_component(self, ctx: SiteImitateContext, memory: object) -> SiteImitateState:
        """Verify the current component; pass advances, fail re-enters its build."""
        comp = self._current_component(ctx)
        label = f"{ctx.component_index + 1}/{len(ctx.component_plan)} - {comp.get('name') or 'component'}"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Project path: {ctx.artifacts.get('project_path', '')}\n\n"
                    f"VERIFYING COMPONENT {label}:\n"
                    f"  verify checks: {comp.get('verify') or '(compiles and matches the reference)'}"
                    if attempt == 1
                    else "Call component_verified(summary) or component_verification_failed(errors) now."
                ),
                system_prompt=(
                    "You are in the VERIFY phase of site_imitate. Verify the component just "
                    f"built: '{comp.get('name') or '?'}' (component {ctx.component_index + 1} of "
                    f"{len(ctx.component_plan)}).\n\n"
                    f"Verify checks:\n{comp.get('verify') or '(compiles and matches the reference)'}\n\n"
                    "Check that the component compiles (e.g. npx tsc --noEmit, or the project's "
                    "type check), uses the canonical anatomy from its spec (e.g. the Sheet "
                    "primitive, aria-expanded + aria-controls, a real <label for>), and matches "
                    "the reference site. If it passes, call component_verified(summary). If it "
                    "fails, call component_verification_failed(errors) - the build phase for "
                    "this component will re-run. Only a successful component_verified() or "
                    "component_verification_failed() transition-tool call changes phase; "
                    "prose never advances the workflow."
                ),
                max_turns=10,
                shared_memory=memory,
                tools=_make_verify_tools(event, data),
            )
            if event.is_set():
                action = data.get("action", "")
                if action == "pass":
                    ctx.artifacts[f"verify_{ctx.component_index}"] = data.get("summary", "")
                    ctx.component_index += 1
                    if ctx.component_index < len(ctx.component_plan):
                        return SiteImitateState.BUILD
                    return SiteImitateState.FINAL_VERIFY
                ctx.artifacts[f"verify_errors_{ctx.component_index}"] = data.get("errors", "")
                return SiteImitateState.BUILD

        ctx.fail_reason = f"verify phase never reported a verdict for {label}"
        return SiteImitateState.FAILED

    async def _final_verify(self, ctx: SiteImitateContext, memory: object) -> SiteImitateState:
        """Whole-site build + route check; pass -> COMPLETE, fail -> last component build."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Run the full build and verify the whole site compiles.\n"
                    f"Project path: {ctx.artifacts.get('project_path', '')}\n"
                    f"Component plan:\n{_component_plan_text(ctx)}"
                    if attempt == 1
                    else "Call final_verify_passed(summary) or final_verify_failed(errors) now."
                ),
                system_prompt=(
                    "You are in the FINAL VERIFY phase of site_imitate. Run 'npx next build' in "
                    "the project directory and check for TypeScript errors and build output. "
                    "Verify all routes compile and every planned component from the component "
                    "plan is present and wired into the pages.\n\n"
                    "If the build succeeds, call final_verify_passed(summary) with the build "
                    "output. If it fails, call final_verify_failed(errors) with the errors - "
                    "the last component's build phase will re-run so you can fix them. Only "
                    "a successful final_verify_passed() or final_verify_failed() call "
                    "changes phase; prose never advances the workflow."
                ),
                max_turns=10,
                shared_memory=memory,
                tools=_make_final_verify_tools(event, data),
            )
            if event.is_set():
                action = data.get("action", "")
                if action == "pass":
                    ctx.artifacts["verify_summary"] = data.get("summary", "")
                    return SiteImitateState.COMPLETE
                ctx.artifacts["verify_errors"] = data.get("errors", "")
                if ctx.component_plan:
                    ctx.component_index = max(0, len(ctx.component_plan) - 1)
                return SiteImitateState.BUILD

        ctx.fail_reason = "final verify phase never reported a verdict"
        return SiteImitateState.FAILED


@dataclasses.dataclass
class SiteImitateParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.site_imitate]."""

    analyze_model: str = ""
    plan_model: str = ""
    scaffold_model: str = ""
    build_model: str = ""
    verify_model: str = ""
    final_verify_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "analyze": self.analyze_model,
            "plan": self.plan_model,
            "scaffold": self.scaffold_model,
            "build": self.build_model,
            "verify": self.verify_model,
            "final_verify": self.final_verify_model,
        }


class SiteImitateWorkflow(WorkflowPlugin):
    """Visit a reference website, analyze it, then build a similar site for a new use case."""

    name = "site_imitate"
    description = "Imitate a reference website's design for a new use case using NextJS, Tailwind, shadCN/ui, and TanStack Query."
    mode_bindings = []  # manual only — invoke with /workflow site_imitate

    phases = [
        PhaseSpec(
            name="analyze",
            agent_type="auto",
            max_turns=20,
            next="plan",
            on_reject="analyze",
            system_prompt_override=(
                "You are in the ANALYZE phase of site_imitate. Call "
                "submit_analysis(analysis, components=...) to advance to PLAN. Only a "
                "successful transition-tool call changes phase; prose never advances it."
            ),
        ),
        PhaseSpec(
            name="plan",
            agent_type="auto",
            max_turns=15,
            next="scaffold",
            on_reject="analyze",
            system_prompt_override=(
                "You are in the PLAN phase of site_imitate. Call "
                "submit_plan(plan, components=[...]) to advance to SCAFFOLD, or call "
                "request_reanalysis(question) to branch back to ANALYZE. Only a successful "
                "transition-tool call changes phase; prose never advances it."
            ),
        ),
        PhaseSpec(
            name="scaffold",
            agent_type="auto",
            max_turns=15,
            next="build",
            on_reject="scaffold",
            mode_override="Yolo",
            system_prompt_override=(
                "You are in the SCAFFOLD phase of site_imitate. Call "
                "scaffold_complete(path) to advance to BUILD or FINAL VERIFY. Only a "
                "successful transition-tool call changes phase; prose never advances it."
            ),
        ),
        # The build/verify phases below are a static skeleton; the runner
        # re-enters them once per planned component (like make_pdf_book's
        # chapter loop), so the effective phase count is 2*N + 4.
        PhaseSpec(
            name="build",
            agent_type="auto",
            max_turns=40,
            next="verify",
            on_reject="plan",
            mode_override="Yolo",
            system_prompt_override=(
                "You are in the BUILD phase of site_imitate. Call component_built() to "
                "advance to VERIFY. Only a successful transition-tool call changes phase; "
                "prose never advances it."
            ),
        ),
        PhaseSpec(
            name="verify",
            agent_type="auto",
            max_turns=10,
            next="build",
            on_reject="build",
            system_prompt_override=(
                "You are in the VERIFY phase of site_imitate. Call "
                "component_verified(summary) to advance, or "
                "component_verification_failed(errors) to return to BUILD. Only a "
                "successful transition-tool call changes phase; prose never advances it."
            ),
        ),
        PhaseSpec(
            name="final_verify",
            agent_type="auto",
            max_turns=10,
            next="complete",
            on_reject="build",
            system_prompt_override=(
                "You are in the FINAL VERIFY phase of site_imitate. Call "
                "final_verify_passed(summary) to complete, or "
                "final_verify_failed(errors) to return to BUILD. Only a successful "
                "transition-tool call changes phase; prose never advances it."
            ),
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, SiteImitateContext):
            raise TypeError("site_imitate checkpoint requires SiteImitateContext")
        return {
            "intent": context.intent,
            "target_url": context.target_url,
            "new_purpose": context.new_purpose,
            "run_id": context.run_id,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "fail_reason": context.fail_reason,
            "analysis": context.analysis,
            "plan": context.plan,
            "component_inventory": context.component_inventory,
            "component_plan": context.component_plan,
            "component_index": context.component_index,
            "artifacts": context.artifacts,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> SiteImitateContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", SiteImitateState.ANALYZE.name))
        try:
            state = SiteImitateState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown site_imitate state: {raw_state}") from exc
        raw_artifacts = payload.get("artifacts", {})
        artifacts = (
            {str(key): str(value) for key, value in raw_artifacts.items()}
            if isinstance(raw_artifacts, dict)
            else {}
        )
        return SiteImitateContext(
            intent=str(payload.get("intent", "")),
            target_url=str(payload.get("target_url", "")),
            new_purpose=str(payload.get("new_purpose", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=int(payload.get("phase_iteration", 0)),
            fail_reason=str(payload.get("fail_reason", "")),
            analysis=str(payload.get("analysis", "")),
            plan=str(payload.get("plan", "")),
            component_inventory=str(payload.get("component_inventory", "")),
            component_plan=[
                {str(k): str(v) for k, v in item.items()}
                for item in payload.get("component_plan", [])
                if isinstance(item, dict)
            ],
            component_index=int(payload.get("component_index", 0)),
            artifacts=artifacts,
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> SiteImitateRunner:
        """Return this workflow's own state-machine runner."""
        return SiteImitateRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.site_imitate]."""
        return SiteImitateParams(
            analyze_model=str(source.get("analyze_model", "") or ""),
            plan_model=str(source.get("plan_model", "") or ""),
            scaffold_model=str(source.get("scaffold_model", "") or ""),
            build_model=str(source.get("build_model", "") or ""),
            verify_model=str(source.get("verify_model", "") or ""),
            final_verify_model=str(source.get("final_verify_model", "") or ""),
        )
