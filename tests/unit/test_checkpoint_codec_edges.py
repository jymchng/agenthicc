"""Direct codec and validation coverage for workflow checkpoints."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from enum import Enum

import pytest

from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
    _as_dict_list,
    _as_float,
    _as_int,
    _as_metadata,
    _decode_enum,
    _encode,
    context_from_payload,
    context_to_payload,
)
from agenthicc.workflows.plugin import PhaseOutput, PhaseSpec, WorkflowContext, WorkflowPlugin

pytestmark = pytest.mark.unit


class _State(Enum):
    READY = "ready"


@dataclasses.dataclass
class _Data:
    number: int
    ignored: object = dataclasses.field(default=None, repr=False)


def test_checkpoint_codec_primitives_and_enum_errors() -> None:
    assert _as_int(True, 7) == 7
    assert _as_int(2.9, 0) == 2
    assert _as_int("3", 0) == 3
    assert _as_int("bad", 7) == 7
    assert _as_int(object(), 7) == 7
    assert _as_float(2, 0.0) == 2.0
    assert _as_float("2.5", 0.0) == 2.5
    assert _as_float("bad", 7.0) == 7.0
    assert _as_float(object(), 7.0) == 7.0
    assert _as_metadata({1: "one"}) == {"1": "one"}
    assert _as_metadata("bad") == {}
    assert _as_dict_list("bad") == []
    assert _as_dict_list([{"x": 1}, "bad"]) == [{"x": 1}]
    assert _encode(_State.READY) == {"__enum__": "READY"}
    assert _encode(_Data(2)) == {"number": 2, "ignored": None}
    assert _encode({1: {"values": (1, 2)}}) == {"1": {"values": [1, 2]}}
    assert _decode_enum("raw", _State) == "raw"
    assert _decode_enum({"__enum__": "READY"}, _State) is _State.READY
    with pytest.raises(CheckpointValidationError, match="must be a string"):
        _decode_enum({"__enum__": 1}, _State)
    with pytest.raises(CheckpointValidationError, match="unknown enum"):
        _decode_enum({"__enum__": "MISSING"}, _State)
    with pytest.raises(CheckpointValidationError, match="unsupported"):
        _encode(object())


def test_workflow_context_codec_and_custom_codec_failures() -> None:
    context = WorkflowContext(
        intent="intent",
        run_id="run",
        workflow_name="demo",
        phase_outputs={
            "plan": PhaseOutput(
                phase_name="plan",
                role="planner",
                full_text="done",
                structured={"steps": 1},
                approved=True,
                metadata={"duration": "1"},
                duration_s=1.5,
            )
        },
        current_phase="review",
        phase_iteration=2,
    )
    payload = context_to_payload(context)
    restored = context_from_payload(payload)
    assert isinstance(restored, WorkflowContext)
    assert restored.phase_outputs["plan"].approved is True
    assert restored.phase_outputs["plan"].duration_s == 1.5

    class Custom(WorkflowPlugin):
        name = "codec_edge"
        phases = [PhaseSpec(name="work")]

        @classmethod
        def checkpoint_context_to_payload(cls, value: object) -> dict[str, object] | None:
            return {"value": str(value)} if value == "supported" else None

        @classmethod
        def checkpoint_context_from_payload(
            cls, payload: dict[str, object], memory: object | None = None
        ) -> object | None:
            return payload.get("value")

    assert context_to_payload("supported", workflow=Custom) == {
        "kind": "CustomContext",
        "fields": {"value": "supported"},
    }
    assert (
        context_from_payload(
            {"kind": "CustomContext", "fields": {"value": "supported"}}, workflow=Custom
        )
        == "supported"
    )
    with pytest.raises(CheckpointValidationError, match="no checkpoint codec"):
        context_to_payload("unsupported", workflow=Custom)
    with pytest.raises(CheckpointValidationError, match="cannot restore"):
        context_from_payload({"kind": "CustomContext", "fields": {}}, workflow=Custom)
    with pytest.raises(CheckpointValidationError, match="unsupported workflow context"):
        context_from_payload({"kind": "Other", "fields": {}}, workflow=Custom)

    code_plan_fallback = context_from_payload(
        {"kind": "CodePlanContext", "fields": {"state": "BAD"}}
    )
    assert getattr(code_plan_fallback, "state").name == "PLAN"
    create_fallback = context_from_payload(
        {"kind": "CreateWorkflowContext", "fields": {"state": "BAD"}}
    )
    assert getattr(create_fallback, "state").name == "DESIGN"


def test_checkpoint_from_dict_rejects_schema_identity_and_numeric_errors() -> None:
    checkpoint = WorkflowCheckpoint(
        run_id="run",
        workflow_name="demo",
        conversation_id="session",
        intent="intent",
        status="paused",
        current_phase=None,
        phase_index=0,
        phase_iteration=0,
        conversation_cursor=0,
        context={},
        plugin_fingerprint="fingerprint",
    )
    valid = checkpoint.to_dict()

    def checked(**changes: object) -> None:
        raw = {**valid, **changes}
        unsigned = dict(raw)
        unsigned.pop("content_hash", None)
        raw["content_hash"] = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with pytest.raises(CheckpointValidationError):
            WorkflowCheckpoint.from_dict(raw)

    with pytest.raises(CheckpointValidationError, match="JSON object"):
        WorkflowCheckpoint.from_dict([])
    checked(schema_version=99)
    with pytest.raises(CheckpointValidationError, match="hash mismatch"):
        WorkflowCheckpoint.from_dict({**valid, "content_hash": "bad"})
    checked(run_id=1)
    checked(status="bogus")
    checked(context=[])
    checked(browser=[])
    checked(phase_index=True)
    checked(phase_index=-1)
