"""make_agenthicc_tool — Create an agenthicc-compatible tool plugin.

Turns a request like "create a tool that checks the configured Cloakbrowser
endpoint and returns a bounded status object" into a complete project tool
plugin at ``.agenthicc/tools/<name>.py``:

1. ``analyze`` — plans the tool: name, description, parameters, return shape,
   capability tags, dependencies, and confirmation requirements.
2. ``generate`` — writes the Python module: ``@tool``-decorated callable,
   ``TOOLS`` export, capability decorators, docstring schema, and bounded,
   structured returns.
3. ``validate`` — imports/checks the written file the way the loader will and
   loops back to ``generate`` until it loads cleanly and satisfies the tool
   plugin contract.
4. ``finalize`` — reports the result and how to reload/test the tool.

The tool plugin shape follows ``.agenthicc/tools/`` conventions: a module
under that directory, a callable decorated with ``@tool(...)`` (parentheses
required), a literal ``TOOLS`` export list, optional capability decorators
from ``agenthicc.tools.capabilities``, and an optional ``DEPENDENCIES`` list.
After the run, the user runs ``/tools reload`` (or restarts the session) to
load it.

The declarative ``phases`` list is a static 4-node skeleton; the custom
runner owns the validate → generate retry loop.
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


class MakeToolState(Enum):
    """Every state this workflow can be in."""

    ANALYZE = auto()
    GENERATE = auto()
    VALIDATE = auto()
    FINALIZE = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()  # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (MakeToolState.COMPLETE, MakeToolState.FAILED)


@dataclasses.dataclass
class ToolParam:
    """One parameter of the tool being created."""

    name: str = ""
    type_hint: str = "str"
    default: str = ""
    description: str = ""


@dataclasses.dataclass
class MakeToolContext:
    """Data carried across every phase of one run."""

    intent: str
    run_id: str = ""
    state: MakeToolState = MakeToolState.ANALYZE
    phase_iteration: int = 0
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)
    tool_name: str = ""
    tool_description: str = ""
    parameters: list[ToolParam] = dataclasses.field(default_factory=list)
    capabilities: list[str] = dataclasses.field(default_factory=list)
    dependencies: list[str] = dataclasses.field(default_factory=list)
    requires_confirmation: bool = False
    class_form: bool = False
    plan: str = ""
    tool_file_path: str = ""
    generation_summary: str = ""
    validation_report: str = ""
    final_summary: str = ""
    fail_reason: str = ""
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)


# ── phase tool factories ──────────────────────────────────────────────────────


def _make_analyze_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the analyze phase."""
    from lauren_ai._tools import tool

    @tool()
    async def submit_tool_plan(
        tool_name: str,
        description: str,
        parameters: list[dict[str, str]] | None = None,
        capabilities: list[str] | None = None,
        dependencies: list[str] | None = None,
        requires_confirmation: bool = False,
        class_form: bool = False,
    ) -> dict[str, object]:
        """Record the tool plan and advance to the generate phase.

        Args:
            tool_name: The tool's name (lowercase, snake_case, a valid Python
                identifier).
            description: A short description shown to the model (used in the
                tool schema).
            parameters: List of params, each {"name": str, "type_hint": str,
                "default": str, "description": str}.
            capabilities: Tool capability tags — any subset of "read", "write",
                "execute", "git_read", "git_write", "network", "search".
            dependencies: Optional list of pip requirements the tool needs
                (e.g. ["httpx>=0.27"]).
            requires_confirmation: True when every call needs an explicit user
                confirmation (side-effecting tools).
            class_form: True for a stateful no-arg class with a run() method
                instead of a plain async function.
        """
        if not tool_name.strip():
            return {
                "ok": False,
                "error": "tool_name must not be empty.",
                "fix": "Call submit_tool_plan() with a descriptive tool name.",
            }
        if not tool_name.isidentifier():
            return {
                "ok": False,
                "error": f"tool_name '{tool_name}' is not a valid Python identifier.",
                "fix": "Use lowercase snake_case, e.g. 'project_status'.",
            }
        if not description.strip():
            return {
                "ok": False,
                "error": "description must not be empty.",
                "fix": "Provide a one-line description for the tool schema.",
            }
        valid_caps: tuple[str, ...] = (
            "read", "write", "execute", "git_read", "git_write", "network", "search",
        )
        caps: list[str] = list(capabilities or [])
        bad_caps: list[str] = [c for c in caps if c not in valid_caps]
        if bad_caps:
            return {
                "ok": False,
                "error": f"Unsupported capabilities: {bad_caps}.",
                "fix": f"Use only: {', '.join(valid_caps)}.",
            }
        cleaned_params: list[dict[str, str]] = []
        for raw in parameters or []:
            if not isinstance(raw, dict):
                continue
            p_name = str(raw.get("name", "")).strip()
            if not p_name or not p_name.isidentifier():
                continue
            cleaned_params.append({
                "name": p_name,
                "type_hint": str(raw.get("type_hint", "str")).strip() or "str",
                "default": str(raw.get("default", "")).strip(),
                "description": str(raw.get("description", "")).strip(),
            })
        data["tool_name"] = tool_name.strip()
        data["description"] = description.strip()
        data["parameters"] = cleaned_params
        data["capabilities"] = caps
        data["dependencies"] = [str(d).strip() for d in (dependencies or []) if str(d).strip()]
        data["requires_confirmation"] = bool(requires_confirmation)
        data["class_form"] = bool(class_form)
        event.set()
        return {
            "ok": True,
            "message": (
                f"Tool plan recorded: '{tool_name}' with {len(cleaned_params)} params, "
                f"{len(caps)} capabilities. The generate phase starts next."
            ),
        }

    return [submit_tool_plan]


