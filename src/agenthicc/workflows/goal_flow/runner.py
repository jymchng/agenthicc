"""goal_flow — clarify intent into goals, implement and verify each goal, then summarize.

The runner below is a state machine: the agent first clarifies the intent by
asking focused questions, then decides an ordered list of Goals. Each goal
becomes its own implement -> verify cycle; verification loops back to
implementation (unbounded retries) until the goal is satisfied, and only then
does the cursor move to the next goal. When every goal is satisfied the agent
writes a concise summary of what was done and which files were affected.

Transitions happen only because a phase tool was called — the state methods
check an ``asyncio.Event`` after the turn and never parse the agent's prose.
"""

# GoalFlowRunner deliberately provides a more specific state-machine API than
# CodePlanRunner's extension hooks.  The return types and phase signatures are
# intentionally different even though the session/turn wiring is inherited.
# Keep that architectural override explicit for mypy without scattering
# per-method suppression comments through the implementation.
# mypy: disable-error-code=override

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from enum import Enum, auto
from typing import TYPE_CHECKING, Awaitable, Literal, cast

from agenthicc.tools.base import ToolLike
from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

# Cache-contract boundary: the inherited ``run_phase`` calls
# ``build_workflow_prompt_contract`` before every phase turn. This runner keeps
# all phase state/artifacts in the dynamic region and supplies only its stable
# contract constant to that boundary.

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

#: Bounded tool-wait ceiling per phase invocation — never loop forever waiting
#: for a transition tool call. Goal verification itself retries without a
#: ceiling across phase invocations, but each invocation is still bounded.
_MAX_ATTEMPTS = 5

# Goal text and list limits protect checkpoint size and provider prompt
# construction.  They are configurable through GoalFlowParams; these defaults
# are deliberately generous and reject rather than truncate input.
_DEFAULT_MAX_GOAL_TEXT_CHARS = 4_096
_DEFAULT_MAX_GOALS = 1_000
_MAX_CONFIGURED_GOALS = 10_000
_MAX_CONFIGURED_GOAL_TEXT_CHARS = 65_536
_MAX_RECORD_TEXT_CHARS = 16_384
_MAX_RECORD_FILES = 256
_MAX_RECORD_FILE_PATH_CHARS = 1_024
_MAX_MUTATION_RECEIPTS = 128


def _positive_int(value: object, default: int) -> int:
    """Parse a positive configuration integer without raising during startup."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default


def _clean_files(value: object) -> tuple[list[str] | None, str | None]:
    """Validate bounded file metadata shared by transition tools."""
    if not isinstance(value, list):
        return None, "files must be a JSON array of paths"
    if len(value) > _MAX_RECORD_FILES:
        return None, f"files must contain at most {_MAX_RECORD_FILES} paths"
    cleaned: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            return None, f"files[{index}] must be a string path"
        path = item.strip()
        if not path:
            continue
        if len(path) > _MAX_RECORD_FILE_PATH_CHARS:
            return None, (f"files[{index}] exceeds {_MAX_RECORD_FILE_PATH_CHARS} characters")
        cleaned.append(path)
    return cleaned, None


# Stable workflow policy.  Keep phase-specific instructions and current
# artifacts (clarification notes, goals, verification evidence) in the dynamic
# ``system_prompt`` / ``text`` arguments to ``run_phase``.
CACHE_CONTRACT = """\
[WORKFLOW EXECUTION CONTRACT]
Keep the original user goal and the complete prior conversation in mind.
Phase state, artifacts, questions, answers, and transition details are dynamic
context; do not treat them as permanent workflow policy.

[REQUIREMENTS CLARIFICATION POLICY]
Ask the user a focused clarifying question through the existing `ask_user` tool
whenever required information is missing, ambiguous, or would materially change
the result. Wait for the answer and do not guess over a material ambiguity.
The question policy is stable; each actual question and answer remains dynamic.

[CACHE SAFETY POLICY]
Keep stable instructions and stable tool schemas deterministic. Do not insert
messages near the beginning of conversation history, rewrite old messages, or
put a rolling summary into the stable system prompt. Prompt caching never
replaces capability filtering, approval, or tool authorization.

[DYNAMIC GOAL LIST POLICY]
If implementation or verification discovers necessary work missing from the
current goal list, record it with append_goal(goal) or insert_goal(index, goal).
These are durable non-transition operations: they do not finish the current
goal or change phase. Continue the active goal and use its normal transition
tool after the current work is complete.

[WORKSPACE POLICY]
Use the parent session's `WorkflowConfig.workspace_scope` and
`WorkflowConfig.workspace_access` unchanged for every filesystem, mention, Git,
and command-working-directory access. Never construct a second workspace scope,
allow-list, or unrestricted sandbox inside this workflow, and never use raw
filesystem I/O to bypass the workspace policy.
""".strip()


class GoalState(Enum):
    """Every state this workflow can be in."""

    CLARIFY = auto()
    DECIDE_GOALS = auto()
    IMPLEMENT_GOAL = auto()
    VERIFY_GOAL = auto()
    SUMMARIZE = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()  # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (GoalState.COMPLETE, GoalState.FAILED)


class GoalStatus(str, Enum):
    """Durable lifecycle state for one goal-list entry."""

    PENDING = "pending"
    ACTIVE = "active"
    VERIFIED = "verified"


@dataclasses.dataclass
class GoalRecord:
    """One stable-identity goal and its implementation/verification evidence.

    The numeric position of a goal is intentionally not part of its identity:
    append/insert operations can move a record while its evidence must remain
    attached to the same work item.
    """

    goal_id: str
    text: str
    status: GoalStatus = GoalStatus.PENDING
    attempts: int = 0
    implementation_summary: str = ""
    verification_evidence: str = ""
    files: list[str] = dataclasses.field(default_factory=list)
    created_revision: int = 0
    created_phase: str = "decide_goals"

    def __post_init__(self) -> None:
        """Normalize values restored from JSON or compatibility callers."""
        if not isinstance(self.status, GoalStatus):
            self.status = GoalStatus(str(self.status))
        self.attempts = max(0, self.attempts) if isinstance(self.attempts, int) else 0
        self.files = [str(path) for path in self.files if isinstance(path, str)]

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-safe checkpoint representation."""
        return {
            "goal_id": self.goal_id,
            "text": self.text,
            "status": self.status.value,
            "attempts": self.attempts,
            "implementation_summary": self.implementation_summary,
            "verification_evidence": self.verification_evidence,
            "files": list(self.files),
            "created_revision": self.created_revision,
            "created_phase": self.created_phase,
        }


@dataclasses.dataclass(frozen=True)
class GoalMutationReceipt:
    """Bounded audit record for one committed append/insert operation."""

    revision: int
    operation: Literal["append", "insert"]
    goal_id: str
    index: int
    phase: str
    active_goal_id: str | None

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-safe receipt payload."""
        return {
            "revision": self.revision,
            "operation": self.operation,
            "goal_id": self.goal_id,
            "index": self.index,
            "phase": self.phase,
            "active_goal_id": self.active_goal_id,
        }


def _legacy_goal_id(index: int, text: str) -> str:
    """Derive a stable ID when decoding a pre-dynamic-goal checkpoint."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"legacy-{index}-{digest}"


