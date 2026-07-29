"""CreateWorkflowState — explicit state machine for the create_workflow workflow.

``create_workflow`` is a *meta-workflow*: it drives a single agent through the
phases of authoring a brand-new agenthicc :class:`~agenthicc.workflows.plugin.WorkflowPlugin`
and writing it to ``.agenthicc/workflows/<name>.py``.  It is modelled tightly on
``code_plan``:

* an **outer loop** (:meth:`~agenthicc.workflows.create_workflow.runner.CreateWorkflowRunner.run`)
  that evolves this state;
* an **inner loop** (each phase method) that runs agent turns until the phase's
  transition tool fires;
* **phase transitions only via tool calls** — never by parsing the agent's prose;
* a **context** (:class:`CreateWorkflowContext`) that captures the artefact each
  phase produced as a :class:`PhaseArtifact`.

State graph::

    DESIGN    → GENERATE   (design approved + finalize_design)
           ↺  → DESIGN     (design not finalized — retry)
           → EXITED     (exit_create_workflow — request needs no new workflow)
    GENERATE  → VALIDATE   (mark_generation_complete + a file path recorded)
           ↺  → GENERATE   (nothing marked complete — retry)
    VALIDATE  → SUMMARIZE  (approve_workflow AND deterministic validation passed)
           → GENERATE   (reject_workflow, or the agent approved a file that
                            failed deterministic validation — fix and retry)
    SUMMARIZE → COMPLETE
    EXITED    (terminal — agent exited without authoring)
    FAILED    (terminal — a phase exhausted retries or hit a permanent error)
"""

from __future__ import annotations

import dataclasses
import time
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory


class CreateWorkflowState(Enum):
    """Every possible state in the create_workflow workflow."""

    DESIGN = auto()
    GENERATE = auto()
    VALIDATE = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()  # terminal — success
    EXITED = auto()  # terminal — agent exited without authoring
    FAILED = auto()  # terminal — exhausted retries or permanent error

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (
            CreateWorkflowState.COMPLETE,
            CreateWorkflowState.EXITED,
            CreateWorkflowState.FAILED,
        )


@dataclasses.dataclass
class PhaseArtifact:
    """One concrete artefact produced by a phase of a create_workflow run.

    Stored in :attr:`CreateWorkflowContext.artifacts` keyed by ``phase`` so the
    outer loop and downstream phases can inspect exactly what each phase produced
    without re-deriving it from the conversation transcript.

    :param phase: The phase that produced the artefact (``"design"``, ``"generate"``, …).
    :param kind: A short machine label for the artefact (``"design"``, ``"workflow_file"``, …).
    :param content: The primary textual content of the artefact.
    :param metadata: Structured side-channel facts (paths, ok-flags, counts).
    :param created_at: Wall-clock time the artefact was recorded.
    """

    phase: str
    kind: str
    content: str
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)
    created_at: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class CreateWorkflowContext:
    """Data carried across every phase of one create_workflow run.

    The runner mutates a single instance in place as the outer loop advances; the
    :attr:`artifacts` map is the durable record of what each phase produced.
    """

    intent: str
    run_id: str
    design: str = ""  # approved design text, set after DESIGN
    workflow_name: str = ""  # lower_snake_case slug of the workflow being authored
    generated_path: str = ""  # path the workflow file was written to, set after GENERATE
    generation_summary: str = ""  # agent's summary of what it generated
    validation_report: str = ""  # deterministic loader/validator report
    validation_summary: str = ""  # agent's approval summary, set on VALIDATE approve
    rejection_reason: str = ""  # set when VALIDATE routes back to GENERATE
    suggestion: str = ""  # set on EXITED — what the agent suggests instead
    fail_reason: str = ""  # set on FAILED
    repair_cycles: int = 0  # times VALIDATE routed back to GENERATE (bounds the repair loop)
    artifacts: dict[str, PhaseArtifact] = dataclasses.field(default_factory=dict)
    command_outcomes: list[dict[str, object]] = dataclasses.field(default_factory=list)
    shared_memory: ShortTermMemory | None = None  # shared across all phases
    state: CreateWorkflowState = CreateWorkflowState.DESIGN
    phase_iteration: int = 0

    def add_artifact(self, artifact: PhaseArtifact) -> None:
        """Record *artifact*, keyed by its phase (latest write for a phase wins)."""
        self.artifacts[artifact.phase] = artifact
