"""Phase-local handoff tools for ``create_workflow``.

Each factory closes over an event and a data dictionary owned by one phase
attempt.  The runner only transitions after the event is set, so assistant
prose and ordinary tool calls cannot mutate the state machine.
"""

import asyncio
import re
from pathlib import Path
from lauren_ai._tools import tool

from agenthicc.tools.base import ToolLike

_WORKFLOW_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _failure(data: dict[str, object], error: str, fix: str, **extra: object) -> dict[str, object]:
    data["last_error"] = error
    return {
        "ok": False,
        "error": error,
        "fix": fix,
        "message": f"{error} Fix: {fix}",
        **extra,
    }


def _non_empty(value: object, label: str) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty string."
    return value.strip(), None


def make_interpret_tools(event: asyncio.Event, data: dict[str, object]) -> list[ToolLike]:
    """Return the interpretation handoff tool."""

    @tool()
    async def complete_interpret_phase(summary: str, workflow_name: str) -> dict[str, object]:
        """Hand off a normalized use case and valid Python workflow name.

        Args:
            summary: Normalized purpose, inputs, outputs, integrations, and success criteria.
            workflow_name: Lowercase Python filename stem for the generated workflow.
        """

        normalized, error = _non_empty(summary, "summary")
        if error is not None:
            return _failure(data, error, "Provide the normalized use case and retry the handoff.")
        if not isinstance(workflow_name, str) or _WORKFLOW_NAME.fullmatch(workflow_name) is None:
            error = "workflow_name must match [a-z][a-z0-9_]{0,63}."
            return _failure(
                data,
                error,
                "Choose a lowercase Python filename stem without hyphens or path separators.",
            )
        data.update({"summary": normalized, "workflow_name": workflow_name})
        data.pop("last_error", None)
        event.set()
        return {"ok": True, "message": "Interpretation accepted; continue with DESIGN."}

    return [complete_interpret_phase]


def make_design_tools(event: asyncio.Event, data: dict[str, object]) -> list[ToolLike]:
    """Return the design handoff tool."""

    @tool()
    async def complete_design_phase(design: str) -> dict[str, object]:
        """Hand off the complete implementation design without writing source.

        Args:
            design: Complete phase, prompt, tool, state, context, transition, and test design.
        """

        normalized, error = _non_empty(design, "design")
        if error is not None:
            return _failure(data, error, "Provide a complete design and retry the handoff.")
        data.update({"design": normalized})
        data.pop("last_error", None)
        event.set()
        return {"ok": True, "message": "Design accepted; continue with EXECUTE."}

    return [complete_design_phase]


def make_execute_tools(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    expected_root: Path,
    expected_name: str,
) -> list[ToolLike]:
    """Return the execute handoff tool with an exact-path write check.

    This tool never writes, copies, parses, imports, or validates generated
    Python.  The agent owns the file creation through the canonical
    ``write_file`` tool; this handoff only proves that the expected artifact is
    present before allowing the state machine to advance.
    """

    root = expected_root.resolve()

    @tool()
    async def complete_execute_phase(
        summary: str,
        artifact_name: str,
        artifact_description: str,
    ) -> dict[str, object]:
        """Hand off after writing the generated workflow source.

        Args:
            summary: Evidence describing what was written.
            artifact_name: The same lowercase name used in the write_file path.
            artifact_description: Short description of the generated workflow.
        """

        normalized_summary, error = _non_empty(summary, "summary")
        if error is not None:
            return _failure(data, error, "Describe the completed write and retry the handoff.")
        normalized_description, error = _non_empty(artifact_description, "artifact_description")
        if error is not None:
            return _failure(data, error, "Describe the generated workflow and retry the handoff.")
        if not isinstance(artifact_name, str) or _WORKFLOW_NAME.fullmatch(artifact_name) is None:
            return _failure(
                data,
                "artifact_name must match [a-z][a-z0-9_]{0,63}.",
                "Use the same lowercase workflow name chosen during INTERPRET.",
            )
        if artifact_name != expected_name:
            return _failure(
                data,
                f"artifact_name must be {expected_name!r}, matching the interpreted name.",
                f"Write and declare .agenthicc/workflows/{expected_name}.py.",
            )

        expected_path = (root / f"{artifact_name}.py").resolve()
        if root not in expected_path.parents or not expected_path.is_file():
            return _failure(
                data,
                f"The expected workflow file does not exist at {expected_path}.",
                f"Call write_file with path '.agenthicc/workflows/{expected_name}.py' and the complete source, then retry.",
            )

        data.update(
            {
                "summary": normalized_summary,
                "artifact_name": artifact_name,
                "artifact_description": normalized_description,
                "artifact_path": str(expected_path),
            }
        )
        data.pop("last_error", None)
        event.set()
        return {
            "ok": True,
            "path": str(expected_path),
            "message": "Execution accepted; continue with SUMMARIZE.",
        }

    return [complete_execute_phase]


def make_summarize_tools(event: asyncio.Event, data: dict[str, object]) -> list[ToolLike]:
    """Return the terminal summary handoff tool."""

    @tool()
    async def complete_summarize_phase(summary: str) -> dict[str, object]:
        """Complete the authoring run with a truthful user-facing summary.

        Args:
            summary: Result, artifact path, capabilities, and next action for the user.
        """

        normalized, error = _non_empty(summary, "summary")
        if error is not None:
            return _failure(
                data, error, "Provide a concise truthful summary and retry the handoff."
            )
        data.update({"summary": normalized})
        data.pop("last_error", None)
        event.set()
        return {"ok": True, "message": "Summary accepted; create_workflow is complete."}

    return [complete_summarize_phase]


__all__ = [
    "make_interpret_tools",
    "make_design_tools",
    "make_execute_tools",
    "make_summarize_tools",
]