@dataclasses.dataclass
class GoalContext:
    """Data carried across every phase of one run."""

    intent: str
    run_id: str = ""
    state: GoalState = GoalState.CLARIFY
    phase_iteration: int = 0
    clarification_notes: str = ""
    goals: list[str] = dataclasses.field(default_factory=list)
    goal_index: int = 0
    # Compatibility projections derived from ``goal_records``. New runtime
    # code must use record fields so insertion cannot shift ownership.
    goal_attempts: list[int] = dataclasses.field(default_factory=list)
    goal_evidence: list[str] = dataclasses.field(default_factory=list)
    goal_files: list[list[str]] = dataclasses.field(default_factory=list)
    # Goal completion is a stronger durability boundary than ordinary phase
    # entry: implementation and verification have both succeeded, so the
    # next resume must never return to that goal.  These lists are persisted in
    # the custom checkpoint payload as an auditable compatibility projection.
    completed_goal_indices: list[int] = dataclasses.field(default_factory=list)
    goal_checkpoint_revisions: list[int] = dataclasses.field(default_factory=list)
    summary: str = ""
    files_affected: list[str] = dataclasses.field(default_factory=list)
    fail_reason: str = ""
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: "ShortTermMemory | None" = dataclasses.field(default=None, repr=False)
    # Stable records are canonical for new runtime operations.  The fields
    # above remain as compatibility projections for existing integrations and
    # old checkpoints.
    goal_records: list[GoalRecord] = dataclasses.field(default_factory=list)
    active_goal_id: str = ""
    goal_list_revision: int = 0
    goal_mutation_receipts: list[dict[str, object]] = dataclasses.field(default_factory=list)

    def __post_init__(self) -> None:
        """Build stable records for legacy constructors and synchronize views."""
        if self.goal_records:
            self.goal_records = [
                dataclasses.replace(record, files=list(record.files))
                for record in self.goal_records
            ]
            self._sync_legacy_views()
            return
        if not self.goals:
            return

        completed = set(self.completed_goal_indices)
        records: list[GoalRecord] = []
        for index, text in enumerate(self.goals):
            records.append(
                GoalRecord(
                    goal_id=_legacy_goal_id(index, text),
                    text=text,
                    status=(GoalStatus.VERIFIED if index in completed else GoalStatus.PENDING),
                    attempts=(self.goal_attempts[index] if index < len(self.goal_attempts) else 0),
                    implementation_summary=(
                        self.goal_evidence[index] if index < len(self.goal_evidence) else ""
                    ),
                    verification_evidence=(
                        self.goal_evidence[index]
                        if index < len(self.goal_evidence) and index in completed
                        else ""
                    ),
                    files=(list(self.goal_files[index]) if index < len(self.goal_files) else []),
                    created_phase="legacy",
                )
            )
        self.goal_records = records
        if not self.active_goal_id and records:
            active_index = min(max(self.goal_index, 0), len(records) - 1)
            if records[active_index].status is not GoalStatus.VERIFIED:
                self.active_goal_id = records[active_index].goal_id
        self._sync_legacy_views()

    def initialize_goals(self, goals: list[str]) -> None:
        """Replace the initial decision with fresh, uniquely identified goals."""
        self.goal_records = [GoalRecord(goal_id=uuid.uuid4().hex, text=text) for text in goals]
        self.active_goal_id = self.goal_records[0].goal_id if self.goal_records else ""
        self.goal_list_revision = 0
        self.goal_mutation_receipts = []
        self.goal_index = 0
        self.completed_goal_indices = []
        self.goal_checkpoint_revisions = []
        self._sync_legacy_views()

    def active_record(self) -> GoalRecord | None:
        """Return the active record, repairing only a derived index if needed."""
        if not self.goal_records:
            return None
        if self.active_goal_id:
            for index, record in enumerate(self.goal_records):
                if record.goal_id == self.active_goal_id:
                    if record.status is not GoalStatus.VERIFIED:
                        record.status = GoalStatus.ACTIVE
                        self.goal_index = index
                        return record
                    break
        index = min(max(self.goal_index, 0), len(self.goal_records) - 1)
        record = self.goal_records[index]
        if record.status is GoalStatus.VERIFIED:
            record = next(
                (
                    candidate
                    for candidate in self.goal_records
                    if candidate.status is not GoalStatus.VERIFIED
                ),
                record,
            )
            index = self.goal_records.index(record)
        if record.status is not GoalStatus.VERIFIED:
            record.status = GoalStatus.ACTIVE
        self.active_goal_id = record.goal_id
        self.goal_index = index
        return record

    def first_pending(self) -> tuple[int, GoalRecord] | None:
        """Return the first pending goal in current list order."""
        for index, record in enumerate(self.goal_records):
            if record.status is GoalStatus.PENDING:
                return index, record
        return None

    def goal_list_text(self) -> str:
        """Render the ordered goal list for dynamic phase context."""
        return "\n".join(
            f"{index + 1}. [{record.status.value}] {record.text}"
            for index, record in enumerate(self.goal_records)
        )

    def _sync_legacy_views(self) -> None:
        """Update old index-aligned fields from canonical stable records."""
        if self.active_goal_id:
            for record in self.goal_records:
                if record.goal_id == self.active_goal_id:
                    if record.status is not GoalStatus.VERIFIED:
                        record.status = GoalStatus.ACTIVE
                elif record.status is GoalStatus.ACTIVE:
                    record.status = GoalStatus.PENDING
        self.goals = [record.text for record in self.goal_records]
        self.goal_attempts = [record.attempts for record in self.goal_records]
        self.goal_evidence = [
            record.verification_evidence or record.implementation_summary
            for record in self.goal_records
        ]
        self.goal_files = [list(record.files) for record in self.goal_records]
        self.completed_goal_indices = [
            index
            for index, record in enumerate(self.goal_records)
            if record.status is GoalStatus.VERIFIED
        ]
        if self.active_goal_id:
            for index, record in enumerate(self.goal_records):
                if record.goal_id == self.active_goal_id:
                    self.goal_index = index
                    break

    def goal_snapshot(self) -> dict[str, object]:
        """Capture mutable goal state for atomic mutation rollback."""
        return {
            "goal_records": [
                dataclasses.replace(record, files=list(record.files))
                for record in self.goal_records
            ],
            "goals": list(self.goals),
            "goal_index": self.goal_index,
            "goal_attempts": list(self.goal_attempts),
            "goal_evidence": list(self.goal_evidence),
            "goal_files": [list(files) for files in self.goal_files],
            "completed_goal_indices": list(self.completed_goal_indices),
            "active_goal_id": self.active_goal_id,
            "goal_list_revision": self.goal_list_revision,
            "goal_mutation_receipts": [dict(item) for item in self.goal_mutation_receipts],
        }

    def restore_goal_snapshot(self, snapshot: Mapping[str, object]) -> None:
        """Restore state captured by :meth:`goal_snapshot`."""
        raw_records = snapshot.get("goal_records", [])
        records = raw_records if isinstance(raw_records, list) else []
        self.goal_records = [
            dataclasses.replace(record, files=list(record.files))
            for record in records
            if isinstance(record, GoalRecord)
        ]
        raw_goals = snapshot.get("goals", [])
        self.goals = list(raw_goals) if isinstance(raw_goals, list) else []
        raw_goal_index = snapshot.get("goal_index", 0)
        self.goal_index = raw_goal_index if isinstance(raw_goal_index, int) else 0
        raw_attempts = snapshot.get("goal_attempts", [])
        self.goal_attempts = list(raw_attempts) if isinstance(raw_attempts, list) else []
        raw_evidence = snapshot.get("goal_evidence", [])
        self.goal_evidence = list(raw_evidence) if isinstance(raw_evidence, list) else []
        raw_files = snapshot.get("goal_files", [])
        self.goal_files = (
            [list(files) for files in raw_files if isinstance(files, list)]
            if isinstance(raw_files, list)
            else []
        )
        raw_completed = snapshot.get("completed_goal_indices", [])
        self.completed_goal_indices = list(raw_completed) if isinstance(raw_completed, list) else []
        self.active_goal_id = str(snapshot.get("active_goal_id", ""))
        raw_revision = snapshot.get("goal_list_revision", 0)
        self.goal_list_revision = raw_revision if isinstance(raw_revision, int) else 0
        raw_receipts = snapshot.get("goal_mutation_receipts", [])
        self.goal_mutation_receipts = (
            [dict(item) for item in raw_receipts if isinstance(item, Mapping)]
            if isinstance(raw_receipts, list)
            else []
        )


