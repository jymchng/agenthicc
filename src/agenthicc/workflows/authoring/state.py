"""State and context types for the built-in ``create_workflow`` workflow.

The authoring workflow deliberately keeps its state separate from the generic
phase-graph context.  Its runner has a small, explicit state machine like
``code_plan`` so that a phase can only advance after its handoff tool has
signalled completion.
"""

from __future__ import annotations

import dataclasses
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory


class CreateWorkflowState(Enum):
    """States in the ``create_workflow`` authoring state machine."""

    INTERPRET = auto()
    DESIGN = auto()
    EXECUTE = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()
    FAILED = auto()

    @property
    def is_terminal(self) -> bool:
        """Whether this state ends the authoring run."""

        return self in {CreateWorkflowState.COMPLETE, CreateWorkflowState.FAILED}


@dataclasses.dataclass
class PhaseArtifact:
    """Evidence captured from one successful phase handoff.

    ``data`` contains only structured values supplied by the phase handoff
    tool.  The runner never copies or parses an assistant response into an
    artifact.
    """

    phase_name: str
    summary: str
    data: dict[str, object] = dataclasses.field(default_factory=dict)
    attempts: int = 0


@dataclasses.dataclass
class CreateWorkflowContext:
    """Mutable context carried by every phase of one authoring run."""

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
    shared_memory: "ShortTermMemory | None" = None

    def add_artifact(
        self,
        phase_name: str,
        summary: str,
        *,
        data: dict[str, object] | None = None,
        attempts: int = 0,
    ) -> PhaseArtifact:
        """Record the structured result of a completed phase."""

        artifact = PhaseArtifact(
            phase_name=phase_name,
            summary=summary,
            data=dict(data or {}),
            attempts=attempts,
        )
        self.phase_artifacts[phase_name] = artifact
        self.phase_attempts[phase_name] = attempts
        return artifact

    def as_system_block(self) -> str:
        """Render bounded prior phase evidence for a subsequent agent turn."""

        lines = [
            "[CREATE_WORKFLOW CONTEXT]",
            f"Original intent: {self.intent}",
            f"Workflow name: {self.workflow_name or '(not chosen yet)'}",
        ]
        if self.phase_artifacts:
            lines.append("Completed authoring phases:")
            for artifact in self.phase_artifacts.values():
                summary = artifact.summary[:2_000]
                if len(artifact.summary) > 2_000:
                    summary += "..."
                lines.append(f"- {artifact.phase_name}: {summary}")
        if self.artifact_path:
            lines.append(f"Agent-written artifact path: {self.artifact_path}")
        return "\n".join(lines)
