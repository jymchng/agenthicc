"""Tool-gated controls for ``create_workflow``, ``create_tools`` and ``create_commands``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


def make_authoring_transition_tools(
    phase_name: str,
    transition_event: asyncio.Event,
    transition_data: dict[str, object],
) -> list[Callable[..., object]]:
    """Create the phase-local transition tools.

    The tools close over one phase invocation.  A model can inspect, reason,
    and take multiple turns, but the runner only advances when the phase's
    completion tool is called.  ``submit_generated_source`` additionally
    captures source without relying on a response envelope or delimiter.
    """

    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def complete_authoring_phase(summary: str) -> dict[str, object]:
        """Complete the current authoring phase and request its configured transition.

        Call this only after the current phase objective is complete.  The
        workflow runner validates the result before entering the next phase.

        Args:
            summary: Concise evidence of the work completed in this phase.
        """

        cleaned = summary.strip()
        if not cleaned:
            return {"ok": False, "error": "summary must not be empty"}
        transition_data["summary"] = cleaned
        transition_data["phase"] = phase_name
        transition_event.set()
        return {
            "ok": True,
            "message": (
                f"The {phase_name} phase is complete. Stop this phase now; "
                "the runner will apply the configured transition."
            ),
        }

    tools: list[Callable[..., object]] = [complete_authoring_phase]

    if phase_name == "design":

        @_tool()
        async def submit_generated_source(
            source: str,
            artifact_name: str,
            artifact_description: str,
        ) -> dict[str, object]:
            """Submit one complete raw Python artifact for staging.

            The source is captured exactly as provided.  Do not include
            Markdown fences, XML, JSON, commentary, or a patch.  The runner
            still parses and statically validates it before any side effect.

            Args:
                source: Complete raw Python source for the requested artifact.
                artifact_name: Stable project-local artifact name.
                artifact_description: Short user-facing artifact description.
            """

            if not source.strip():
                return {"ok": False, "error": "source must not be empty"}
            if not artifact_name.strip() or not artifact_description.strip():
                return {
                    "ok": False,
                    "error": "artifact_name and artifact_description must not be empty",
                }
            transition_data["source"] = source
            transition_data["artifact_name"] = artifact_name.strip()
            transition_data["artifact_description"] = artifact_description.strip()
            transition_data["source_submitted"] = True
            return {
                "ok": True,
                "message": (
                    "Complete source captured. Now call complete_authoring_phase(summary) "
                    "to hand the artifact to staging."
                ),
            }

        tools.append(submit_generated_source)

    return tools
