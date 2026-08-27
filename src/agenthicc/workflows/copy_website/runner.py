"""copy_website — study a live website with Playwright, then rebuild it.

A 10-phase fully automated workflow: extract the target URL, study the site
across desktop and mobile viewports with Playwright, produce a design spec,
scaffold a Next.js + Tailwind + shadcn/ui + TanStack Query project, implement
layout / pages / data, polish responsiveness, verify parity side-by-side, and
write the final report. All phase transitions happen only through explicit
transition-tool calls; prose never advances the workflow.
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
#: Bounded fix loop for parity verification rejections.
_MAX_FIX_ITERATIONS = 5

# Stable workflow policy.  Keep phase instructions and current artifacts in the
# dynamic ``system_prompt`` argument to ``run_phase``.
CACHE_CONTRACT = """[CACHE-STABLE WORKFLOW POLICY]
Keep this workflow contract unchanged across phases. Ask the user a focused
clarifying question through the existing `ask_user` tool whenever required
information is missing, ambiguous, or would materially change the result.
Wait for the answer; do not guess. Actual questions and answers, phase state,
and artifacts are dynamic context and do not belong here.
"""


class CopyWebsiteState(Enum):
    """Every state this workflow can be in."""

    EXTRACT_TARGET = auto()
    SITE_STUDY = auto()
    DESIGN_SPEC = auto()
    SCAFFOLD = auto()
    IMPLEMENT_LAYOUT = auto()
    IMPLEMENT_PAGES = auto()
    IMPLEMENT_DATA = auto()
    RESPONSIVE_PASS = auto()
    PARITY_VERIFY = auto()
    FINAL_REPORT = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()    # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (CopyWebsiteState.COMPLETE, CopyWebsiteState.FAILED)


@dataclasses.dataclass
class CopyContext:
    """Data carried across every phase of one run."""

    intent: str
    run_id: str
    state: CopyWebsiteState
    phase_iteration: int = 0

    # Phase outputs
    target_url: str = ""
    target_pages: list[str] = dataclasses.field(default_factory=list)
    study_report: str = ""
    screenshots: list[str] = dataclasses.field(default_factory=list)
    design_spec: str = ""
    design_tokens: str = ""
    component_map: str = ""
    data_plan: str = ""
    project_path: str = ""
    layout_files: str = ""
    pages_built: list[str] = dataclasses.field(default_factory=list)
    data_hooks: str = ""
    responsive_verified: str = ""
    parity_summary: str = ""
    fix_reason: str = ""
    fix_iterations: int = 0
    fail_reason: str = ""
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)



def _make_extract_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the extract_target phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_target(url: str, scope: str, pages: str) -> dict[str, object]:
        """Record the target website and advance to the site_study phase.

        Args:
            url: The target website URL to study.
            scope: The replication scope (e.g. 'sitemap' or a page list).
            pages: Comma-separated list of discovered page paths, if known.
        """
        if not url.strip():
            return {
                "ok": False,
                "error": "The URL was rejected: it must not be empty.",
                "fix": "Call submit_target(url, scope, pages) with a non-empty URL.",
            }
        data["url"] = url.strip()
        data["scope"] = scope.strip() or "sitemap"
        data["pages"] = pages.strip()
        event.set()
        return {"ok": True, "message": "Target recorded. The site_study phase starts next."}

    return [submit_target]


def _make_study_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the site_study phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_study_report(
        report: str,
        pages: str,
        screenshots: str,
    ) -> dict[str, object]:
        """Record the study report and advance to the design_spec phase.

        Args:
            report: Detailed observations: layout, colors, typography, spacing,
                motion, breakpoints, component inventory, nav structure.
            pages: Comma-separated list of pages discovered on the site.
            screenshots: Comma-separated list of saved screenshot file paths.
        """
        if not report.strip():
            return {
                "ok": False,
                "error": "The report was rejected: it must not be empty.",
                "fix": "Call submit_study_report(report, pages, screenshots) with observations.",
            }
        data["report"] = report.strip()
        data["pages"] = pages.strip()
        data["screenshots"] = screenshots.strip()
        event.set()
        return {"ok": True, "message": "Study report recorded. The design_spec phase starts next."}

    return [submit_study_report]


def _make_design_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the design_spec phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_design_spec(
        spec: str,
        tokens: str,
        components: str,
        data_plan: str,
    ) -> dict[str, object]:
        """Record the design spec and advance to the scaffold phase.

        Args:
            spec: Full page-by-page design specification.
            tokens: Design tokens (colors, fonts, radii, shadows, spacing).
            components: shadcn/ui component inventory mapped to page sections.
            data_plan: TanStack Query data plan (queries, keys, sources).
        """
        if not spec.strip():
            return {
                "ok": False,
                "error": "The spec was rejected: it must not be empty.",
                "fix": "Call submit_design_spec(spec, tokens, components, data_plan) with the design.",
            }
        data["spec"] = spec.strip()
        data["tokens"] = tokens.strip()
        data["components"] = components.strip()
        data["data_plan"] = data_plan.strip()
        event.set()
        return {"ok": True, "message": "Design spec recorded. The scaffold phase starts next."}

    return [submit_design_spec]



def _make_scaffold_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the scaffold phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_scaffold(project_path: str) -> dict[str, object]:
        """Record the scaffolded project path and advance to implement_layout.

        Args:
            project_path: Path where the Next.js project was created.
        """
        if not project_path.strip():
            return {
                "ok": False,
                "error": "The path was rejected: it must not be empty.",
                "fix": "Call submit_scaffold(project_path) with the project location.",
            }
        data["project_path"] = project_path.strip()
        event.set()
        return {"ok": True, "message": "Scaffold recorded. The implement_layout phase starts next."}

    return [submit_scaffold]


def _make_layout_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the implement_layout phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_layout(files: str) -> dict[str, object]:
        """Record the layout implementation and advance to implement_pages.

        Args:
            files: Comma-separated list of layout files written.
        """
        if not files.strip():
            return {
                "ok": False,
                "error": "The file list was rejected: it must not be empty.",
                "fix": "Call submit_layout(files) with the layout files written.",
            }
        data["files"] = files.strip()
        event.set()
        return {"ok": True, "message": "Layout recorded. The implement_pages phase starts next."}

    return [submit_layout]


def _make_pages_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the implement_pages phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_pages(pages_built: str) -> dict[str, object]:
        """Record the built pages and advance to implement_data.

        Args:
            pages_built: Comma-separated list of page routes built.
        """
        if not pages_built.strip():
            return {
                "ok": False,
                "error": "The page list was rejected: it must not be empty.",
                "fix": "Call submit_pages(pages_built) with the routes built.",
            }
        data["pages_built"] = pages_built.strip()
        event.set()
        return {"ok": True, "message": "Pages recorded. The implement_data phase starts next."}

    return [submit_pages]


def _make_data_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the implement_data phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_data_layer(hooks: str) -> dict[str, object]:
        """Record the data layer and advance to responsive_pass.

        Args:
            hooks: Comma-separated list of TanStack Query hooks/providers written.
        """
        if not hooks.strip():
            return {
                "ok": False,
                "error": "The hook list was rejected: it must not be empty.",
                "fix": "Call submit_data_layer(hooks) with the hooks/providers written.",
            }
        data["hooks"] = hooks.strip()
        event.set()
        return {"ok": True, "message": "Data layer recorded. The responsive_pass phase starts next."}

    return [submit_data_layer]


def _make_responsive_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the responsive_pass phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def submit_responsive(verified: str) -> dict[str, object]:
        """Record responsive verification and advance to parity_verify.

        Args:
            verified: Summary of mobile-friendliness verification results.
        """
        if not verified.strip():
            return {
                "ok": False,
                "error": "The verification summary was rejected: it must not be empty.",
                "fix": "Call submit_responsive(verified) with what was checked.",
            }
        data["verified"] = verified.strip()
        event.set()
        return {"ok": True, "message": "Responsive pass recorded. The parity_verify phase starts next."}

    return [submit_responsive]



def _make_parity_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the pass/block decision tools for the parity_verify phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def approve_parity(summary: str) -> dict[str, object]:
        """Signal that the copy passes parity verification.

        Args:
            summary: What was verified and how the copy matches the original.
        """
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True}

    @tool_control
    @tool()
    async def reject_parity(reason: str) -> dict[str, object]:
        """Signal that parity failed and fixes are required.

        Args:
            reason: The concrete gap that must be fixed.
        """
        data["action"] = "reject"
        data["reason"] = reason.strip()
        event.set()
        return {"ok": True}

    return [approve_parity, reject_parity]



# Phase prompts.  Each names its exact transition tool(s) and states that only
# a successful transition-tool call changes phase; prose never advances.
PHASE_PROMPTS = {
    "extract_target": """You are in the EXTRACT_TARGET phase of copy_website.
