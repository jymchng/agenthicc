"""Tool-gated phase handoffs for ``create_workflow``.

Every tool in this module closes over one phase's event and structured data.
The runner observes the event after the agent turn returns; assistant prose
alone can never move the state machine forward.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from lauren_ai._tools import tool
from agenthicc.tools.base import ToolLike

_WORKFLOW_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _failure(error: str, fix: str, **extra: object) -> dict[str, object]:
    """Return one consistent, model-actionable rejected handoff."""

    return {
        "ok": False,
        "error": error,
        "fix": fix,
        "message": f"{error} Fix: {fix}",
        **extra,
    }


def _remember_error(data: dict[str, object], message: str) -> dict[str, object]:
    data["last_error"] = message
    return _failure(
        message, "Correct the missing or invalid fields and call the handoff tool again."
    )


def make_interpret_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[ToolLike]:
    """Build the interpretation handoff tool for one phase attempt."""

    @tool()
    async def complete_interpret_phase(summary: str, workflow_name: str) -> dict[str, object]:
        """Complete interpretation with a normalized intent and stable workflow name.

        Args:
            summary: The normalized purpose, inputs, outputs, integrations, and success criteria.
            workflow_name: Lowercase Python filename stem for the generated workflow.
        """

        if not isinstance(summary, str) or not summary.strip():
            return _remember_error(data, "The interpretation summary must be non-empty.")
        if not isinstance(workflow_name, str) or not _WORKFLOW_NAME.fullmatch(workflow_name):
            message = "workflow_name must match [a-z][a-z0-9_]{1,63} and be suitable as a Python filename stem."
            data["last_error"] = message
            return _failure(
                message,
                "Choose a stable lowercase workflow name and call complete_interpret_phase again.",
            )
        data.update({"summary": summary.strip(), "workflow_name": workflow_name})
        data.pop("last_error", None)
        event.set()
        return {
            "ok": True,
            "message": "Interpretation accepted. Stop this phase; the runner will start design.",
        }

    return [complete_interpret_phase]


def make_design_tools(event: asyncio.Event, data: dict[str, object]) -> list[ToolLike]:
    """Build the design handoff tool for one phase attempt."""

    @tool()
    async def complete_design_phase(design: str) -> dict[str, object]:
        """Complete the implementation design without writing source files.

        Args:
            design: Complete workflow design, including phases, prompts, tools, and runner contracts.
        """

        if not isinstance(design, str) or not design.strip():
            return _remember_error(data, "The workflow design must be non-empty.")
        data.update({"design": design.strip()})
        data.pop("last_error", None)
        event.set()
        return {
            "ok": True,
            "message": "Design accepted. Stop this phase; the runner will start execute.",
        }

    return [complete_design_phase]


def make_execute_tools(
    event: asyncio.Event,
    data: dict[str, object],
    *,
    expected_root: Path,
    expected_name: str,
) -> list[ToolLike]:
    """Build the execute handoff tool for one phase attempt.

    The agent owns the write through the canonical ``write_file`` tool.  This
    handoff performs only an exact-path existence check; it never writes,
    parses, validates, stages, or publishes source.
    """

    @tool()
    async def complete_execute_phase(
        summary: str,
        artifact_name: str,
        artifact_description: str,
    ) -> dict[str, object]:
        """Complete execution after writing the workflow source.

        Args:
            summary: Evidence that the complete workflow source was written.
            artifact_name: The stable lowercase workflow filename stem.
            artifact_description: Short description of the generated workflow.
        """

        if not isinstance(summary, str) or not summary.strip():
            return _remember_error(data, "The execution summary must be non-empty.")
        if not isinstance(artifact_name, str) or not _WORKFLOW_NAME.fullmatch(artifact_name):
            message = "artifact_name must be a valid lowercase workflow name."
            data["last_error"] = message
            return _failure(message, "Use the same stable name as the write_file path.")
        if artifact_name != expected_name:
            message = (
                f"artifact_name must be {expected_name!r}, matching the interpreted workflow name."
            )
            data["last_error"] = message
            return _failure(message, f"Write and declare .agenthicc/workflows/{expected_name}.py.")
        if not isinstance(artifact_description, str) or not artifact_description.strip():
            return _remember_error(data, "artifact_description must be non-empty.")

        expected_path = (expected_root / f"{artifact_name}.py").resolve()
        try:
            exists = expected_path.is_file() and expected_root.resolve() in expected_path.parents
        except OSError:
            exists = False
        if not exists:
            message = (
                f"The workflow file does not exist at the exact expected path: {expected_path}"
            )
            data["last_error"] = message
            return _failure(
                message,
                f"Call write_file with path '.agenthicc/workflows/{artifact_name}.py' and the complete source, then retry this handoff.",
            )

        data.update(
            {
                "summary": summary.strip(),
                "artifact_name": artifact_name,
                "artifact_description": artifact_description.strip(),
                "artifact_path": str(expected_path),
            }
        )
        data.pop("last_error", None)
        event.set()
        return {
            "ok": True,
            "path": str(expected_path),
            "message": "Execution accepted. Stop this phase; the runner will start summarize.",
        }

    return [complete_execute_phase]


def make_summarize_tools(event: asyncio.Event, data: dict[str, object]) -> list[ToolLike]:
    """Build the terminal summarize handoff tool for one phase attempt."""

    @tool()
    async def complete_summarize_phase(summary: str) -> dict[str, object]:
        """Complete the authoring run with a truthful user-facing summary.

        Args:
            summary: Concise result, artifact path, and exact next action for the user.
        """

        if not isinstance(summary, str) or not summary.strip():
            return _remember_error(data, "The final summary must be non-empty.")
        data.update({"summary": summary.strip()})
        data.pop("last_error", None)
        event.set()
        return {
            "ok": True,
            "message": "Summary accepted. The create_workflow run is complete.",
        }

    return [complete_summarize_phase]


__all__ = [
    "make_design_tools",
    "make_execute_tools",
    "make_interpret_tools",
    "make_summarize_tools",
]