def _make_clarify_tools(
    event: asyncio.Event,
    data: dict[str, str],
) -> list[ToolLike]:
    """Return the only tool that can end the clarify phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def complete_clarification(notes: str) -> dict[str, object]:
        """Record the clarification notes and advance to goal decision.

        Args:
            notes: A summary of the questions asked and the answers received.
        """
        if not notes.strip():
            return {
                "ok": False,
                "error": "The notes were rejected: they must not be empty.",
                "fix": "Call complete_clarification(notes) with a summary of your questions and answers.",
            }
        data["notes"] = notes.strip()
        event.set()
        return {
            "ok": True,
            "message": "Clarification recorded. The goal-decision phase starts next.",
        }

    return [complete_clarification]


def _make_decide_goals_tools(
    event: asyncio.Event,
    data: dict[str, list[str]],
) -> list[ToolLike]:
    """Return the only tool that can end the decide-goals phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def finalize_goals(goals: list[str | dict[str, str]]) -> dict[str, object]:
        """Record the ordered goals and advance to implementing goal #0.

        Args:
            goals: The concrete, testable goals to satisfy, in order. Each item
                is normally a string; for compatibility, an object containing
                a string ``text`` (or ``goal``/``description``) field is also
                accepted.
        """
        if not isinstance(goals, list):
            return {
                "ok": False,
                "error": "The goals were rejected: goals must be a JSON array.",
                "fix": "Call finalize_goals(goals) with one or more concrete goals.",
            }

        cleaned: list[str] = []
        invalid_indexes: list[int] = []
        for index, item in enumerate(goals):
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, Mapping):
                candidates = [
                    candidate.strip()
                    for key in ("text", "goal", "description")
                    if isinstance(candidate := item.get(key), str)
                ]
                text = next((candidate for candidate in candidates if candidate), "")
                if not candidates:
                    # An object without a supported text field is malformed,
                    # rather than an empty goal.
                    invalid_indexes.append(index)
                    continue
            else:
                invalid_indexes.append(index)
                continue

            if text:
                cleaned.append(text)

        if invalid_indexes:
            indexes = ", ".join(str(index) for index in invalid_indexes)
            return {
                "ok": False,
                "error": (
                    "The goals were rejected: goal items at index "
                    f"{indexes} must be strings or objects with a string text field."
                ),
                "fix": "Use finalize_goals(goals) with strings or {'text': '...'} objects.",
            }
        if not cleaned:
            return {
                "ok": False,
                "error": "The goals were rejected: at least one non-empty goal is required.",
                "fix": "Call finalize_goals(goals) with one or more concrete goals.",
            }
        data["goals"] = cleaned
        event.set()
        return {
            "ok": True,
            "message": f"{len(cleaned)} goals recorded. Implementation starts with goal #0.",
        }

    return [finalize_goals]


def _make_implement_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[ToolLike]:
    """Return the only tool that can end the implement-goal phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def goal_implemented(summary: str, files: list[str]) -> dict[str, object]:
        """Signal that the current goal has been implemented.

        Args:
            summary: What was implemented for the current goal.
            files: The files created or modified for this goal.
        """
        if not isinstance(summary, str) or not summary.strip():
            return {
                "ok": False,
                "error": "The summary was rejected: it must not be empty.",
                "fix": "Call goal_implemented(summary, files) describing what you implemented.",
            }
        if len(summary.strip()) > _MAX_RECORD_TEXT_CHARS:
            return {
                "ok": False,
                "error": f"The summary was rejected: it exceeds {_MAX_RECORD_TEXT_CHARS} characters.",
                "fix": "Provide a concise implementation summary.",
            }
        cleaned_files, files_error = _clean_files(files)
        if cleaned_files is None:
            return {
                "ok": False,
                "error": f"The files were rejected: {files_error}.",
                "fix": "Call goal_implemented(summary, files) with a list of path strings.",
            }
        data["summary"] = summary.strip()
        data["files"] = cleaned_files
        event.set()
        return {"ok": True, "message": "Implementation recorded. Verification starts next."}

    return [goal_implemented]


def _make_verify_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[ToolLike]:
    """Return the pass/retry decision tools for the verify-goal phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def verify_goal(satisfied: bool, evidence: str) -> dict[str, object]:
        """Report whether the current goal is satisfied.

        Args:
            satisfied: True when the goal is fully satisfied, False to retry.
            evidence: The checks, test results, or inspection that support the verdict.
        """
        if not isinstance(evidence, str) or not evidence.strip():
            return {
                "ok": False,
                "error": "The evidence was rejected: it must not be empty.",
                "fix": "Call verify_goal(satisfied, evidence) with concrete evidence.",
            }
        if len(evidence.strip()) > _MAX_RECORD_TEXT_CHARS:
            return {
                "ok": False,
                "error": f"The evidence was rejected: it exceeds {_MAX_RECORD_TEXT_CHARS} characters.",
                "fix": "Provide bounded evidence with the relevant check results.",
            }
        data["satisfied"] = bool(satisfied)
        data["evidence"] = evidence.strip()
        event.set()
        return {"ok": True, "message": "Verdict recorded."}

    return [verify_goal]


def _make_summarize_tools(
    event: asyncio.Event,
    data: dict[str, object],
    can_complete: Callable[[], bool] | None = None,
) -> list[ToolLike]:
    """Return the only tool that can end the summarize phase."""
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def complete_workflow(summary: str, files: list[str]) -> dict[str, object]:
        """Record the final summary and finish the workflow.

        Args:
            summary: A concise summary of everything that was done.
            files: The complete list of files affected by the run.
        """
        if not isinstance(summary, str) or not summary.strip():
            return {
                "ok": False,
                "error": "The summary was rejected: it must not be empty.",
                "fix": "Call complete_workflow(summary, files) with the final summary.",
            }
        if len(summary.strip()) > _MAX_RECORD_TEXT_CHARS:
            return {
                "ok": False,
                "error": f"The summary was rejected: it exceeds {_MAX_RECORD_TEXT_CHARS} characters.",
                "fix": "Provide a concise final summary.",
            }
        cleaned_files, files_error = _clean_files(files)
        if cleaned_files is None:
            return {
                "ok": False,
                "error": f"The files were rejected: {files_error}.",
                "fix": "Call complete_workflow(summary, files) with a list of path strings.",
            }
        if can_complete is not None and not can_complete():
            return {
                "ok": False,
                "error_code": "pending_goals",
                "error": "The workflow cannot complete while a goal is pending or active.",
                "fix": "Implement and verify every pending goal before calling complete_workflow.",
            }
        data["summary"] = summary.strip()
        data["files"] = cleaned_files
        event.set()
        return {"ok": True, "message": "Workflow complete."}

    return [complete_workflow]


