"""Tool-gated controls for ``create_workflow``, ``create_tools`` and ``create_commands``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


TransitionValidator = Callable[[dict[str, object]], tuple[str, str] | None]


_PHASE_TRANSITION_TOOL_NAMES = {
    "interpret": "complete_interpret_phase",
    "design": "complete_design_phase",
    "stage": "complete_stage_phase",
    "validate": "complete_validate_phase",
    "review": "request_publication_approval",
    "publish": "complete_publish_phase",
    "summarize": "complete_summarize_phase",
}


def authoring_transition_tool_name(phase_name: str) -> str:
    """Return the unique handoff tool name for an authoring phase."""

    return _PHASE_TRANSITION_TOOL_NAMES.get(phase_name, f"complete_{phase_name}_phase")


def make_authoring_transition_tools(
    phase_name: str,
    transition_event: asyncio.Event,
    transition_data: dict[str, object],
    validator: TransitionValidator | None = None,
) -> list[Callable[..., object]]:
    """Create the phase-local transition tools.

    The tools close over one phase invocation.  A model can inspect, reason,
    and take multiple turns, but the runner only advances when the phase's
    phase-specific completion tool is called.  ``submit_generated_source``
    additionally captures source without relying on a response envelope or
    delimiter.
    """

    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    transition_tool_name = authoring_transition_tool_name(phase_name)

    @_tool(name=transition_tool_name)
    async def complete_phase(summary: str) -> dict[str, object]:
        """Complete the current authoring phase and request its configured transition.

        Call this only after the current phase objective is complete.  The
        workflow runner validates the result before entering the next phase.

        Args:
            summary: Concise evidence of the work completed in this phase.
        """

        cleaned = summary.strip()
        if not cleaned:
            message = "The phase transition was rejected: summary must not be empty."
            transition_data["last_error"] = message
            return {
                "ok": False,
                "error": message,
                "fix": (
                    "Provide concise evidence of the work completed, then call "
                    f"{transition_tool_name}(summary) again."
                ),
            }
        transition_data["summary_candidate"] = cleaned
        if validator is not None:
            try:
                failure = validator(transition_data)
            except Exception:  # noqa: BLE001
                failure = (
                    f"The {phase_name} transition validator could not verify the handoff.",
                    "Inspect the current phase inputs and retry the handoff with valid data.",
                )
            if failure is not None:
                error, fix = failure
                message = f"The {phase_name} phase cannot advance: {error} Fix: {fix}"
                transition_data["last_error"] = message
                transition_data.pop("summary_candidate", None)
                return {"ok": False, "error": message, "fix": fix, "retry": True}
        transition_data["summary"] = cleaned
        transition_data.pop("summary_candidate", None)
        transition_data["phase"] = phase_name
        transition_event.set()
        return {
            "ok": True,
            "message": (
                f"The {phase_name} phase is complete. Stop this phase now; "
                f"the runner will apply the configured transition after {transition_tool_name}()."
            ),
        }

    tools: list[Callable[..., object]] = [complete_phase]

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
                transition_data["last_error"] = "source must not be empty"
                return {
                    "ok": False,
                    "error": "source must not be empty",
                    "fix": "Generate the complete raw Python source and submit it again.",
                }
            if not artifact_name.strip() or not artifact_description.strip():
                transition_data["last_error"] = (
                    "artifact_name and artifact_description must not be empty"
                )
                return {
                    "ok": False,
                    "error": "artifact_name and artifact_description must not be empty",
                    "fix": "Provide both a stable artifact name and a concise description.",
                }
            transition_data["source"] = source
            transition_data["artifact_name"] = artifact_name.strip()
            transition_data["artifact_description"] = artifact_description.strip()
            transition_data["source_submitted"] = True
            return {
                "ok": True,
                "message": (
                    "Complete source captured. Now call "
                    f"{transition_tool_name}(summary) to hand the artifact to staging."
                ),
            }

        tools.append(submit_generated_source)

    return tools


def make_authoring_review_tools(
    request_approval: Callable[[], Awaitable[bool]],
    transition_event: asyncio.Event,
    transition_data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the publication-review transition tool for the review phase.

    Approval is deliberately performed inside the phase-local tool, matching
    ``code_plan`` where the agent invokes the tool that owns the phase decision.
    A denial is recorded as a rejected transition and returned to the agent as
    actionable tool output before the runner routes to ``summarize``.
    """

    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool(name=authoring_transition_tool_name("review"))
    async def request_publication_approval() -> dict[str, object]:
        """Ask for publication approval and signal the review transition."""

        if transition_data.get("approval_decided") is True:
            return {
                "ok": False,
                "error": "Publication approval has already been decided for this review attempt.",
                "fix": "Stop the review phase and let the runner apply the decision.",
            }
        try:
            allowed = await request_approval()
        except Exception as exc:  # noqa: BLE001
            message = f"Publication approval could not be requested: {type(exc).__name__}: {exc}"
            transition_data["last_error"] = message
            return {
                "ok": False,
                "error": message,
                "fix": "Resolve the approval-service error and request review again.",
                "retry": True,
            }

        transition_data["approval_decided"] = True
        transition_data["approved"] = allowed
        transition_event.set()
        if allowed:
            return {
                "ok": True,
                "message": (
                    "Publication approved. Stop the review phase; the runner will advance "
                    "to the publish phase."
                ),
            }
        return {
            "ok": False,
            "error": "Publication approval was denied; the artifact remains staged.",
            "fix": "Do not publish the artifact. Stop the review phase so the runner can summarize the rejection.",
            "rejected": True,
        }

    return [request_publication_approval]