Parse the target website URL from the intent (e.g. `/workflow copy_website https://example.com`).
If the URL is missing or ambiguous, ask the user through the existing `ask_user` tool and wait.
Record the target and any known pages by calling submit_target(url, scope, pages).
Only a successful submit_target(url, scope, pages) call changes phase; prose such as 'done' never advances the workflow. Do not study the site yet.""",

    "site_study": """You are in the SITE_STUDY phase of copy_website.
Use the Playwright browser tools to visit the target URL and study the site carefully:
- open the site and take snapshots at desktop (1440x900) and mobile (390x844) viewports
- click through the navigation to discover as many pages as the sitemap/nav reveals
- record layout structure, colors, typography, spacing, motion, breakpoints, component inventory
- take bounded screenshots of key pages and save them as workspace artifacts
Write fresh observations (do not copy the site's text verbatim — we will write original copy later).
Call submit_study_report(report, pages, screenshots) when done.
Only a successful submit_study_report() call changes phase; prose such as 'done' never advances the workflow.""",

    "design_spec": """You are in the DESIGN_SPEC phase of copy_website.
Turn the study report into a complete design specification:
- design tokens: colors, fonts, radii, shadows, spacing scale, breakpoints
- shadcn/ui component inventory mapped to each page section
- page architecture: routes, layout components, section order
- TanStack Query data plan: query keys, fetch sources, loading/error states
Call submit_design_spec(spec, tokens, components, data_plan) when complete.
Only a successful submit_design_spec() call changes phase; prose such as 'done' never advances the workflow.""",

    "scaffold": """You are in the SCAFFOLD phase of copy_website.
Scaffold the project with the stack: Next.js (App Router), Tailwind CSS, shadcn/ui, @tanstack/react-query.
- create the Next.js app (e.g. create-next-app) with TypeScript and Tailwind
- run shadcn init and add the components from the component inventory
- install @tanstack/react-query and set up the directory structure
Call submit_scaffold(project_path) with the project location when done.
Only a successful submit_scaffold() call changes phase; prose such as 'done' never advances the workflow.""",

    "implement_layout": """You are in the IMPLEMENT_LAYOUT phase of copy_website.
Build the application shell per the design spec:
- root layout, theme provider, global styles and Tailwind theme from the design tokens
- navigation (desktop + mobile menu), footer, shared layout components
{fix_context}
Call submit_layout(files) with the layout files written when done.
Only a successful submit_layout() call changes phase; prose such as 'done' never advances the workflow.""",

    "implement_pages": """You are in the IMPLEMENT_PAGES phase of copy_website.
Build every page route from the design spec using the shadcn/ui components and Tailwind utilities:
- replicate layout, spacing, and visual style to match the original's taste
- write FRESH, ORIGINAL copy — do not reuse the target site's text verbatim
{fix_context}
Call submit_pages(pages_built) with the routes built when done.
Only a successful submit_pages() call changes phase; prose such as 'done' never advances the workflow.""",

    "implement_data": """You are in the IMPLEMENT_DATA phase of copy_website.
Wire the TanStack Query data layer per the data plan:
- QueryClientProvider setup, query hooks, query keys, data sources
- loading / error / empty states wired into the pages
Call submit_data_layer(hooks) with the hooks/providers written when done.
Only a successful submit_data_layer() call changes phase; prose such as 'done' never advances the workflow.""",

    "responsive_pass": """You are in the RESPONSIVE_PASS phase of copy_website.
Make sure the site is genuinely mobile-friendly:
- verify every page at mobile viewports (e.g. 390x844) with Playwright screenshots
- fix overflow, tap-target sizes, menu behavior, and responsive grids
- check breakpoint behavior between mobile and desktop
Call submit_responsive(verified) with a summary when done.
Only a successful submit_responsive() call changes phase; prose such as 'done' never advances the workflow.""",

    "parity_verify": """You are in the PARITY_VERIFY phase of copy_website.
Verify the copy matches the original in taste and style:
- run `npm run build` in the project and fix build errors
- start the dev server and take side-by-side Playwright screenshots of the original
  vs the copy at desktop and mobile viewports
- compare layout, colors, typography, spacing, motion, and mobile-friendliness
- confirm as many pages as the sitemap revealed are covered
Call approve_parity(summary) if the copy passes, or reject_parity(reason) with the concrete gap.
Only a successful call to approve_parity() or reject_parity() changes phase; prose never advances the workflow.""",

    "final_report": """You are in the FINAL_REPORT phase of copy_website.
Write the completion summary: what was studied, what was built, the project path,
pages covered, and the parity result. No phase-transition tool is available in
this terminal phase; the runner owns completion.""",
}


class CopyWebsiteRunner(CodePlanRunner):
    """State-machine runner for copy_website."""

    workflow_name = "copy_website"
    total_phases = 10

    async def run(self, intent: str) -> CopyContext:
        """Drive the 10-phase workflow."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        ctx = CopyContext(
            intent=intent,
            run_id=run_id,
            state=CopyWebsiteState.EXTRACT_TARGET,
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
                handle.update_phase(state.name.lower(), self._phase_index(state), ctx.phase_iteration)
            match state:
                case CopyWebsiteState.EXTRACT_TARGET:
                    state = await self._extract_target(ctx, memory)
                case CopyWebsiteState.SITE_STUDY:
                    state = await self._site_study(ctx, memory)
                case CopyWebsiteState.DESIGN_SPEC:
                    state = await self._design_spec(ctx, memory)
                case CopyWebsiteState.SCAFFOLD:
                    state = await self._scaffold(ctx, memory)
                case CopyWebsiteState.IMPLEMENT_LAYOUT:
                    state = await self._implement_layout(ctx, memory)
                case CopyWebsiteState.IMPLEMENT_PAGES:
                    state = await self._implement_pages(ctx, memory)
                case CopyWebsiteState.IMPLEMENT_DATA:
                    state = await self._implement_data(ctx, memory)
                case CopyWebsiteState.RESPONSIVE_PASS:
                    state = await self._responsive_pass(ctx, memory)
                case CopyWebsiteState.PARITY_VERIFY:
                    state = await self._parity_verify(ctx, memory)
                case CopyWebsiteState.FINAL_REPORT:
                    state = await self._final_report(ctx, memory)
            log.info("copy_website → %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> CopyContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, CopyContext):
            raise TypeError("copy_website resume requires CopyContext")
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
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
        state = context.state
        while not state.is_terminal:
            context.state = state
            context.phase_iteration += 1
            if handle is not None:
                handle.attach_context(context)
                handle.update_phase(state.name.lower(), self._phase_index(state), context.phase_iteration)
            match state:
                case CopyWebsiteState.EXTRACT_TARGET:
                    state = await self._extract_target(context, memory)
                case CopyWebsiteState.SITE_STUDY:
                    state = await self._site_study(context, memory)
                case CopyWebsiteState.DESIGN_SPEC:
                    state = await self._design_spec(context, memory)
                case CopyWebsiteState.SCAFFOLD:
                    state = await self._scaffold(context, memory)
                case CopyWebsiteState.IMPLEMENT_LAYOUT:
                    state = await self._implement_layout(context, memory)
                case CopyWebsiteState.IMPLEMENT_PAGES:
                    state = await self._implement_pages(context, memory)
                case CopyWebsiteState.IMPLEMENT_DATA:
                    state = await self._implement_data(context, memory)
                case CopyWebsiteState.RESPONSIVE_PASS:
                    state = await self._responsive_pass(context, memory)
                case CopyWebsiteState.PARITY_VERIFY:
                    state = await self._parity_verify(context, memory)
                case CopyWebsiteState.FINAL_REPORT:
                    state = await self._final_report(context, memory)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: CopyWebsiteState) -> int:
        return {
            CopyWebsiteState.EXTRACT_TARGET: 0,
            CopyWebsiteState.SITE_STUDY: 1,
            CopyWebsiteState.DESIGN_SPEC: 2,
            CopyWebsiteState.SCAFFOLD: 3,
            CopyWebsiteState.IMPLEMENT_LAYOUT: 4,
            CopyWebsiteState.IMPLEMENT_PAGES: 5,
            CopyWebsiteState.IMPLEMENT_DATA: 6,
            CopyWebsiteState.RESPONSIVE_PASS: 7,
            CopyWebsiteState.PARITY_VERIFY: 8,
            CopyWebsiteState.FINAL_REPORT: 9,
        }.get(state, 0)

    def _fix_context(self, ctx: CopyContext) -> str:
        """Return dynamic fix context for re-entered implementation phases."""
        if ctx.fix_reason and ctx.fix_iterations > 0:
            return (
                f"Parity verification was rejected (iteration {ctx.fix_iterations}): "
                f"{ctx.fix_reason}\nFix exactly these issues before calling the transition tool."
            )
        return ""

    async def _extract_target(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_target fires; return SITE_STUDY or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=ctx.intent if attempt == 1 else "Call submit_target(url, scope, pages) now.",
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["extract_target"],
                mode="Yolo",
                max_turns=10,
                shared_memory=memory,
                tools=_make_extract_tools(event, data),
            )
            if event.is_set():
                ctx.target_url = str(data.get("url", ""))
                ctx.artifacts["target_url"] = ctx.target_url
                pages = str(data.get("pages", ""))
                if pages:
                    ctx.target_pages = [p.strip() for p in pages.split(",") if p.strip()]
                return CopyWebsiteState.SITE_STUDY
        ctx.fail_reason = "extract_target phase never called submit_target()"
        return CopyWebsiteState.FAILED

    async def _site_study(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_study_report fires; return DESIGN_SPEC or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Study the target website now: {ctx.target_url}"
                    if attempt == 1
                    else "Call submit_study_report(report, pages, screenshots) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["site_study"],
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_study_tools(event, data),
            )
            if event.is_set():
                ctx.study_report = str(data.get("report", ""))
                pages = str(data.get("pages", ""))
                if pages:
                    ctx.target_pages = [p.strip() for p in pages.split(",") if p.strip()]
                shots = str(data.get("screenshots", ""))
                if shots:
                    ctx.screenshots = [s.strip() for s in shots.split(",") if s.strip()]
                ctx.artifacts["study_report"] = ctx.study_report
                ctx.artifacts["screenshots"] = shots
                return CopyWebsiteState.DESIGN_SPEC
        ctx.fail_reason = "site_study phase never called submit_study_report()"
        return CopyWebsiteState.FAILED

    async def _design_spec(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_design_spec fires; return SCAFFOLD or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Study report:\n{ctx.study_report}\n\nProduce the design spec."
                    if attempt == 1
                    else "Call submit_design_spec(spec, tokens, components, data_plan) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["design_spec"],
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_design_tools(event, data),
            )
            if event.is_set():
                ctx.design_spec = str(data.get("spec", ""))
                ctx.design_tokens = str(data.get("tokens", ""))
                ctx.component_map = str(data.get("components", ""))
                ctx.data_plan = str(data.get("data_plan", ""))
                ctx.artifacts["design_spec"] = ctx.design_spec
                return CopyWebsiteState.SCAFFOLD
        ctx.fail_reason = "design_spec phase never called submit_design_spec()"
        return CopyWebsiteState.FAILED

    async def _scaffold(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_scaffold fires; return IMPLEMENT_LAYOUT or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Design spec:\n{ctx.design_spec}\n\nTokens:\n{ctx.design_tokens}"
                    if attempt == 1
                    else "Call submit_scaffold(project_path) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["scaffold"],
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_scaffold_tools(event, data),
            )
            if event.is_set():
                ctx.project_path = str(data.get("project_path", ""))
                ctx.artifacts["project_path"] = ctx.project_path
                return CopyWebsiteState.IMPLEMENT_LAYOUT
        ctx.fail_reason = "scaffold phase never called submit_scaffold()"
        return CopyWebsiteState.FAILED

    async def _implement_layout(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_layout fires; return IMPLEMENT_PAGES or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            fix_context = self._fix_context(ctx)
            prompt = PHASE_PROMPTS["implement_layout"].replace("{fix_context}", fix_context)
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Project: {ctx.project_path}\nDesign spec:\n{ctx.design_spec}\nTokens:\n{ctx.design_tokens}"
                    if attempt == 1
                    else "Call submit_layout(files) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=prompt,
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_layout_tools(event, data),
            )
            if event.is_set():
                ctx.layout_files = str(data.get("files", ""))
                ctx.artifacts["layout_files"] = ctx.layout_files
                return CopyWebsiteState.IMPLEMENT_PAGES
        ctx.fail_reason = "implement_layout phase never called submit_layout()"
        return CopyWebsiteState.FAILED

    async def _implement_pages(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_pages fires; return IMPLEMENT_DATA or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            fix_context = self._fix_context(ctx)
            prompt = PHASE_PROMPTS["implement_pages"].replace("{fix_context}", fix_context)
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Project: {ctx.project_path}\nPages to build: {', '.join(ctx.target_pages) or 'from sitemap'}\nSpec:\n{ctx.design_spec}"
                    if attempt == 1
                    else "Call submit_pages(pages_built) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=prompt,
                mode="Yolo",
                max_turns=40,
                shared_memory=memory,
                tools=_make_pages_tools(event, data),
            )
            if event.is_set():
                pages = str(data.get("pages_built", ""))
                ctx.pages_built = [p.strip() for p in pages.split(",") if p.strip()]
                ctx.artifacts["pages_built"] = pages
                return CopyWebsiteState.IMPLEMENT_DATA
        ctx.fail_reason = "implement_pages phase never called submit_pages()"
        return CopyWebsiteState.FAILED

    async def _implement_data(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_data_layer fires; return RESPONSIVE_PASS or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Project: {ctx.project_path}\nData plan:\n{ctx.data_plan}"
                    if attempt == 1
                    else "Call submit_data_layer(hooks) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["implement_data"],
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_data_tools(event, data),
            )
            if event.is_set():
                ctx.data_hooks = str(data.get("hooks", ""))
                ctx.artifacts["data_hooks"] = ctx.data_hooks
                return CopyWebsiteState.RESPONSIVE_PASS
        ctx.fail_reason = "implement_data phase never called submit_data_layer()"
        return CopyWebsiteState.FAILED

    async def _responsive_pass(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until submit_responsive fires; return PARITY_VERIFY or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Project: {ctx.project_path}\nPages: {', '.join(ctx.pages_built)}"
                    if attempt == 1
                    else "Call submit_responsive(verified) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["responsive_pass"],
                mode="Yolo",
                max_turns=30,
                shared_memory=memory,
                tools=_make_responsive_tools(event, data),
            )
            if event.is_set():
                ctx.responsive_verified = str(data.get("verified", ""))
                ctx.artifacts["responsive_verified"] = ctx.responsive_verified
                return CopyWebsiteState.PARITY_VERIFY
        ctx.fail_reason = "responsive_pass phase never called submit_responsive()"
        return CopyWebsiteState.FAILED

    async def _parity_verify(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Loop until a verdict tool fires; return FINAL_REPORT or IMPLEMENT_LAYOUT."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Project: {ctx.project_path}\nOriginal: {ctx.target_url}\nPages: {', '.join(ctx.pages_built)}"
                    if attempt == 1
                    else "Call approve_parity(summary) or reject_parity(reason) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=PHASE_PROMPTS["parity_verify"],
                mode="Yolo",
                max_turns=35,
                shared_memory=memory,
                tools=_make_parity_tools(event, data),
            )
            if event.is_set():
                action = str(data.get("action", ""))
                if action == "approve":
                    ctx.parity_summary = str(data.get("summary", ""))
                    ctx.artifacts["parity_summary"] = ctx.parity_summary
                    return CopyWebsiteState.FINAL_REPORT
                else:
                    ctx.fix_iterations += 1
                    ctx.fix_reason = str(data.get("reason", "Parity rejected"))
                    ctx.artifacts["fix_reason"] = ctx.fix_reason
                    log.warning(
                        "Parity rejected (iteration %d): %s",
                        ctx.fix_iterations,
                        ctx.fix_reason,
                    )
                    if ctx.fix_iterations >= _MAX_FIX_ITERATIONS:
                        ctx.fail_reason = f"parity rejected {ctx.fix_iterations} times: {ctx.fix_reason}"
                        return CopyWebsiteState.FAILED
                    return CopyWebsiteState.IMPLEMENT_LAYOUT
        ctx.fail_reason = "parity_verify phase never reported a verdict"
        return CopyWebsiteState.FAILED

    async def _final_report(self, ctx: CopyContext, memory: object) -> CopyWebsiteState:
        """Single turn; always returns COMPLETE."""
        await self.run_phase(
            intent=ctx.intent,
            text=(
                f"Target: {ctx.target_url}\nProject: {ctx.project_path}\n"
                f"Pages: {', '.join(ctx.pages_built)}\n"
                f"Parity: {ctx.parity_summary}"
            ),
            stable_system_prompt=CACHE_CONTRACT,
            system_prompt=PHASE_PROMPTS["final_report"],
            max_turns=10,
            shared_memory=memory,
        )
        return CopyWebsiteState.COMPLETE



@dataclasses.dataclass
class CopyWebsiteParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.copy_website]."""

    study_model: str = ""
    build_model: str = ""
    verify_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "extract_target": self.study_model,
            "site_study": self.study_model,
            "design_spec": self.study_model,
            "scaffold": self.build_model,
            "implement_layout": self.build_model,
            "implement_pages": self.build_model,
            "implement_data": self.build_model,
            "responsive_pass": self.build_model,
            "parity_verify": self.verify_model,
            "final_report": self.build_model,
        }


class CopyWebsiteWorkflow(WorkflowPlugin):
    """Study a website with Playwright and rebuild it with Next.js + Tailwind + shadcn/ui + TanStack Query."""

    name = "copy_website"
    description = (
        "Study a website with Playwright, then rebuild it with Next.js, Tailwind CSS, "
        "shadcn/ui, and TanStack Query — identical in taste and style, mobile-friendly."
    )
    mode_bindings = []  # manual only — invoke with /workflow copy_website <url>

    phases = [
        PhaseSpec(
            name="extract_target",
            agent_type="planner",
            max_turns=10,
            next="site_study",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["extract_target"],
        ),
        PhaseSpec(
            name="site_study",
            agent_type="explorer",
            max_turns=35,
            next="design_spec",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["site_study"],
        ),
        PhaseSpec(
            name="design_spec",
            agent_type="planner",
            max_turns=25,
            next="scaffold",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["design_spec"],
        ),
        PhaseSpec(
            name="scaffold",
            agent_type="executor",
            max_turns=25,
            next="implement_layout",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["scaffold"],
        ),
        PhaseSpec(
            name="implement_layout",
            agent_type="executor",
            max_turns=30,
            next="implement_pages",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["implement_layout"],
        ),
        PhaseSpec(
            name="implement_pages",
            agent_type="executor",
            max_turns=40,
            next="implement_data",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["implement_pages"],
        ),
        PhaseSpec(
            name="implement_data",
            agent_type="executor",
            max_turns=30,
            next="responsive_pass",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["implement_data"],
        ),
        PhaseSpec(
            name="responsive_pass",
            agent_type="executor",
            max_turns=30,
            next="parity_verify",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["responsive_pass"],
        ),
        PhaseSpec(
            name="parity_verify",
            agent_type="verifier",
            max_turns=35,
            next="final_report",
            on_reject="implement_layout",
            max_iterations=_MAX_FIX_ITERATIONS,
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["parity_verify"],
        ),
        PhaseSpec(
            name="final_report",
            agent_type="executor",
            max_turns=10,
            output_schema="free_text",
            mode_override="Yolo",
            system_prompt_override=PHASE_PROMPTS["final_report"],
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, CopyContext):
            raise TypeError("copy_website checkpoint requires CopyContext")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "target_url": context.target_url,
            "target_pages": context.target_pages,
            "study_report": context.study_report,
            "screenshots": context.screenshots,
            "design_spec": context.design_spec,
            "design_tokens": context.design_tokens,
            "component_map": context.component_map,
            "data_plan": context.data_plan,
            "project_path": context.project_path,
            "layout_files": context.layout_files,
            "pages_built": context.pages_built,
            "data_hooks": context.data_hooks,
            "responsive_verified": context.responsive_verified,
            "parity_summary": context.parity_summary,
            "fix_reason": context.fix_reason,
            "fix_iterations": context.fix_iterations,
            "fail_reason": context.fail_reason,
            "artifacts": context.artifacts,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> CopyContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", CopyWebsiteState.EXTRACT_TARGET.name))
        try:
            state = CopyWebsiteState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown copy_website state: {raw_state}") from exc
        raw_artifacts = payload.get("artifacts", {})
        artifacts = (
            {str(key): str(value) for key, value in raw_artifacts.items()}
            if isinstance(raw_artifacts, dict)
            else {}
        )
        raw_pages = payload.get("target_pages", [])
        target_pages = (
            [str(item) for item in raw_pages] if isinstance(raw_pages, list) else []
        )
        raw_shots = payload.get("screenshots", [])
        screenshots = (
            [str(item) for item in raw_shots] if isinstance(raw_shots, list) else []
        )
        raw_built = payload.get("pages_built", [])
        pages_built = (
            [str(item) for item in raw_built] if isinstance(raw_built, list) else []
        )
        return CopyContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=int(payload.get("phase_iteration", 0)),
            target_url=str(payload.get("target_url", "")),
            target_pages=target_pages,
            study_report=str(payload.get("study_report", "")),
            screenshots=screenshots,
            design_spec=str(payload.get("design_spec", "")),
            design_tokens=str(payload.get("design_tokens", "")),
            component_map=str(payload.get("component_map", "")),
            data_plan=str(payload.get("data_plan", "")),
            project_path=str(payload.get("project_path", "")),
            layout_files=str(payload.get("layout_files", "")),
            pages_built=pages_built,
            data_hooks=str(payload.get("data_hooks", "")),
            responsive_verified=str(payload.get("responsive_verified", "")),
            parity_summary=str(payload.get("parity_summary", "")),
            fix_reason=str(payload.get("fix_reason", "")),
            fix_iterations=int(payload.get("fix_iterations", 0)),
            fail_reason=str(payload.get("fail_reason", "")),
            artifacts=artifacts,
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CopyWebsiteRunner:
        """Return this workflow's own state-machine runner."""
        return CopyWebsiteRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.copy_website]."""
        return CopyWebsiteParams(
            study_model=str(source.get("study_model", "") or ""),
            build_model=str(source.get("build_model", "") or ""),
            verify_model=str(source.get("verify_model", "") or ""),
        )
