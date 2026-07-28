"""Read-only inspection tools for the create_workflow design phase.

These give the authoring agent *ground truth* about the workflow-plugin API —
the ``PhaseSpec`` field list is read live from the dataclass and the capability
and role lists are read live from their enums, so the guidance can never drift
from the real contract.  A canonical, known-valid example workflow is included
so the agent has a correct template to adapt rather than one it hallucinates.

All tools here are read-only: they take no session state and cause no side
effects, so they are safe to inject into any phase.
"""

from __future__ import annotations

from collections.abc import Callable

# Curated one-line purposes for PhaseSpec fields.  The field *names* come from
# the live dataclass (see describe_phasespec); this mapping only supplies human
# purpose text, and a field with no entry simply reports an empty purpose.
_PHASESPEC_PURPOSE: dict[str, str] = {
    "name": "Unique phase id within the workflow; referenced by next / on_reject.",
    "agent_type": (
        "Registry key selecting the default prompt and allowed capabilities: "
        "auto, planner, executor, reviewer, explorer, verifier, human, custom."
    ),
    "system_prompt_override": "Replaces the role's default system prompt entirely for this phase.",
    "mode_override": "RuntimeMode to activate for this phase, e.g. 'Auto' to unlock write tools.",
    "allowed_capabilities": "Optional capability allowlist for this phase (None = role default).",
    "allowed_capabilities_override": "Explicit per-phase capability override; wins over the field and role default.",
    "max_turns": "Maximum LLM sub-turns (tool-call → response cycles) within one phase run.",
    "output_schema": "How to parse the phase output: 'plan', 'review_result', or 'free_text'.",
    "next": "Phase to run next on success; None ends the workflow.",
    "on_reject": "Phase to jump to when this phase's output is approved=False (retry loops).",
    "on_error": "Phase to run when this phase raises (reserved).",
    "max_iterations": "Retry ceiling for re-entering this phase; -1 = unlimited.",
    "require_explicit_completion": "Loop until the phase's completion tool is called.",
    "require_plan_finalization": "Loop until finalize_plan() is called.",
    "require_explicit_review": "Require approve/reject tool calls instead of an XML review tag.",
    "parallel_with": "Sibling phase names to run concurrently via asyncio.gather.",
    "terminal_wait_policy": "Terminal default: 'foreground' or 'background'.",
    "command_lifecycle": "Command lifecycle: 'oneshot' or 'service'.",
    "require_successful_commands": "Gate the phase transition on successful command outcomes.",
    "require_readiness": "Require a successful service-readiness result before transition.",
}

# A canonical, known-valid example the agent can copy and adapt.  Kept minimal so
# it always parses cleanly through the workflow loader.
_EXAMPLE_WORKFLOW = '''\
"""doc_review — draft a document, then review it (example custom workflow)."""

from __future__ import annotations

from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin


class DocReviewWorkflow(WorkflowPlugin):
    name = "doc_review"
    description = "Draft a document, then review it."
    mode_bindings = []  # manual only — invoke with /workflow doc_review
    phases = [
        PhaseSpec(
            name="draft",
            agent_type="auto",
            max_turns=20,
            next="review",
            mode_override="Auto",  # unlock write tools for this phase
            system_prompt_override=(
                "You are in the DRAFT phase. Write the requested document to disk "
                "using the write tools, then briefly state what you wrote and where."
            ),
        ),
        PhaseSpec(
            name="review",
            agent_type="auto",
            max_turns=8,
            next=None,  # None ends the workflow
            output_schema="free_text",
            system_prompt_override=(
                "You are in the REVIEW phase. Read the drafted document and summarise "
                "whether it satisfies the request, noting any gaps."
            ),
        ),
    ]
'''


def make_inspection_tools() -> list[Callable[..., object]]:
    """Return the read-only authoring-surface inspection tools.

    ``[describe_phasespec, list_tool_capabilities, list_agent_roles, show_example_workflow]``
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def describe_phasespec() -> dict[str, object]:
        """Describe every PhaseSpec field: name, type, default, and purpose.

        Returns the authoritative field reference for the PhaseSpec dataclass,
        read live from the running code so it never drifts from the real API.
        Use it to decide which fields each phase of your new workflow needs.
        """
        import dataclasses  # noqa: PLC0415

        from agenthicc.workflows.plugin import PhaseSpec  # noqa: PLC0415

        fields: list[dict[str, str]] = []
        for spec_field in dataclasses.fields(PhaseSpec):
            if spec_field.default is not dataclasses.MISSING:
                default_repr = repr(spec_field.default)
            elif spec_field.default_factory is not dataclasses.MISSING:
                default_repr = repr(spec_field.default_factory())
            else:
                default_repr = "(required)"
            fields.append(
                {
                    "name": spec_field.name,
                    "type": str(spec_field.type),
                    "default": default_repr,
                    "purpose": _PHASESPEC_PURPOSE.get(spec_field.name, ""),
                }
            )
        return {"phasespec_fields": fields}

    @_tool()
    async def list_tool_capabilities() -> dict[str, object]:
        """List the tool capabilities a phase can allow or a mode can block.

        Returns each ToolCapability value with its description, read live from the
        enum.  Use these when reasoning about mode_override (which unlocks write /
        execute / network tools) and allowed_capabilities.
        """
        from agenthicc.tools.capabilities import ToolCapability  # noqa: PLC0415

        descriptions: dict[str, str] = {
            "read": "Reads files / data — no persistent side effects.",
            "write": "Creates, modifies, or deletes files / data.",
            "execute": "Runs shell commands or arbitrary code.",
            "git_read": "Reads git history, diffs, status, blame.",
            "git_write": "Modifies git state (add, commit, checkout, stash).",
            "network": "Makes outbound network calls.",
            "search": "Searches content without state changes.",
        }
        caps = [
            {"value": cap.value, "description": descriptions.get(cap.value, "")}
            for cap in ToolCapability
        ]
        return {"capabilities": caps}

    @_tool()
    async def list_agent_roles() -> dict[str, object]:
        """List the agent_type values a phase may use.

        Returns the PhaseRole constants read live from the code.  Most phases use
        'auto'; the others map to role-specific default prompts and capabilities.
        """
        from agenthicc.workflows.plugin import PhaseRole  # noqa: PLC0415

        roles = [
            value
            for name, value in vars(PhaseRole).items()
            if not name.startswith("_") and isinstance(value, str)
        ]
        return {"agent_types": roles}

    @_tool()
    async def show_example_workflow() -> dict[str, object]:
        """Return a complete, known-valid example workflow file to adapt.

        The example defines a two-phase workflow (draft → review) that parses
        cleanly through the workflow loader.  Copy its structure — module
        docstring, imports, WorkflowPlugin subclass, PhaseSpec list — and adapt
        the phases to the approved design.
        """
        return {"path": ".agenthicc/workflows/doc_review.py", "source": _EXAMPLE_WORKFLOW}

    return [
        describe_phasespec,
        list_tool_capabilities,
        list_agent_roles,
        show_example_workflow,
    ]
