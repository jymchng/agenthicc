"""Serializable workflow checkpoints and context codecs (PRD-156)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.workflows.plugin import PhaseOutput

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointValidationError",
    "WorkflowCheckpoint",
    "context_from_payload",
    "context_to_payload",
    "workflow_fingerprint",
]

CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 1_000_000


class CheckpointValidationError(ValueError):
    """Raised when a checkpoint cannot be trusted or rehydrated."""


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _as_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_as_metadata(item) for item in value if isinstance(item, dict)]


def _encode(value: object) -> object:
    """Convert supported context values into JSON-compatible data."""
    if isinstance(value, Enum):
        return {"__enum__": value.name}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _encode(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name not in {"shared_memory"}
        }
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise CheckpointValidationError(
        f"unsupported checkpoint value type: {type(value).__module__}.{type(value).__qualname__}"
    )


def _decode_enum(value: object, enum_type: type[Enum]) -> Enum | object:
    if not isinstance(value, dict) or value.get("__enum__") is None:
        return value
    name = value.get("__enum__")
    if not isinstance(name, str):
        raise CheckpointValidationError("encoded enum name must be a string")
    try:
        return enum_type[name]
    except KeyError as exc:
        raise CheckpointValidationError(f"unknown enum value {name!r}") from exc


def _phase_output_from_payload(payload: object) -> "PhaseOutput":
    from agenthicc.workflows.plugin import PhaseOutput

    data = _clean_fields(payload)
    metadata = data.get("metadata")
    approved_value = data.get("approved")
    return PhaseOutput(
        phase_name=str(data.get("phase_name", "")),
        role=str(data.get("role", "")),
        full_text=str(data.get("full_text", "")),
        structured=_as_metadata(data.get("structured")),
        approved=approved_value if isinstance(approved_value, bool) else None,
        metadata=_as_metadata(metadata),
        agent_id=str(data.get("agent_id", "")),
        duration_s=_as_float(data.get("duration_s"), 0.0),
    )


def context_to_payload(
    context: object,
    *,
    workflow: type[object] | None = None,
) -> dict[str, object]:
    """Encode one supported workflow context without serializing memory."""
    from agenthicc.workflows.code_plan.state import CodePlanContext
    from agenthicc.workflows.create_workflow.state import CreateWorkflowContext, PhaseArtifact
    from agenthicc.workflows.plugin import WorkflowContext

    if isinstance(context, WorkflowContext):
        return {"kind": "WorkflowContext", "fields": _encode(context)}
    if isinstance(context, CodePlanContext):
        return {"kind": "CodePlanContext", "fields": _encode(context)}
    if isinstance(context, CreateWorkflowContext):
        return {"kind": "CreateWorkflowContext", "fields": _encode(context)}
    if workflow is not None:
        codec = getattr(workflow, "checkpoint_context_to_payload", None)
        if callable(codec):
            custom_fields = codec(context)
            if isinstance(custom_fields, dict):
                return {"kind": "CustomContext", "fields": _encode(custom_fields)}
    # Keep the explicit type check above so a dataclass from an extension cannot
    # be silently persisted without a declared codec.
    if isinstance(context, PhaseArtifact):  # pragma: no cover - defensive
        raise CheckpointValidationError("a phase artifact is not a workflow context")
    raise CheckpointValidationError(
        f"workflow context has no checkpoint codec: {type(context).__qualname__}"
    )


def _clean_fields(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise CheckpointValidationError("context fields must be an object")
    return dict(payload)


def context_from_payload(
    payload: dict[str, object],
    *,
    memory: "ShortTermMemory | None" = None,
    workflow: type[object] | None = None,
) -> object:
    """Restore one built-in workflow context and attach shared memory."""
    from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState
    from agenthicc.workflows.create_workflow.state import (
        CreateWorkflowContext,
        CreateWorkflowState,
        PhaseArtifact,
    )
    from agenthicc.workflows.plugin import WorkflowContext

    kind = payload.get("kind")
    fields = _clean_fields(payload.get("fields"))
    if kind == "WorkflowContext":
        raw_outputs = fields.get("phase_outputs", {})
        outputs: dict[str, PhaseOutput] = (
            {str(name): _phase_output_from_payload(value) for name, value in raw_outputs.items()}
            if isinstance(raw_outputs, dict)
            else {}
        )
        return WorkflowContext(
            intent=str(fields.get("intent", "")),
            run_id=str(fields.get("run_id", "")),
            workflow_name=str(fields.get("workflow_name", "")),
            phase_outputs=outputs,
            current_phase=(
                str(fields["current_phase"]) if fields.get("current_phase") is not None else None
            ),
            phase_iteration=_as_int(fields.get("phase_iteration"), 0),
        )
    if kind == "CodePlanContext":
        raw_state = fields.get("state", {"__enum__": "PLAN"})
        state = _decode_enum(raw_state, CodePlanState)
        if not isinstance(state, CodePlanState):
            state = CodePlanState.PLAN
        return CodePlanContext(
            intent=str(fields.get("intent", "")),
            run_id=str(fields.get("run_id", "")),
            plan=str(fields.get("plan", "")),
            execute_mode=str(fields.get("execute_mode", "Safe")),
            execute_summary=str(fields.get("execute_summary", "")),
            review_summary=str(fields.get("review_summary", "")),
            rejection_reason=str(fields.get("rejection_reason", "")),
            fail_reason=str(fields.get("fail_reason", "")),
            command_outcomes=(_as_dict_list(fields.get("command_outcomes"))),
            shared_memory=memory,
            state=state,
            phase_iteration=_as_int(fields.get("phase_iteration"), 0),
        )
    if kind == "CreateWorkflowContext":
        raw_artifacts = fields.get("artifacts", {})
        artifacts: dict[str, PhaseArtifact] = {}
        if isinstance(raw_artifacts, dict):
            for name, raw in raw_artifacts.items():
                if isinstance(raw, dict):
                    artifacts[str(name)] = PhaseArtifact(
                        phase=str(raw.get("phase", name)),
                        kind=str(raw.get("kind", "")),
                        content=str(raw.get("content", "")),
                        metadata=_as_metadata(raw.get("metadata")),
                        created_at=_as_float(raw.get("created_at"), time.time()),
                    )
        raw_state = fields.get("state", {"__enum__": "DESIGN"})
        state = _decode_enum(raw_state, CreateWorkflowState)
        if not isinstance(state, CreateWorkflowState):
            state = CreateWorkflowState.DESIGN
        return CreateWorkflowContext(
            intent=str(fields.get("intent", "")),
            run_id=str(fields.get("run_id", "")),
            design=str(fields.get("design", "")),
            workflow_name=str(fields.get("workflow_name", "")),
            generated_path=str(fields.get("generated_path", "")),
            generation_summary=str(fields.get("generation_summary", "")),
            validation_report=str(fields.get("validation_report", "")),
            validation_summary=str(fields.get("validation_summary", "")),
            rejection_reason=str(fields.get("rejection_reason", "")),
            suggestion=str(fields.get("suggestion", "")),
            fail_reason=str(fields.get("fail_reason", "")),
            repair_cycles=_as_int(fields.get("repair_cycles"), 0),
            artifacts=artifacts,
            command_outcomes=(_as_dict_list(fields.get("command_outcomes"))),
            shared_memory=memory,
            state=state,
            phase_iteration=_as_int(fields.get("phase_iteration"), 0),
        )
    if kind == "CustomContext" and workflow is not None:
        codec = getattr(workflow, "checkpoint_context_from_payload", None)
        if callable(codec):
            custom_context = codec(fields, memory)
            if custom_context is not None:
                return custom_context
        raise CheckpointValidationError(
            f"workflow {getattr(workflow, 'name', workflow.__name__)} cannot restore custom context"
        )
    raise CheckpointValidationError(f"unsupported workflow context kind: {kind!r}")


def workflow_fingerprint(plugin: type[object]) -> str:
    """Return a stable fingerprint for the declared workflow topology."""
    name = getattr(plugin, "name", plugin.__qualname__)
    phases = getattr(plugin, "phases", ())
    topology = [
        {
            "name": getattr(phase, "name", ""),
            "next": getattr(phase, "next", None),
            "on_reject": getattr(phase, "on_reject", None),
        }
        for phase in phases
    ]
    raw = json.dumps({"name": name, "phases": topology}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowCheckpoint:
    """Versioned, bounded workflow metadata pointing into session history."""

    run_id: str
    workflow_name: str
    conversation_id: str
    intent: str
    status: str
    current_phase: str | None
    phase_index: int
    phase_iteration: int
    conversation_cursor: int
    context: dict[str, object]
    plugin_fingerprint: str
    revision: int = 0
    reason: str = ""
    browser: dict[str, object] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "conversation_id": self.conversation_id,
            "intent": self.intent,
            "status": self.status,
            "current_phase": self.current_phase,
            "phase_index": self.phase_index,
            "phase_iteration": self.phase_iteration,
            "conversation_cursor": self.conversation_cursor,
            "context": self.context,
            "plugin_fingerprint": self.plugin_fingerprint,
            "revision": self.revision,
            "reason": self.reason,
            "browser": self.browser,
            "created_at": self.created_at,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload["content_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> "WorkflowCheckpoint":
        if not isinstance(raw, dict):
            raise CheckpointValidationError("checkpoint must be a JSON object")
        schema = raw.get("schema_version")
        if schema != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointValidationError(f"unsupported checkpoint schema: {schema!r}")
        required = (
            "run_id",
            "workflow_name",
            "conversation_id",
            "intent",
            "status",
            "phase_index",
            "phase_iteration",
            "conversation_cursor",
            "context",
            "plugin_fingerprint",
            "revision",
        )
        missing = [key for key in required if key not in raw]
        if missing:
            raise CheckpointValidationError(f"checkpoint is missing fields: {', '.join(missing)}")
        expected = raw.get("content_hash")
        unsigned = dict(raw)
        unsigned.pop("content_hash", None)
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
        if expected != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
            raise CheckpointValidationError("checkpoint content hash mismatch")
        if not all(
            isinstance(raw[key], str)
            for key in (
                "run_id",
                "workflow_name",
                "conversation_id",
                "intent",
                "status",
                "plugin_fingerprint",
            )
        ):
            raise CheckpointValidationError("checkpoint identity fields must be strings")
        if raw["status"] not in {
            "running",
            "pausing",
            "paused",
            "resuming",
            "complete",
            "failed",
            "discarded",
        }:
            raise CheckpointValidationError(f"unsupported workflow status: {raw['status']!r}")
        if not isinstance(raw["context"], dict):
            raise CheckpointValidationError("checkpoint context must be an object")
        browser = raw.get("browser", {})
        if not isinstance(browser, dict):
            raise CheckpointValidationError("checkpoint browser metadata must be an object")
        numeric = ("phase_index", "phase_iteration", "conversation_cursor", "revision")
        if not all(isinstance(raw[key], int) and not isinstance(raw[key], bool) for key in numeric):
            raise CheckpointValidationError("checkpoint numeric fields must be integers")
        if any(raw[key] < 0 for key in numeric):
            raise CheckpointValidationError("checkpoint numeric fields must not be negative")
        phase = raw.get("current_phase")
        if phase is not None and not isinstance(phase, str):
            raise CheckpointValidationError("current_phase must be a string or null")
        return cls(
            run_id=str(raw["run_id"]),
            workflow_name=str(raw["workflow_name"]),
            conversation_id=str(raw["conversation_id"]),
            intent=str(raw["intent"]),
            status=str(raw["status"]),
            current_phase=phase,
            phase_index=int(raw["phase_index"]),
            phase_iteration=int(raw["phase_iteration"]),
            conversation_cursor=int(raw["conversation_cursor"]),
            context=dict(raw["context"]),
            plugin_fingerprint=str(raw["plugin_fingerprint"]),
            revision=int(raw["revision"]),
            reason=str(raw.get("reason", "")),
            browser=dict(raw.get("browser", {}))
            if isinstance(raw.get("browser", {}), dict)
            else {},
            created_at=float(raw.get("created_at", time.time()) or time.time()),
        )
