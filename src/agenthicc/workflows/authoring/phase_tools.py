"""Tool-gated controls for ``create_workflow``, ``create_tools`` and ``create_commands``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


TransitionValidator = Callable[[dict[str, object]], tuple[str, str] | None]


_PHASE_TRANSITION_TOOL_NAMES = {
    "interpret": "complete_interpret_phase",
    "design": "complete_design_phase",
    "execute": "complete_execute_phase",
    "stage": "complete_stage_phase",
    "review": "request_publication_approval",
    "publish": "complete_publish_phase",
    "summarize": "complete_summarize_phase",
}


def _transition_failure(
    error: str,
    fix: str,
    **extra: object,
) -> dict[str, object]:
    """Return the common actionable shape for a rejected phase handoff."""

    return {
        "ok": False,
        "error": error,
        "fix": fix,
        "message": f"{error} Fix: {fix}",
        **extra,
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
    phase-specific completion tool is called.  For ``create_workflow``, the
    execute agent writes source with the canonical ``write_file`` tool before
    calling its transition; the design transition only hands off an
    implementation specification.
    """

    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    transition_tool_name = authoring_transition_tool_name(phase_name)

    @_tool(name=transition_tool_name)
    async def complete_phase(
        summary: str,
        artifact_name: str = "",
        artifact_description: str = "",
    ) -> dict[str, object]:
        """Complete the current authoring phase and request its configured transition.

        Call this only after the current phase objective is complete.  The
        workflow runner validates the result before entering the next phase.

        Args:
            summary: Concise evidence of the work completed in this phase.
            artifact_name: For the execute phase, the stable lowercase name
                used in the path the agent wrote. Ignored by other phases.
            artifact_description: For the execute phase, a concise description
                of the generated artifact. Ignored by other phases.
        """

        if not isinstance(summary, str) or not summary.strip():
            message = "The phase transition was rejected: summary must be a non-empty string."
            fix = f"Provide concise evidence of the completed work, then call {transition_tool_name}(summary) again."
            transition_data["last_error"] = message
            return _transition_failure(message, fix, retry=True)

        cleaned = summary.strip()
        transition_data["summary_candidate"] = cleaned
        if phase_name == "execute":
            if not isinstance(artifact_name, str) or not isinstance(artifact_description, str):
                message = (
                    f"The {phase_name} phase transition was rejected: artifact metadata "
                    "must be strings."
                )
                fix = (
                    "Provide artifact_name as the stable lowercase filename stem and "
                    "artifact_description as a concise non-empty description, then "
                    f"call {transition_tool_name}(summary, artifact_name, artifact_description) again."
                )
                transition_data["last_error"] = message
                transition_data.pop("summary_candidate", None)
                return _transition_failure(message, fix, retry=True)
            if artifact_name.strip():
                transition_data["artifact_name"] = artifact_name.strip()
            if artifact_description.strip():
                transition_data["artifact_description"] = artifact_description.strip()
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
                error_message = f"The {phase_name} phase cannot advance: {error} Fix: {fix}"
                transition_data["last_error"] = error_message
                transition_data.pop("summary_candidate", None)
                return _transition_failure(error_message, fix, message=error_message, retry=True)
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

    return [complete_phase]


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
            return _transition_failure(
                "Publication approval has already been decided for this review attempt.",
                "Stop calling the review transition tool and let the runner apply the recorded approval decision.",
                retry=False,
            )
        try:
            allowed = await request_approval()
        except Exception as exc:  # noqa: BLE001
            message = f"Publication approval could not be requested: {type(exc).__name__}: {exc}"
            transition_data["last_error"] = message
            return _transition_failure(
                message,
                "Resolve the approval-service error and request publication approval again.",
                retry=True,
            )

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
        return _transition_failure(
            "Publication approval was denied; the artifact remains staged.",
            "Do not publish the artifact. Stop the review phase so the runner can summarize the rejection.",
            rejected=True,
        )

    return [request_publication_approval]
