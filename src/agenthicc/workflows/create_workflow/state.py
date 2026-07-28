"""Typed state and context for ``create_workflow``."""

from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory


class CreateWorkflowState(Enum):
    """States in the workflow-authoring state machine.

    The four non-terminal states correspond to the four phase methods on
    :class:`CreateWorkflowRunner`.  A phase method returns the next state; it
    never mutates the state machine's routing directly.
    """

    INTERPRET = auto()
    DESIGN = auto()
    EXECUTE = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()
    FAILED = auto()

    @property
    def is_terminal(self) -> bool:
        """Return whether this state ends the run."""

        return self in {CreateWorkflowState.COMPLETE, CreateWorkflowState.FAILED}


@dataclasses.dataclass
class PhaseArtifact:
    """Structured evidence captured when one phase hands off successfully."""

    phase_name: str
    summary: str
    data: dict[str, object] = dataclasses.field(default_factory=dict)
    attempts: int = 0


@dataclasses.dataclass
class CreateWorkflowContext:
    """Mutable context shared by every phase in one authoring run.

    The context stores values supplied by handoff tools.  Assistant prose is
    deliberately not promoted to an artifact by the runner.
    """

    intent: str
    run_id: str
    state: CreateWorkflowState = CreateWorkflowState.INTERPRET
    workflow_name: str = ""
    interpreted_intent: str = ""
    design: str = ""
    artifact_path: str = ""
    artifact_description: str = ""
    execute_summary: str = ""
    final_summary: str = ""
    fail_reason: str = ""
    phase_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    phase_artifacts: dict[str, PhaseArtifact] = dataclasses.field(default_factory=dict)
    command_outcomes: list[dict[str, object]] = dataclasses.field(default_factory=list)
    shared_memory: "ShortTermMemory | None" = None

    def add_artifact(
        self,
        phase_name: str,
        summary: str,
        *,
        data: dict[str, object] | None = None,
        attempts: int = 0,
    ) -> PhaseArtifact:
        """Record one phase's structured handoff and return the record."""

        artifact = PhaseArtifact(
            phase_name=phase_name,
            summary=summary.strip(),
            data=dict(data or {}),
            attempts=attempts,
        )
        self.phase_artifacts[phase_name] = artifact
        self.phase_attempts[phase_name] = attempts
        return artifact

    def as_system_block(self) -> str:
        """Render bounded prior-phase evidence for a subsequent agent turn."""

        lines = [
            "[CREATE_WORKFLOW CONTEXT]",
            f"Original intent: {self.intent}",
            f"Workflow name: {self.workflow_name or '(not chosen yet)'}",
        ]
        if self.design:
            lines.append(f"Design available: {self.design[:4_000]}")
        if self.artifact_path:
            lines.append(f"Agent-written artifact path: {self.artifact_path}")
        if self.phase_artifacts:
            lines.append("Completed phases:")
            for artifact in self.phase_artifacts.values():
                summary = artifact.summary[:2_000]
                if len(artifact.summary) > 2_000:
                    summary += "..."
                lines.append(f"- {artifact.phase_name} (attempt {artifact.attempts}): {summary}")
                for key, value in artifact.data.items():
                    rendered = str(value)
                    if len(rendered) > 800:
                        rendered = rendered[:800] + "..."
                    lines.append(f"  {key}: {rendered}")
        return "\n".join(lines)


__all__ = ["CreateWorkflowState", "PhaseArtifact", "CreateWorkflowContext"]