def _make_goal_mutation_tools(
    append_goal_callback: Callable[[str], Awaitable[dict[str, object]]],
    insert_goal_callback: Callable[[int, str], Awaitable[dict[str, object]]],
) -> list[ToolLike]:
    """Return durable, non-transitioning goal-list mutation tools.

    The callbacks own validation, identity generation, checkpointing, and
    rollback. Keeping these decorators thin ensures that the provider schema
    stays small and both operations share exactly the same atomicity rules.
    """
    from lauren_ai._tools import tool
    from agenthicc.tools.capabilities import tool_control

    @tool_control
    @tool()
    async def append_goal(goal: str) -> dict[str, object]:
        """Append one newly discovered pending goal to the ordered goal list.

        This operation does not finish or change the current goal. Continue
        the current implementation/verification cycle after the call.

        Args:
            goal: One concrete, testable goal to add at the end of the list.
        """
        return await append_goal_callback(goal)

    @tool_control
    @tool()
    async def insert_goal(index: int, goal: str) -> dict[str, object]:
        """Insert one newly discovered pending goal at a zero-based position.

        This operation does not finish or change the current goal. Continue
        the current implementation/verification cycle after the call.

        Args:
            index: Zero-based position in the current list, from 0 through its length.
            goal: One concrete, testable goal to insert at that position.
        """
        return await insert_goal_callback(index, goal)

    # Lauren's schema generator intentionally stays provider-neutral and does
    # not infer JSON Schema keywords such as ``minimum`` or
    # ``additionalProperties`` from Python annotations.  These are part of
    # this public control-tool contract, so tighten only these two generated
    # schemas after decoration.  The callable annotations remain the source of
    # truth for runtime invocation and schema introspection.
    from lauren_ai._tools import TOOL_META, ToolMeta

    for function in (append_goal, insert_goal):
        metadata = cast(ToolMeta, vars(function)[TOOL_META])
        input_schema = metadata.parameters.get("input_schema")
        if isinstance(input_schema, dict):
            input_schema["additionalProperties"] = False
            properties = input_schema.get("properties")
            if isinstance(properties, dict):
                index_schema = properties.get("index")
                if isinstance(index_schema, dict):
                    index_schema["minimum"] = 0

    return [append_goal, insert_goal]