def _make_generate_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the generate phase."""
    from lauren_ai._tools import tool

    @tool()
    async def confirm_generation_complete(
        file_path: str,
        summary: str,
    ) -> dict[str, object]:
        """Signal that the tool module was written successfully.

        Args:
            file_path: The path of the written tool module (must be under
                .agenthicc/tools/).
            summary: A short description of what was written.
        """
        if not file_path.strip():
            return {
                "ok": False,
                "error": "file_path must not be empty.",
                "fix": "Provide the actual path of the tool module you wrote.",
            }
        if ".agenthicc/tools/" not in file_path.replace("\\", "/"):
            return {
                "ok": False,
                "error": "file_path must be under .agenthicc/tools/.",
                "fix": "Write the tool module to .agenthicc/tools/<tool_name>.py.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the module you wrote.",
            }
        data["file_path"] = file_path.strip()
        data["summary"] = summary.strip()
        event.set()
        return {
            "ok": True,
            "message": f"Generation confirmed at '{file_path}'. The validate phase starts next.",
        }

    return [confirm_generation_complete]


def _make_validate_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the pass/fail decision tools for the validate phase."""
    from lauren_ai._tools import tool

    @tool()
    async def approve_tool(summary: str) -> dict[str, object]:
        """Signal that the tool plugin passes validation.

        Args:
            summary: What was verified (imports, TOOLS export, schema, caps).
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe what you verified.",
            }
        data["action"] = "approve"
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Tool approved."}

    @tool()
    async def reject_tool(issue: str) -> dict[str, object]:
        """Signal that the tool needs fixes before it can be approved.

        Args:
            issue: The concrete problem found (syntax, missing TOOLS export,
                bad name, missing docstring, etc.).
        """
        if not issue.strip():
            return {
                "ok": False,
                "error": "issue must not be empty.",
                "fix": "Describe what needs to be fixed.",
            }
        data["action"] = "reject"
        data["issue"] = issue.strip()
        event.set()
        return {"ok": True, "message": f"Tool sent back for fixes: {issue}"}

    return [approve_tool, reject_tool]


# ── runner ────────────────────────────────────────────────────────────────────

# Static phase names for status-bar and event payloads.
_PHASE_NAMES: tuple[str, ...] = ("analyze", "generate", "validate", "finalize")

#: Zero-based status-bar position of each phase.
_PHASE_INDEX: dict[str, int] = {name: index for index, name in enumerate(_PHASE_NAMES)}


class MakeToolRunner(CodePlanRunner):
    """State-machine runner for make_agenthicc_tool.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none of
    code_plan's own phases execute — this runner owns the whole flow.
    """

    workflow_name = "make_agenthicc_tool"
    total_phases = 4

    async def run(self, intent: str) -> MakeToolContext:
        """Drive analyze → generate → validate → finalize."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(
                max_tokens=self._cfg.cfg.execution.effective_usable_budget()
            )
        )
        ctx = MakeToolContext(
            intent=intent,
            run_id=run_id,
            state=MakeToolState.ANALYZE,
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
                    state.name.lower(),
                    self._phase_index(state),
                    ctx.phase_iteration,
                )
            match state:
                case MakeToolState.ANALYZE:
                    state = await self._analyze(ctx, memory)
                case MakeToolState.GENERATE:
                    state = await self._generate(ctx, memory)
                case MakeToolState.VALIDATE:
                    state = await self._validate(ctx, memory)
                case MakeToolState.FINALIZE:
                    state = await self._finalize(ctx, memory)
            log.info("make_agenthicc_tool → %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> MakeToolContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, MakeToolContext):
            raise TypeError("make_agenthicc_tool resume requires MakeToolContext")
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
                handle.update_phase(
                    state.name.lower(),
                    self._phase_index(state),
                    context.phase_iteration,
                )
            match state:
                case MakeToolState.ANALYZE:
                    state = await self._analyze(context, memory)
                case MakeToolState.GENERATE:
                    state = await self._generate(context, memory)
                case MakeToolState.VALIDATE:
                    state = await self._validate(context, memory)
                case MakeToolState.FINALIZE:
                    state = await self._finalize(context, memory)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: MakeToolState) -> int:
        return _PHASE_INDEX.get(state.name.lower(), 0)


    async def _analyze(
        self,
        ctx: MakeToolContext,
        memory: object,
    ) -> MakeToolState:
        """Loop until submit_tool_plan fires; return GENERATE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    ctx.intent
                    if attempt == 1
                    else (
                        "Call submit_tool_plan(tool_name, description, parameters, "
                        "capabilities, dependencies, requires_confirmation, class_form) now."
                    )
                ),
                system_prompt=(
                    "You are in the ANALYZE phase of make_agenthicc_tool. Plan an "
                    "agenthicc-compatible tool plugin from the user's intent.\n\n"
                    "Steps:\n"
                    "1. Understand what the tool must do: its inputs, outputs, and "
                    "side effects (does it read files, call a network endpoint, run "
                    "commands, mutate state?).\n"
                    "2. Choose a tool_name — lowercase snake_case, a valid Python "
                    "identifier (e.g. 'project_status', 'cloakbrowser_check').\n"
                    "3. Write a one-line description for the tool schema (what it "
                    "returns, what it does).\n"
                    "4. List the parameters: name, type_hint (str/int/float/bool/"
                    "list[str]/dict[str,object] etc.), default ('' = required), and a "
                    "short description. Keep it bounded — 0-5 params is typical.\n"
                    "5. Choose the capability tags the tool needs — any subset of "
                    "read, write, execute, git_read, git_write, network, search. "
                    "Match them to the tool's real behaviour (a tool that calls an "
                    "HTTP endpoint needs 'network'; one that reads files needs "
                    "'read').\n"
                    "6. Note any dependencies (pip requirements) the tool needs, e.g. "
                    "['httpx>=0.27'].\n"
                    "7. Decide requires_confirmation: True for side-effecting tools "
                    "(writes, deletes, outbound calls that mutate remote state).\n"
                    "8. Decide class_form: True only for a stateful tool that needs a "
                    "no-arg class with a run() method; otherwise False (async "
                    "function).\n\n"
                    "Call submit_tool_plan(tool_name, description, parameters, "
                    "capabilities, dependencies, requires_confirmation, class_form) "
                    "with your plan. Do NOT write any code yet.\n\n"
                    "Be precise about capabilities — they drive the approval/safety "
                    "gates in the session."
                ),
                max_turns=10,
                shared_memory=memory,
                tools=_make_analyze_tools(event, data),
            )
            if event.is_set():
                ctx.tool_name = str(data.get("tool_name", ""))
                ctx.tool_description = str(data.get("description", ""))
                ctx.capabilities = list(data.get("capabilities", []))
                ctx.dependencies = list(data.get("dependencies", []))
                ctx.requires_confirmation = bool(data.get("requires_confirmation", False))
                ctx.class_form = bool(data.get("class_form", False))
                ctx.parameters = [
                    ToolParam(
                        name=str(p.get("name", "")),
                        type_hint=str(p.get("type_hint", "str")),
                        default=str(p.get("default", "")),
                        description=str(p.get("description", "")),
                    )
                    for p in data.get("parameters", [])
                    if isinstance(p, dict)
                ]
                ctx.plan = (
                    f"Tool: {ctx.tool_name}\n"
                    f"Description: {ctx.tool_description}\n"
                    f"Parameters: {', '.join(p.name for p in ctx.parameters) or '(none)'}\n"
                    f"Capabilities: {', '.join(ctx.capabilities) or '(none)'}\n"
                    f"Dependencies: {', '.join(ctx.dependencies) or '(none)'}\n"
                    f"Requires confirmation: {ctx.requires_confirmation}\n"
                    f"Class form: {ctx.class_form}"
                )
                ctx.artifacts["plan"] = ctx.plan
                return MakeToolState.GENERATE

        ctx.fail_reason = "analyze phase never called submit_tool_plan()"
        return MakeToolState.FAILED

    async def _generate(
        self,
        ctx: MakeToolContext,
        memory: object,
    ) -> MakeToolState:
        """Loop until confirm_generation_complete fires; return VALIDATE or FAILED."""
        params_block = "\n".join(
            f"    {p.name} ({p.type_hint}){' = ' + p.default if p.default else ''} — {p.description}"
            for p in ctx.parameters
        ) or "    (no parameters)"
        caps_note = ", ".join(ctx.capabilities) if ctx.capabilities else "(none)"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Write the tool plugin for '{ctx.tool_name}' to "
                    f".agenthicc/tools/{ctx.tool_name}.py.\n\nPlan:\n{ctx.plan}"
                    if attempt == 1
                    else (
                        "Fix the issues from validation and rewrite the module, then "
                        "call confirm_generation_complete(file_path, summary)."
                    )
                ),
                system_prompt=(
                    "You are in the GENERATE phase of make_agenthicc_tool. Write a "
                    "complete, agenthicc-compatible tool plugin.\n\n"
                    f"Tool: {ctx.tool_name}\n"
                    f"Description: {ctx.tool_description}\n"
                    f"Parameters:\n{params_block}\n"
                    f"Capabilities: {caps_note}\n"
                    f"Dependencies: {', '.join(ctx.dependencies) or '(none)'}\n"
                    f"Requires confirmation: {ctx.requires_confirmation}\n"
                    f"Class form: {ctx.class_form}\n\n"
                    "THE TOOL PLUGIN CONTRACT (follow exactly):\n"
                    "1. Create the directory .agenthicc/tools/ if missing, and write "
                    f".agenthicc/tools/{ctx.tool_name}.py.\n"
                    "2. Decorator — MUST include the parentheses:\n"
                    "        from lauren_ai import tool\n"
                    "        @tool(name=\"<tool_name>\", description=\"<description>\")\n"
                    "   Use the planned tool_name and description verbatim.\n"
                    "3. Function form (default):\n"
                    "        async def <tool_name>(<params>) -> dict[str, object]:\n"
                    "   with a docstring that has an 'Args:' section describing each "
                    "parameter (this builds the schema shown to the model).\n"
                    "   Class form (stateful): a no-arg class with the @tool decorator "
                    "and 'async def run(self, <params>) -> dict[str, object]:'.\n"
                    "4. TOOLS export (required) — a literal list:\n"
                    "        TOOLS = [<tool_name>]\n"
                    "5. Capability decorators — from agenthicc.tools.capabilities, "
                    "applied ABOVE @tool, matching the planned capabilities:\n"
                    "        from agenthicc.tools.capabilities import tool_read_search\n"
                    "        @tool_read_search\n"
                    "        @tool(name=..., description=...)\n"
                    "   Individual tags: read, write, execute, git_read, git_write, "
                    "network, search. Common combos: tool_read_search, "
                    "tool_network_read, tool_network_write. An untagged tool has an "
                    "empty capability set (passes gates but declares nothing) — always "
                    "tag when the tool does something.\n"
                    "6. Confirmation for side effects: if requires_confirmation is "
                    "True, add @tool(..., requires_confirmation=True).\n"
                    "7. Dependencies: if the plan lists any, declare them at module "
                    "level:\n"
                    "        DEPENDENCIES = [\"<pkg>=<ver>\"]\n"
                    "   Do NOT auto-install; just declare. Prefer deps already in the "
                    "project environment when possible.\n"
                    "8. Bounded, structured returns — always return a dict:\n"
                    "        return {\"ok\": True, ...}\n"
                    "   and for recoverable failures:\n"
                    "        return {\"ok\": False, \"error\": \"...\", \"recoverable\": True}\n"
                    "   Bound inputs and outputs; never log credentials, tokens, or "
                    "unbounded remote responses.\n"
                    "9. NO import-time side effects: no network calls, file mutation, "
                    "or secret printing at module top level. All work happens inside "
                    "the entry point. Make repeated calls safe/idempotent (transport "
                    "retries can re-fire).\n"
                    "10. Optional: inject ToolContext by declaring a parameter "
                    "annotated with 'from lauren_ai import ToolContext' — it is hidden "
                    "from the schema. Only add it if the tool genuinely needs call "
                    "metadata.\n\n"
                    "Write the file with the write tool, then call "
                    "confirm_generation_complete(file_path, summary) with the exact "
                    "path you wrote and a one-line summary."
                ),
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_generate_tools(event, data),
            )
            if event.is_set():
                ctx.tool_file_path = str(data.get("file_path", ""))
                ctx.generation_summary = str(data.get("summary", ""))
                ctx.artifacts["generated"] = ctx.tool_file_path
                return MakeToolState.VALIDATE

        ctx.fail_reason = "generate phase never called confirm_generation_complete()"
        return MakeToolState.FAILED


    async def _validate(
        self,
        ctx: MakeToolContext,
        memory: object,
    ) -> MakeToolState:
        """Loop until a verdict tool fires; return FINALIZE or GENERATE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Validate the tool plugin at '{ctx.tool_file_path}'.\n\n"
                    f"Planned tool: {ctx.tool_name}\nPlan:\n{ctx.plan}"
                    if attempt == 1
                    else "Call approve_tool(summary) or reject_tool(issue) now."
                ),
                system_prompt=(
                    "You are in the VALIDATE phase of make_agenthicc_tool. Verify the "
                    "written tool plugin against the tool plugin contract before "
                    "approving.\n\n"
                    f"Expected file: {ctx.tool_file_path}\n"
                    f"Expected tool name: {ctx.tool_name}\n"
                    f"Expected capabilities: {', '.join(ctx.capabilities) or '(none)'}\n"
                    f"Expected dependencies: {', '.join(ctx.dependencies) or '(none)'}\n\n"
                    "Deterministic checks (run these first, before any opinion):\n"
                    "1. The file exists at the reported path under .agenthicc/tools/, "
                    "is a .py file, and its filename is not underscore-prefixed.\n"
                    "2. It parses as valid Python (python -m py_compile or ast).\n"
                    "3. It imports cleanly (python -c \"import importlib.util; ...\") — "
                    "if it fails on a missing dependency listed in DEPENDENCIES, that "
                    "is fixable (reject with the missing module named); if it fails "
                    "with a syntax/name error, that is also fixable.\n"
                    "4. It defines a TOOLS list exporting a callable whose declared "
                    "@tool name equals the planned tool_name.\n"
                    "5. @tool(...) is used WITH parentheses (bare @tool without parens "
                    "is the most common authoring bug).\n"
                    "6. The callable has a docstring with an 'Args:' section and "
                    "type-annotated parameters.\n"
                    "7. Capability decorators match the planned capabilities (or at "
                    "least are not weaker than the tool's real behaviour — a tool that "
                    "calls the network MUST be tagged network).\n"
                    "8. No import-time side effects: no top-level network calls, file "
                    "mutation, or secret printing.\n"
                    "9. Returns are structured dicts ({\"ok\": ...}) and inputs/outputs "
                    "are bounded; idempotency is addressed for side-effecting tools.\n"
                    "10. Dependencies are declared (DEPENDENCIES or sidecar "
                    "<name>.requirements.txt) when the tool imports third-party "
                    "packages.\n\n"
                    "Report the PASS/FAIL of each check. Then:\n"
                    "  - If everything passes, call approve_tool(summary) with a "
                    "summary of what was verified.\n"
                    "  - If anything fails, call reject_tool(issue) with the concrete "
                    "problem (name the file, the line, and the fix). The run will "
                    "loop back to generate for repairs.\n"
                    "You MUST call one of them."
                ),
                max_turns=12,
                shared_memory=memory,
                tools=_make_validate_tools(event, data),
            )
            if event.is_set():
                action: str = str(data.get("action", ""))
                if action == "approve":
                    ctx.validation_report = str(data.get("summary", ""))
                    ctx.artifacts["validated"] = ctx.validation_report
                    return MakeToolState.FINALIZE
                ctx.fail_reason = str(data.get("issue", ""))
                ctx.artifacts["validation_issue"] = ctx.fail_reason
                return MakeToolState.GENERATE

        ctx.fail_reason = "validate phase never reported a verdict"
        return MakeToolState.FAILED

    async def _finalize(
        self,
        ctx: MakeToolContext,
        memory: object,
    ) -> MakeToolState:
        """Single turn; always returns COMPLETE."""
        await self.run_phase(
            intent=ctx.intent,
            text=(
                f"Tool '{ctx.tool_name}' was created at {ctx.tool_file_path} and "
                "validated. Summarize the tool for the user."
            ),
            system_prompt=(
                "You are in the FINALIZE phase of make_agenthicc_tool. Report the "
                "finished tool.\n\n"
                f"Tool: {ctx.tool_name}\n"
                f"File: {ctx.tool_file_path}\n"
                f"Description: {ctx.tool_description}\n"
                f"Parameters: {', '.join(p.name for p in ctx.parameters) or '(none)'}\n"
                f"Capabilities: {', '.join(ctx.capabilities) or '(none)'}\n"
                f"Dependencies: {', '.join(ctx.dependencies) or '(none)'}\n"
                f"Requires confirmation: {ctx.requires_confirmation}\n"
                f"Validation: {ctx.validation_report or '(approved)'}\n\n"
                "Give the user:\n"
                "1. A one-paragraph description of what the tool does.\n"
                "2. How to load it: run '/tools reload' in the session (or restart), "
                "then '/tools' to confirm it appears.\n"
                "3. How to test it (a sample invocation), and a note that project "
                "tool plugins are executable Python — review the file before use.\n"
                "4. The exact path written.\n\n"
                "Keep it concise and actionable."
            ),
            max_turns=6,
            shared_memory=memory,
        )
        return MakeToolState.COMPLETE