class GoalFlowRunner(CodePlanRunner):
    """State-machine runner for goal_flow.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none of
    code_plan's own phases execute — this runner owns the whole flow.
    """

    workflow_name = "goal_flow"
    total_phases = 5

    def __init__(
        self,
        config: "WorkflowConfig",
        mode_manager: "ModeManager | None" = None,
    ) -> None:
        """Initialize the runner and serialize goal-list mutations per owner."""
        super().__init__(config, mode_manager)
        self._goal_mutation_lock = asyncio.Lock()

    def _goal_limits(self) -> tuple[int, int]:
        """Return configured ``(max_goals, max_text_chars)`` safety limits."""
        params = self._cfg.params
        max_goals = params.max_goals if isinstance(params, GoalFlowParams) else _DEFAULT_MAX_GOALS
        max_text = (
            params.max_goal_text_chars
            if isinstance(params, GoalFlowParams)
            else _DEFAULT_MAX_GOAL_TEXT_CHARS
        )
        if not isinstance(max_goals, int) or isinstance(max_goals, bool) or max_goals < 1:
            max_goals = _DEFAULT_MAX_GOALS
        if not isinstance(max_text, int) or isinstance(max_text, bool) or max_text < 1:
            max_text = _DEFAULT_MAX_GOAL_TEXT_CHARS
        return min(max_goals, _MAX_CONFIGURED_GOALS), min(max_text, _MAX_CONFIGURED_GOAL_TEXT_CHARS)

    @staticmethod
    def _mutation_error(code: str, message: str, fix: str) -> dict[str, object]:
        """Return a bounded, actionable goal-mutation rejection."""
        return {
            "ok": False,
            "error_code": code,
            "error": message[:512],
            "fix": fix[:512],
            "message": f"{message[:400]} Fix: {fix[:400]}",
        }

    async def _append_goal(self, ctx: GoalContext, goal: str) -> dict[str, object]:
        """Append one goal through the shared atomic mutation path."""
        return await self._mutate_goal(ctx, operation="append", goal=goal)

    async def _insert_goal(
        self,
        ctx: GoalContext,
        index: int,
        goal: str,
    ) -> dict[str, object]:
        """Insert one goal through the shared atomic mutation path."""
        return await self._mutate_goal(ctx, operation="insert", goal=goal, index=index)

    async def _mutate_goal(
        self,
        ctx: GoalContext,
        *,
        operation: Literal["append", "insert"],
        goal: str,
        index: int | None = None,
    ) -> dict[str, object]:
        """Validate, checkpoint, and commit one goal-list mutation atomically."""
        async with self._goal_mutation_lock:
            if ctx.state not in {GoalState.IMPLEMENT_GOAL, GoalState.VERIFY_GOAL}:
                return self._mutation_error(
                    "goal_mutation_unavailable_phase",
                    "Goal-list mutations are available only during implementation or verification.",
                    "Continue the current phase and call append_goal(goal) or insert_goal(index, goal) there.",
                )
            if not isinstance(goal, str):
                return self._mutation_error(
                    "goal_text_invalid",
                    "The goal was rejected: goal must be a string.",
                    "Call the tool with one concrete string goal.",
                )
            cleaned_goal = goal.strip()
            if not cleaned_goal:
                return self._mutation_error(
                    "goal_text_empty",
                    "The goal was rejected: goal must not be empty.",
                    "Provide one concrete, testable goal.",
                )
            max_goals, max_text_chars = self._goal_limits()
            if len(cleaned_goal) > max_text_chars:
                return self._mutation_error(
                    "goal_text_too_long",
                    f"The goal was rejected: goal exceeds {max_text_chars} characters.",
                    "Shorten the goal while retaining its testable outcome.",
                )
            if len(ctx.goal_records) >= max_goals:
                return self._mutation_error(
                    "goal_list_limit_reached",
                    f"The goal was rejected: the list is limited to {max_goals} goals.",
                    "Combine related work into an existing goal or complete pending goals first.",
                )
            if operation == "insert":
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index > len(ctx.goal_records)
                ):
                    return self._mutation_error(
                        "goal_index_invalid",
                        "The goal was rejected: index must be an integer from 0 through the current list length.",
                        f"Call insert_goal(index, goal) with an index in 0..{len(ctx.goal_records)}.",
                    )
                position = index
            else:
                position = len(ctx.goal_records)

            handle = self._cfg.workflow_handle
            if handle is None or not handle.checkpoint_supported:
                return self._mutation_error(
                    "goal_checkpoint_unavailable",
                    "The goal was not added because durable workflow checkpointing is unavailable.",
                    "Continue without mutating the goal list or resume this workflow with durable state enabled.",
                )
            snapshot = ctx.goal_snapshot()
            if handle.claim_owner_id is not None:
                claim_owner = handle.checkpoint_store.claim_owner(handle.run_id)
                if claim_owner != handle.claim_owner_id:
                    ctx.restore_goal_snapshot(snapshot)
                    return self._mutation_error(
                        "goal_checkpoint_conflict",
                        "The goal was not added because this workflow owner no longer holds the run claim.",
                        "Resume the run through its live owner before mutating the goal list.",
                    )
            if ctx.active_record() is None:
                ctx.restore_goal_snapshot(snapshot)
                return self._mutation_error(
                    "goal_mutation_context_missing",
                    "The goal was rejected because no active goal context is available.",
                    "Complete the initial goal decision before adding discovered work.",
                )

            revision = ctx.goal_list_revision + 1
            record = GoalRecord(
                goal_id=uuid.uuid4().hex,
                text=cleaned_goal,
                status=GoalStatus.PENDING,
                created_revision=revision,
                created_phase=ctx.state.name,
            )
            ctx.goal_records.insert(position, record)
            ctx.goal_list_revision = revision
            receipt = GoalMutationReceipt(
                revision=revision,
                operation=operation,
                goal_id=record.goal_id,
                index=position,
                phase=ctx.state.name,
                active_goal_id=ctx.active_goal_id or None,
            )
            ctx.goal_mutation_receipts = [
                *ctx.goal_mutation_receipts,
                receipt.to_payload(),
            ][-_MAX_MUTATION_RECEIPTS:]
            ctx._sync_legacy_views()
            handle.attach_context(ctx)
            try:
                handle.save_checkpoint(reason="goal_list_mutated")
            except Exception as exc:  # noqa: BLE001
                ctx.restore_goal_snapshot(snapshot)
                handle.attach_context(ctx)
                return self._mutation_error(
                    "goal_checkpoint_unavailable",
                    f"The goal was not added because its checkpoint failed: {type(exc).__name__}.",
                    "Retry the mutation after durable checkpoint storage is available.",
                )

            self._cfg.conv_store.append_event(
                "goal_list_mutated",
                {
                    "operation": operation,
                    "goal_id": record.goal_id,
                    "index": position,
                    "goal_count": len(ctx.goal_records),
                    "goal_list_revision": revision,
                    "active_goal_id": ctx.active_goal_id or None,
                    "active_goal_index": ctx.goal_index,
                    "phase": ctx.state.name,
                },
            )
            return {
                "ok": True,
                "operation": operation,
                "goal_id": record.goal_id,
                "index": position,
                "goal_count": len(ctx.goal_records),
                "goal_list_revision": revision,
                "active_goal_id": ctx.active_goal_id or None,
                "active_goal_index": ctx.goal_index,
                "message": (
                    "Goal added and checkpointed. Continue the current goal; "
                    "the new goal is pending."
                ),
            }

    # ------------------------------------------------------------------ driver
    async def run(self, intent: str) -> GoalContext:
        """Drive clarify -> decide -> per-goal implement/verify -> summarize."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        self._run_id = run_id
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        ctx = GoalContext(
            intent=intent,
            run_id=run_id,
            state=GoalState.CLARIFY,
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
                    state.name.lower(), self._phase_index(state), ctx.phase_iteration
                )
            match state:
                case GoalState.CLARIFY:
                    state = await self._clarify(ctx, memory)
                case GoalState.DECIDE_GOALS:
                    state = await self._decide_goals(ctx, memory)
                case GoalState.IMPLEMENT_GOAL:
                    state = await self._implement_goal(ctx, memory)
                case GoalState.VERIFY_GOAL:
                    state = await self._verify_goal(ctx, memory)
                case GoalState.SUMMARIZE:
                    state = await self._summarize(ctx, memory)
            log.info("goal_flow \u2192 %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> GoalContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, GoalContext):
            raise TypeError("goal_flow resume requires GoalContext")
        self._run_id = context.run_id
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else context.shared_memory
        )
        if memory is None:
            memory = ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
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
                    state.name.lower(), self._phase_index(state), context.phase_iteration
                )
            match state:
                case GoalState.CLARIFY:
                    state = await self._clarify(context, memory)
                case GoalState.DECIDE_GOALS:
                    state = await self._decide_goals(context, memory)
                case GoalState.IMPLEMENT_GOAL:
                    state = await self._implement_goal(context, memory)
                case GoalState.VERIFY_GOAL:
                    state = await self._verify_goal(context, memory)
                case GoalState.SUMMARIZE:
                    state = await self._summarize(context, memory)
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: GoalState) -> int:
        return {
            GoalState.CLARIFY: 0,
            GoalState.DECIDE_GOALS: 1,
            GoalState.IMPLEMENT_GOAL: 2,
            GoalState.VERIFY_GOAL: 3,
            GoalState.SUMMARIZE: 4,
        }.get(state, 0)

    def _checkpoint_completed_goal(
        self,
        ctx: GoalContext,
        goal_index: int | str,
        next_state: GoalState,
    ) -> None:
        """Persist the exact boundary after one goal is implemented and verified.

        The state and handle cursor are moved to ``next_state`` before the
        checkpoint is serialized.  If the process disappears immediately
        after verification, recovery therefore starts with the next goal (or
        summary) and cannot replay the completed goal's side effects.
        """
        if isinstance(goal_index, str):
            index = next(
                (
                    candidate_index
                    for candidate_index, candidate in enumerate(ctx.goal_records)
                    if candidate.goal_id == goal_index
                ),
                -1,
            )
        else:
            index = goal_index
        if not ctx.goal_records:
            # Preserve the pre-dynamic helper behavior for integrations that
            # call this method before a goal list exists. Real workflow runs
            # always initialize records before reaching this boundary.
            if index == 0 and index not in ctx.completed_goal_indices:
                ctx.completed_goal_indices.append(index)
            ctx.state = next_state
            return
        if index < 0 or index >= len(ctx.goal_records):
            raise ValueError(f"goal completion index is out of range: {goal_index!r}")
        record = ctx.goal_records[index]
        if record.status is GoalStatus.VERIFIED:
            return

        record.status = GoalStatus.VERIFIED
        pending = ctx.first_pending()
        if pending is None:
            ctx.active_goal_id = ""
        else:
            ctx.goal_index, next_record = pending
            ctx.active_goal_id = next_record.goal_id
        ctx._sync_legacy_views()
        handle = self._cfg.workflow_handle
        if handle is None:
            # Headless callers without a session-owned handle have no durable
            # checkpoint store.  The typed context still records completion so
            # a caller that supplies a handle later cannot lose the boundary.
            ctx.state = next_state
            return
        if not handle.checkpoint_supported:
            raise RuntimeError(
                "goal_flow cannot checkpoint a completed goal because its "
                "workflow context codec is unavailable"
            )

        ctx.state = next_state
        handle.attach_context(ctx)
        # ``update_phase(..., persist=False)`` selects the continuation cursor
        # without creating an intermediate checkpoint. The goal-completion
        # save below is the single durable boundary for this transition.
        handle.update_phase(
            next_state.name.lower(),
            self._phase_index(next_state),
            ctx.phase_iteration,
            persist=False,
        )
        expected_revision = handle.checkpoint_revision + 1
        ctx.goal_checkpoint_revisions.append(expected_revision)
        checkpoint = handle.save_checkpoint(reason=f"goal_{index + 1}_completed")
        if checkpoint.revision != expected_revision:  # pragma: no cover - defensive invariant
            raise RuntimeError(
                "goal_flow checkpoint revision advanced unexpectedly while "
                f"completing goal {index + 1}"
            )

    # ------------------------------------------------------------- state steps
    async def _clarify(self, ctx: GoalContext, memory: "ShortTermMemory") -> GoalState:
        """Loop until complete_clarification fires; return DECIDE_GOALS or FAILED."""
        self._active_phase_name = "clarify"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, str] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=ctx.intent if attempt == 1 else "Call complete_clarification(notes) now.",
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the CLARIFY phase of goal_flow. Ask as many focused "
                    "clarifying questions as needed through the existing `ask_user` tool "
                    "until the intent is unambiguous; wait for each answer and do not "
                    "guess. When you have enough detail, call "
                    "complete_clarification(notes). Only a successful "
                    "complete_clarification(notes) call changes phase; prose such as "
                    "'done' never advances the workflow. You MUST call it."
                ),
                max_turns=10,
                shared_memory=memory,
                tools=_make_clarify_tools(event, data),
            )
            if event.is_set():
                ctx.clarification_notes = data["notes"]
                return GoalState.DECIDE_GOALS

        ctx.fail_reason = "clarify phase never called complete_clarification()"
        return GoalState.FAILED

    async def _decide_goals(self, ctx: GoalContext, memory: "ShortTermMemory") -> GoalState:
        """Loop until finalize_goals fires; return IMPLEMENT_GOAL or FAILED."""
        self._active_phase_name = "decide_goals"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, list[str]] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Clarification notes:\n{ctx.clarification_notes}"
                    if attempt == 1
                    else "Call finalize_goals(goals) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the DECIDE_GOALS phase of goal_flow. Turn the intent and "
                    "clarification notes into an ordered list of concrete, testable "
                    "goals, then call finalize_goals(goals). Only a successful "
                    "finalize_goals(goals) call changes phase; prose such as 'done' "
                    "never advances the workflow. You MUST call it."
                ),
                max_turns=6,
                shared_memory=memory,
                tools=_make_decide_goals_tools(event, data),
            )
            if event.is_set():
                ctx.initialize_goals(data["goals"])
                return GoalState.IMPLEMENT_GOAL

        ctx.fail_reason = "decide-goals phase never called finalize_goals()"
        return GoalState.FAILED

    async def _implement_goal(self, ctx: GoalContext, memory: "ShortTermMemory") -> GoalState:
        """Loop until goal_implemented fires; return VERIFY_GOAL or FAILED."""
        self._active_phase_name = "implement_goal"
        record = ctx.active_record()
        if record is None:
            ctx.fail_reason = "implement phase has no active goal"
            return GoalState.FAILED
        record.status = GoalStatus.ACTIVE
        ctx._sync_legacy_views()
        idx = ctx.goal_records.index(record)
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            current_goal = record.text
            phase_tools = _make_implement_tools(event, data)
            phase_tools.extend(
                _make_goal_mutation_tools(
                    lambda goal: self._append_goal(ctx, goal),
                    lambda index, goal: self._insert_goal(ctx, index, goal),
                )
            )
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Goal #{idx + 1} of {len(ctx.goals)}:\n{current_goal}\n\n"
                    f"Prior attempts for this goal: {record.attempts}\n\n"
                    f"Current ordered goal list:\n{ctx.goal_list_text()}"
                    if attempt == 1
                    else "Call goal_implemented(summary, files) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the IMPLEMENT_GOAL phase of goal_flow. Work toward the "
                    "current goal using the file, shell, and git tools: read relevant "
                    "code, write or modify files, and run commands as needed. When the "
                    "work reveals a necessary missing goal, call append_goal(goal) to "
                    "queue it at the end or insert_goal(index, goal) to place it at a "
                    "specific zero-based position. A goal mutation is not a phase "
                    "transition: continue the current goal. When the current goal is "
                    "implemented, call goal_implemented(summary, files). Only a "
                    "successful goal_implemented(summary, files) call changes phase; "
                    "prose such as 'done' never advances the workflow. You MUST call it."
                ),
                mode="Yolo",  # unlock write / execute tools for this phase
                max_turns=25,
                shared_memory=memory,
                tools=phase_tools,
            )
            if event.is_set():
                record.attempts += 1
                record.implementation_summary = str(data.get("summary", ""))
                files = data.get("files", [])
                if isinstance(files, list):
                    record.files = [str(f) for f in files]
                record.status = GoalStatus.ACTIVE
                ctx._sync_legacy_views()
                return GoalState.VERIFY_GOAL

        ctx.fail_reason = "implement phase never called goal_implemented()"
        return GoalState.FAILED

    async def _verify_goal(self, ctx: GoalContext, memory: "ShortTermMemory") -> GoalState:
        """Loop until verify_goal fires; branch to next goal, retry, or FAILED."""
        self._active_phase_name = "verify_goal"
        record = ctx.active_record()
        if record is None:
            ctx.fail_reason = "verify phase has no active goal"
            return GoalState.FAILED
        idx = ctx.goal_records.index(record)
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            current_goal = record.text
            phase_tools = _make_verify_tools(event, data)
            phase_tools.extend(
                _make_goal_mutation_tools(
                    lambda goal: self._append_goal(ctx, goal),
                    lambda index, goal: self._insert_goal(ctx, index, goal),
                )
            )
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Goal #{idx + 1} of {len(ctx.goals)}:\n{current_goal}\n\n"
                    f"Implementation summary:\n{record.implementation_summary}\n\n"
                    f"Current ordered goal list:\n{ctx.goal_list_text()}"
                    if attempt == 1
                    else "Call verify_goal(satisfied, evidence) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the VERIFY_GOAL phase of goal_flow. Check that the "
                    "current goal is actually satisfied: inspect the code, run tests or "
                    "commands, and gather concrete evidence. If verification reveals "
                    "necessary missing work, call append_goal(goal) or "
                    "insert_goal(index, goal); these tools do not change phase, so "
                    "continue verifying the current goal. Then call "
                    "verify_goal(satisfied, evidence). Only a successful "
                    "verify_goal(satisfied, evidence) call changes phase; prose such as "
                    "'done' never advances the workflow. You MUST call it."
                ),
                max_turns=12,
                shared_memory=memory,
                tools=phase_tools,
            )
            if event.is_set():
                satisfied = bool(data.get("satisfied", False))
                evidence = str(data.get("evidence", ""))
                record.verification_evidence = evidence
                if not satisfied:
                    # Loop back to the same goal's implementation phase (unbounded).
                    ctx._sync_legacy_views()
                    return GoalState.IMPLEMENT_GOAL
                pending = ctx.first_pending()
                if pending is None:
                    next_state = GoalState.SUMMARIZE
                else:
                    ctx.goal_index, next_record = pending
                    ctx.active_goal_id = next_record.goal_id
                    next_state = GoalState.IMPLEMENT_GOAL
                # ``idx`` is only a presentation projection. A mutation may
                # have shifted the active record since the phase began, so
                # the durable completion boundary must use its stable ID.
                self._checkpoint_completed_goal(ctx, record.goal_id, next_state)
                return next_state

        ctx.fail_reason = "verify phase never called verify_goal()"
        return GoalState.FAILED

    async def _summarize(
        self,
        ctx: GoalContext,
        memory: "ShortTermMemory",
    ) -> GoalState:
        """Loop until complete_workflow fires; return COMPLETE or FAILED."""
        self._active_phase_name = "summarize"
        if any(record.status is not GoalStatus.VERIFIED for record in ctx.goal_records):
            ctx.fail_reason = "summarize phase reached with pending or active goals"
            return GoalState.FAILED
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            goals_block = "\n".join(
                f"- {g}  [attempts={ctx.goal_attempts[i]}]" for i, g in enumerate(ctx.goals)
            )
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"All goals satisfied:\n{goals_block}\n\n"
                    f"Files affected so far: {ctx.files_affected}"
                    if attempt == 1
                    else "Call complete_workflow(summary, files) now."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                system_prompt=(
                    "You are in the SUMMARIZE phase of goal_flow. Write a concise final "
                    "summary of everything that was done and every file affected, then "
                    "call complete_workflow(summary, files). Only a successful "
                    "complete_workflow(summary, files) call changes phase; prose such as "
                    "'done' never advances the workflow. You MUST call it."
                ),
                max_turns=4,
                shared_memory=memory,
                tools=_make_summarize_tools(
                    event,
                    data,
                    can_complete=lambda: all(
                        record.status is GoalStatus.VERIFIED for record in ctx.goal_records
                    ),
                ),
            )
            if event.is_set():
                ctx.summary = str(data.get("summary", ""))
                files = data.get("files", [])
                if isinstance(files, list):
                    ctx.files_affected = [str(f) for f in files]
                return GoalState.COMPLETE

        ctx.fail_reason = "summarize phase never called complete_workflow()"
        return GoalState.FAILED


@dataclasses.dataclass
class GoalFlowParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.goal_flow]."""

    clarify_model: str = ""
    decide_goals_model: str = ""
    implement_model: str = ""
    verify_model: str = ""
    summarize_model: str = ""
    max_goals: int = _DEFAULT_MAX_GOALS
    max_goal_text_chars: int = _DEFAULT_MAX_GOAL_TEXT_CHARS

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "clarify": self.clarify_model,
            "decide_goals": self.decide_goals_model,
            "implement_goal": self.implement_model,
            "verify_goal": self.verify_model,
            "summarize": self.summarize_model,
        }


class GoalFlowWorkflow(WorkflowPlugin):
    """Clarify intent into goals, implement and verify each goal, then summarize."""

    name = "goal_flow"
    description = "Clarify intent into goals, implement and verify each goal, then summarize."
    mode_bindings: list[str] = []  # manual only — invoke with /workflow goal_flow
    # Declarative metadata for the registry and the TUI phase counter; the runner
    # above is what actually executes, and it follows exactly this graph (the
    # runner additionally loops implement -> verify per goal at runtime).
    phases = [
        PhaseSpec(
            name="clarify",
            max_turns=10,
            next="decide_goals",
            system_prompt_override=(
                "You are in the CLARIFY phase of goal_flow. Ask clarifying questions "
                "through the existing `ask_user` tool as needed, then call "
                "complete_clarification(notes); only a successful transition-tool call "
                "changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="decide_goals",
            max_turns=6,
            next="implement_goal",
            system_prompt_override=(
                "You are in the DECIDE_GOALS phase of goal_flow. Decide the ordered "
                "goals and call finalize_goals(goals); only a successful "
                "transition-tool call changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="implement_goal",
            max_turns=25,
            next="verify_goal",
            mode_override="Yolo",
            system_prompt_override=(
                "You are in the IMPLEMENT_GOAL phase of goal_flow. Implement the "
                "current goal. If you discover necessary missing work, call "
                "append_goal(goal) or insert_goal(index, goal); those tools do not "
                "change phase. Then call goal_implemented(summary, files) for the "
                "active goal; only that successful transition-tool call changes "
                "phase, never prose."
            ),
        ),
        PhaseSpec(
            name="verify_goal",
            max_turns=12,
            next="implement_goal",
            system_prompt_override=(
                "You are in the VERIFY_GOAL phase of goal_flow. Verify the current "
                "goal is satisfied. If verification discovers necessary missing work, "
                "call append_goal(goal) or insert_goal(index, goal); those tools do not "
                "change phase. Then call verify_goal(satisfied, evidence); only a "
                "successful transition-tool call changes phase, never prose."
            ),
        ),
        PhaseSpec(
            name="summarize",
            max_turns=4,
            output_schema="free_text",
            system_prompt_override=(
                "You are in the SUMMARIZE phase of goal_flow. Write the final summary "
                "and call complete_workflow(summary, files); only a successful "
                "transition-tool call changes phase, never prose."
            ),
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, GoalContext):
            raise TypeError("goal_flow checkpoint requires GoalContext")
        # Keep compatibility fields authoritative when an older integration
        # has edited them directly, while ensuring the new record section is
        # what a dynamic-goal checkpoint restores.
        records: list[GoalRecord] = []
        completed = set(context.completed_goal_indices)
        for index, source in enumerate(context.goal_records):
            record = dataclasses.replace(source, files=list(source.files))
            if index in completed:
                record.status = GoalStatus.VERIFIED
            if index < len(context.goal_attempts):
                record.attempts = max(0, context.goal_attempts[index])
            if index < len(context.goal_files):
                record.files = [str(path) for path in context.goal_files[index]]
            if index < len(context.goal_evidence):
                evidence = str(context.goal_evidence[index])
                if record.status is GoalStatus.VERIFIED:
                    record.verification_evidence = evidence
                elif not record.implementation_summary:
                    record.implementation_summary = evidence
            records.append(record)
        active_goal_id = context.active_goal_id
        if active_goal_id and not any(
            record.goal_id == active_goal_id and record.status is not GoalStatus.VERIFIED
            for record in records
        ):
            # Compatibility callers can still update completed_goal_indices
            # directly. Never emit a checkpoint whose active cursor points at
            # a now-verified record; select the first real pending/active
            # record or leave the cursor empty for summary/terminal state.
            active_goal_id = next(
                (record.goal_id for record in records if record.status is not GoalStatus.VERIFIED),
                "",
            )
        for record in records:
            if record.goal_id == active_goal_id and record.status is not GoalStatus.VERIFIED:
                record.status = GoalStatus.ACTIVE
        completed_indices = [
            index for index, record in enumerate(records) if record.status is GoalStatus.VERIFIED
        ]
        return {
            "goal_list_version": 2,
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "clarification_notes": context.clarification_notes,
            "goals": context.goals,
            "goal_index": context.goal_index,
            "goal_attempts": context.goal_attempts,
            "goal_evidence": context.goal_evidence,
            "goal_files": context.goal_files,
            "completed_goal_indices": completed_indices,
            "goal_checkpoint_revisions": context.goal_checkpoint_revisions,
            "goal_records": [record.to_payload() for record in records],
            "active_goal_id": active_goal_id,
            "goal_list_revision": context.goal_list_revision,
            "goal_mutation_receipts": [dict(receipt) for receipt in context.goal_mutation_receipts],
            "summary": context.summary,
            "files_affected": context.files_affected,
            "fail_reason": context.fail_reason,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> GoalContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", GoalState.CLARIFY.name))
        try:
            state = GoalState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown goal_flow state: {raw_state}") from exc

        def _str_list(value: object) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item) for item in value]

        def _int_value(value: object, default: int = 0) -> int:
            """Decode a scalar checkpoint value without trusting JSON types."""
            if isinstance(value, bool):
                return default
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    return default
            return default

        def _nonnegative(value: object, field_name: str, *, default: int = 0) -> int:
            """Decode a strict non-negative integer in versioned goal data."""
            if value is None:
                return default
            parsed = _int_value(value, default=-1)
            if parsed < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
            return parsed

        raw_files = payload.get("goal_files", [])
        goal_files: list[list[str]] = []
        if isinstance(raw_files, list):
            for entry in raw_files:
                goal_files.append(_str_list(entry) if isinstance(entry, list) else [])

        raw_goals = _str_list(payload.get("goals"))
        raw_attempts = payload.get("goal_attempts", [])
        goal_attempts = (
            [_int_value(value) for value in raw_attempts] if isinstance(raw_attempts, list) else []
        )
        goal_evidence = _str_list(payload.get("goal_evidence"))
        raw_completed = payload.get("completed_goal_indices", [])
        completed_indices = (
            {_int_value(value) for value in raw_completed}
            if isinstance(raw_completed, list)
            else set()
        )
        raw_records = payload.get("goal_records")
        if raw_records is not None and not isinstance(raw_records, list):
            raise ValueError("goal_records must be a list when present")
        records: list[GoalRecord] = []
        if isinstance(raw_records, list):
            seen_ids: set[str] = set()
            for index, item in enumerate(raw_records):
                if not isinstance(item, Mapping):
                    raise ValueError(f"goal record at index {index} is not an object")
                goal_id = item.get("goal_id")
                text = item.get("text")
                if not isinstance(goal_id, str) or not goal_id.strip():
                    raise ValueError(f"goal record at index {index} has no valid goal_id")
                if goal_id in seen_ids:
                    raise ValueError(f"duplicate goal_id in checkpoint: {goal_id}")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"goal record at index {index} has no valid text")
                if len(text.strip()) > _MAX_CONFIGURED_GOAL_TEXT_CHARS:
                    raise ValueError(f"goal record text at index {index} is too long")
                seen_ids.add(goal_id)
                raw_status = item.get("status", GoalStatus.PENDING.value)
                try:
                    status = GoalStatus(str(raw_status))
                except ValueError as exc:
                    raise ValueError(
                        f"unknown goal status at index {index}: {raw_status!r}"
                    ) from exc
                raw_record_files = item.get("files", [])
                if not isinstance(raw_record_files, list):
                    raise ValueError(f"goal record files at index {index} are not a list")
                cleaned_files, files_error = _clean_files(raw_record_files)
                if cleaned_files is None:
                    raise ValueError(
                        f"goal record files at index {index} are invalid: {files_error}"
                    )
                attempts = _nonnegative(item.get("attempts"), f"goal record {index} attempts")
                created_revision = _nonnegative(
                    item.get("created_revision"),
                    f"goal record {index} created_revision",
                )
                implementation_summary = item.get("implementation_summary", "")
                verification_evidence = item.get("verification_evidence", "")
                created_phase = item.get("created_phase", "decide_goals")
                if not isinstance(implementation_summary, str):
                    raise ValueError(
                        f"goal record implementation_summary at index {index} must be a string"
                    )
                if not isinstance(verification_evidence, str):
                    raise ValueError(
                        f"goal record verification_evidence at index {index} must be a string"
                    )
                if len(implementation_summary) > _MAX_RECORD_TEXT_CHARS:
                    raise ValueError(
                        f"goal record implementation_summary at index {index} is too long"
                    )
                if len(verification_evidence) > _MAX_RECORD_TEXT_CHARS:
                    raise ValueError(
                        f"goal record verification_evidence at index {index} is too long"
                    )
                if not isinstance(created_phase, str) or not created_phase.strip():
                    raise ValueError(
                        f"goal record created_phase at index {index} must be a non-empty string"
                    )
                records.append(
                    GoalRecord(
                        goal_id=goal_id,
                        text=text.strip(),
                        status=(GoalStatus.VERIFIED if index in completed_indices else status),
                        attempts=attempts,
                        implementation_summary=implementation_summary,
                        verification_evidence=verification_evidence,
                        files=cleaned_files,
                        created_revision=created_revision,
                        created_phase=created_phase,
                    )
                )
        else:
            # Pre-PRD-185 payloads have only strings and parallel arrays.
            # Stable IDs include the original index so duplicate text remains
            # distinguishable and the migration is repeatable.
            for index, text in enumerate(raw_goals):
                is_verified = index in completed_indices
                records.append(
                    GoalRecord(
                        goal_id=_legacy_goal_id(index, text),
                        text=text,
                        status=GoalStatus.VERIFIED if is_verified else GoalStatus.PENDING,
                        attempts=(goal_attempts[index] if index < len(goal_attempts) else 0),
                        implementation_summary=(
                            goal_evidence[index] if index < len(goal_evidence) else ""
                        ),
                        verification_evidence=(
                            goal_evidence[index]
                            if is_verified and index < len(goal_evidence)
                            else ""
                        ),
                        files=(goal_files[index] if index < len(goal_files) else []),
                        created_phase="legacy",
                    )
                )

        raw_receipts = payload.get("goal_mutation_receipts", [])
        receipts: list[dict[str, object]] = []
        if raw_receipts is not None and not isinstance(raw_receipts, list):
            raise ValueError("goal_mutation_receipts must be a list when present")
        last_receipt_revision = 0
        if isinstance(raw_receipts, list):
            previous_revision = 0
            record_ids = {record.goal_id for record in records}
            for receipt_index, item in enumerate(raw_receipts[-_MAX_MUTATION_RECEIPTS:]):
                if not isinstance(item, Mapping):
                    raise ValueError(f"goal mutation receipt {receipt_index} is not an object")
                receipt = {str(key): value for key, value in item.items()}
                revision = _nonnegative(
                    receipt.get("revision"),
                    f"goal mutation receipt {receipt_index} revision",
                )
                if revision == 0 or revision <= previous_revision:
                    raise ValueError(
                        "goal mutation receipts must have increasing positive revisions"
                    )
                operation = receipt.get("operation")
                if operation not in {"append", "insert"}:
                    raise ValueError(f"unknown goal mutation operation: {operation!r}")
                goal_id = receipt.get("goal_id")
                if not isinstance(goal_id, str) or goal_id not in record_ids:
                    raise ValueError("goal mutation receipt references an unknown goal_id")
                receipt_index_value = _nonnegative(
                    receipt.get("index"),
                    f"goal mutation receipt {receipt_index} index",
                )
                if receipt_index_value > len(records):
                    raise ValueError("goal mutation receipt index is outside the goal list")
                phase = receipt.get("phase")
                if phase not in {GoalState.IMPLEMENT_GOAL.name, GoalState.VERIFY_GOAL.name}:
                    raise ValueError(f"invalid goal mutation receipt phase: {phase!r}")
                active_receipt_id = receipt.get("active_goal_id")
                if active_receipt_id is not None and (
                    not isinstance(active_receipt_id, str) or active_receipt_id not in record_ids
                ):
                    raise ValueError("goal mutation receipt has an unknown active_goal_id")
                receipt["revision"] = revision
                receipt["index"] = receipt_index_value
                receipts.append(receipt)
                previous_revision = revision
                last_receipt_revision = revision

        goal_list_revision = _nonnegative(payload.get("goal_list_revision"), "goal_list_revision")
        if last_receipt_revision > goal_list_revision:
            raise ValueError("goal mutation receipt revision exceeds goal_list_revision")
        active_goal_id = payload.get("active_goal_id", "")
        if not isinstance(active_goal_id, str):
            active_goal_id = ""
        if (
            records
            and any(record.status is not GoalStatus.VERIFIED for record in records)
            and not active_goal_id
            and state
            in {
                GoalState.IMPLEMENT_GOAL,
                GoalState.VERIFY_GOAL,
            }
        ):
            if isinstance(raw_records, list):
                raise ValueError("active_goal_id is required for an active dynamic goal checkpoint")
            # Legacy checkpoints did not have an ID. Their saved numeric
            # cursor is the only safe migration source; do not restart at
            # clarification or guess from the first unverified record.
            legacy_index = _int_value(payload.get("goal_index"), -1)
            if (
                legacy_index < 0
                or legacy_index >= len(records)
                or records[legacy_index].status is GoalStatus.VERIFIED
            ):
                raise ValueError("legacy goal cursor does not identify a pending goal")
            active_goal_id = records[legacy_index].goal_id
        if active_goal_id:
            for record in records:
                if record.goal_id == active_goal_id and record.status is not GoalStatus.VERIFIED:
                    record.status = GoalStatus.ACTIVE
                    break

        restored = GoalContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=_int_value(payload.get("phase_iteration", 0)),
            clarification_notes=str(payload.get("clarification_notes", "")),
            goals=raw_goals,
            goal_index=_int_value(payload.get("goal_index", 0)),
            goal_attempts=goal_attempts,
            goal_evidence=goal_evidence,
            goal_files=goal_files,
            completed_goal_indices=sorted(completed_indices),
            goal_checkpoint_revisions=[
                _int_value(value) for value in _str_list(payload.get("goal_checkpoint_revisions"))
            ],
            summary=str(payload.get("summary", "")),
            files_affected=_str_list(payload.get("files_affected")),
            fail_reason=str(payload.get("fail_reason", "")),
            shared_memory=cast("ShortTermMemory | None", memory),
            goal_records=records,
            active_goal_id=active_goal_id,
            goal_list_revision=goal_list_revision,
            goal_mutation_receipts=receipts,
        )
        if restored.active_goal_id and not any(
            record.goal_id == restored.active_goal_id for record in restored.goal_records
        ):
            raise ValueError("active_goal_id does not identify a goal record")
        if (
            restored.active_goal_id
            and next(
                record
                for record in restored.goal_records
                if record.goal_id == restored.active_goal_id
            ).status
            is GoalStatus.VERIFIED
        ):
            raise ValueError("active_goal_id cannot identify a verified goal")
        if restored.goal_records:
            restored._sync_legacy_views()
        return restored

    @classmethod
    def build_runner(
        cls,
        config: "WorkflowConfig",
        mode_manager: "ModeManager | None",
    ) -> GoalFlowRunner:
        """Return this workflow's own state-machine runner."""
        return GoalFlowRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.goal_flow]."""
        return GoalFlowParams(
            clarify_model=str(source.get("clarify_model", "") or ""),
            decide_goals_model=str(source.get("decide_goals_model", "") or ""),
            implement_model=str(source.get("implement_model", "") or ""),
            verify_model=str(source.get("verify_model", "") or ""),
            summarize_model=str(source.get("summarize_model", "") or ""),
            max_goals=_positive_int(source.get("max_goals"), _DEFAULT_MAX_GOALS),
            max_goal_text_chars=_positive_int(
                source.get("max_goal_text_chars"), _DEFAULT_MAX_GOAL_TEXT_CHARS
            ),
        )