# ── plugin ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class MakeToolParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.make_agenthicc_tool]."""

    analyze_model: str = ""
    generate_model: str = ""
    validate_model: str = ""
    finalize_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "analyze": self.analyze_model,
            "generate": self.generate_model,
            "validate": self.validate_model,
            "finalize": self.finalize_model,
        }


class MakeAgenthiccToolWorkflow(WorkflowPlugin):
    """Create an agenthicc-compatible tool plugin at .agenthicc/tools/."""

    name = "make_agenthicc_tool"
    description = (
        "Create an agenthicc-compatible tool plugin (a @tool-decorated callable "
        "with a TOOLS export under .agenthicc/tools/) from a natural-language "
        "request, validate it, and report how to load it."
    )
    mode_bindings = []  # manual only — invoke with /workflow make_agenthicc_tool

    # Static skeleton graph. The runner owns the validate → generate retry loop.
    phases = [
        PhaseSpec(
            name="analyze",
            agent_type="auto",
            max_turns=10,
            next="generate",
            on_reject="analyze",
            system_prompt_override=(
                "You are in the ANALYZE phase of make_agenthicc_tool. Plan the tool "
                "(name, description, parameters, capability tags, dependencies, "
                "confirmation), then call submit_tool_plan()."
            ),
        ),
        PhaseSpec(
            name="generate",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="validate",
            on_reject="generate",
            system_prompt_override=(
                "You are in the GENERATE phase of make_agenthicc_tool. Write the "
                "complete @tool-decorated plugin with a TOOLS export to "
                ".agenthicc/tools/, then call confirm_generation_complete()."
            ),
        ),
        PhaseSpec(
            name="validate",
            agent_type="auto",
            max_turns=12,
            next="finalize",
            on_reject="generate",  # loop back to fix
            system_prompt_override=(
                "You are in the VALIDATE phase of make_agenthicc_tool. Run the "
                "deterministic checks on the written plugin, then call "
                "approve_tool() or reject_tool()."
            ),
        ),
        PhaseSpec(
            name="finalize",
            agent_type="auto",
            max_turns=6,
            next=None,  # terminal on success
            system_prompt_override=(
                "You are in the FINALIZE phase of make_agenthicc_tool. Summarize the "
                "created tool and how to load and test it."
            ),
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, MakeToolContext):
            raise TypeError("make_agenthicc_tool checkpoint requires MakeToolContext")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "tool_name": context.tool_name,
            "tool_description": context.tool_description,
            "parameters": [
                {
                    "name": p.name,
                    "type_hint": p.type_hint,
                    "default": p.default,
                    "description": p.description,
                }
                for p in context.parameters
            ],
            "capabilities": context.capabilities,
            "dependencies": context.dependencies,
            "requires_confirmation": context.requires_confirmation,
            "class_form": context.class_form,
            "plan": context.plan,
            "tool_file_path": context.tool_file_path,
            "generation_summary": context.generation_summary,
            "validation_report": context.validation_report,
            "final_summary": context.final_summary,
            "fail_reason": context.fail_reason,
            "artifacts": context.artifacts,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> MakeToolContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", MakeToolState.ANALYZE.name))
        try:
            state = MakeToolState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown make_agenthicc_tool state: {raw_state}") from exc
        raw_params = payload.get("parameters", [])
        parameters: list[ToolParam] = []
        if isinstance(raw_params, list):
            for raw in raw_params:
                if not isinstance(raw, dict):
                    continue
                parameters.append(
                    ToolParam(
                        name=str(raw.get("name", "")),
                        type_hint=str(raw.get("type_hint", "str")),
                        default=str(raw.get("default", "")),
                        description=str(raw.get("description", "")),
                    )
                )
        raw_artifacts = payload.get("artifacts", {})
        artifacts = (
            {str(key): str(value) for key, value in raw_artifacts.items()}
            if isinstance(raw_artifacts, dict)
            else {}
        )
        return MakeToolContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=int(payload.get("phase_iteration", 0)),
            tool_name=str(payload.get("tool_name", "")),
            tool_description=str(payload.get("tool_description", "")),
            parameters=parameters,
            capabilities=[str(c) for c in payload.get("capabilities", [])]
            if isinstance(payload.get("capabilities", []), list)
            else [],
            dependencies=[str(d) for d in payload.get("dependencies", [])]
            if isinstance(payload.get("dependencies", []), list)
            else [],
            requires_confirmation=bool(payload.get("requires_confirmation", False)),
            class_form=bool(payload.get("class_form", False)),
            plan=str(payload.get("plan", "")),
            tool_file_path=str(payload.get("tool_file_path", "")),
            generation_summary=str(payload.get("generation_summary", "")),
            validation_report=str(payload.get("validation_report", "")),
            final_summary=str(payload.get("final_summary", "")),
            fail_reason=str(payload.get("fail_reason", "")),
            artifacts=artifacts,
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> MakeToolRunner:
        """Return this workflow's own state-machine runner."""
        return MakeToolRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.make_agenthicc_tool]."""
        return MakeToolParams(
            analyze_model=str(source.get("analyze_model", "") or ""),
            generate_model=str(source.get("generate_model", "") or ""),
            validate_model=str(source.get("validate_model", "") or ""),
            finalize_model=str(source.get("finalize_model", "") or ""),
        )
