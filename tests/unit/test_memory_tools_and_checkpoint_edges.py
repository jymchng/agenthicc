"""Edge coverage for memory tools and checkpoint validation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pytest

from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.workflows.checkpoint import (
    CheckpointValidationError,
    WorkflowCheckpoint,
    context_from_payload,
    context_to_payload,
    workflow_fingerprint,
)
from agenthicc.workflows.memory_tools import make_memory_tools
from agenthicc.workflows.plugin import PhaseOutput, PhaseSpec, WorkflowContext, WorkflowPlugin

pytestmark = pytest.mark.unit


class _MemoryRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def write(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("write", *args, kwargs))
        return {"ok": True, "stored": True}

    async def read(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("read", *args, kwargs))
        return {"found": True, "value": "remembered"}

    async def publish_artifact(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("artifact", *args, kwargs))
        return {"ok": True, "artifact_id": "a1", "size_bytes": 3}


class _SemanticIndex:
    async def search(self, query: str, *, top_k: int) -> list[tuple[str, float]]:
        assert query == "prior decision"
        assert top_k == 2
        return [("doc-1", 0.98765), ("doc-2", 0.5)]


@pytest.mark.asyncio
async def test_memory_tools_return_safe_unavailable_results() -> None:
    tools = {tool.__name__: tool for tool in make_memory_tools(None, None)}

    assert (await tools["memory_write"]("k", "v")) == {
        "ok": False,
        "error": "memory_not_available",
    }
    assert (await tools["memory_read"]("k"))["found"] is False
    assert (await tools["semantic_search"]("q"))["results"] == []
    assert (await tools["publish_artifact"]("body"))["artifact_id"] is None


@pytest.mark.asyncio
async def test_memory_tools_forward_tiers_ttl_and_search_scores() -> None:
    router = _MemoryRouter()
    tools = {tool.__name__: tool for tool in make_memory_tools(router, _SemanticIndex())}

    await tools["memory_write"]("key", "value", "session", "notes", 30)
    await tools["memory_read"]("key", "global", "archive")
    assert await tools["semantic_search"]("prior decision", 2) == {
        "results": [
            {"doc_id": "doc-1", "score": 0.9877},
            {"doc_id": "doc-2", "score": 0.5},
        ]
    }
    await tools["publish_artifact"]("abc", "text/markdown")
    assert router.calls == [
        ("write", "key", "value", {"tier": "session", "namespace": "notes", "ttl": 30}),
        ("read", "key", {"tier": "global", "namespace": "archive"}),
        ("artifact", "abc", {"content_type": "text/markdown"}),
    ]
    assert get_tool_capabilities(tools["memory_write"]) == frozenset({ToolCapability.WRITE})
    assert get_tool_capabilities(tools["semantic_search"]) == frozenset({ToolCapability.SEARCH})


class _State(Enum):
    FIRST = "first"


@dataclass
class _DataclassContext:
    state: _State
    values: tuple[str, ...]


def test_checkpoint_rejects_unsupported_values_and_untyped_contexts() -> None:
    from agenthicc.workflows import checkpoint

    with pytest.raises(CheckpointValidationError, match="unsupported checkpoint value"):
        checkpoint._encode(object())
    assert checkpoint._encode(_State.FIRST) == {"__enum__": "FIRST"}
    assert checkpoint._decode_enum({"__enum__": "FIRST"}, _State) is _State.FIRST
    with pytest.raises(CheckpointValidationError, match="enum name"):
        checkpoint._decode_enum({"__enum__": 1}, _State)
    with pytest.raises(CheckpointValidationError, match="unknown enum"):
        checkpoint._decode_enum({"__enum__": "MISSING"}, _State)
    with pytest.raises(CheckpointValidationError, match="workflow context"):
        context_to_payload(_DataclassContext(_State.FIRST, ("x",)))
    with pytest.raises(CheckpointValidationError, match="context fields"):
        context_from_payload({"kind": "WorkflowContext", "fields": []})


def test_checkpoint_round_trips_phase_outputs_and_custom_codec() -> None:
    context = WorkflowContext(
        intent="intent",
        run_id="run",
        workflow_name="demo",
        current_phase="review",
        phase_iteration=2,
        phase_outputs={
            "review": PhaseOutput(
                phase_name="review",
                role="critic",
                full_text="looks good",
                structured={"approved": True},
                approved=True,
                metadata={"source": "test"},
                agent_id="agent-1",
                duration_s=1.5,
            )
        },
    )
    restored = context_from_payload(context_to_payload(context))
    assert isinstance(restored, WorkflowContext)
    assert restored.phase_outputs["review"].approved is True
    assert restored.phase_outputs["review"].duration_s == 1.5

    class Plugin(WorkflowPlugin):
        name = "custom_codec"
        phases = [PhaseSpec(name="work")]

        @classmethod
        def checkpoint_context_to_payload(cls, value: object) -> dict[str, object] | None:
            return {"value": str(value)}

        @classmethod
        def checkpoint_context_from_payload(
            cls, payload: dict[str, object], memory: object | None = None
        ) -> object:
            return (payload["value"], memory)

    payload = context_to_payload("value", workflow=Plugin)
    assert payload == {"kind": "CustomContext", "fields": {"value": "value"}}
    assert context_from_payload(payload, memory="shared", workflow=Plugin) == ("value", "shared")
    assert len(workflow_fingerprint(Plugin)) == 64


def test_checkpoint_from_dict_validates_schema_identity_numbers_and_status() -> None:
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
    raw = checkpoint.to_dict()
    assert WorkflowCheckpoint.from_dict(raw) == checkpoint
    invalids = [
        {**raw, "schema_version": 999},
        {key: value for key, value in raw.items() if key != "run_id"},
        {**raw, "status": "unknown"},
        {**raw, "phase_index": -1},
        {**raw, "context": []},
        {**raw, "current_phase": 1},
    ]
    for invalid in invalids:
        if invalid.get("schema_version") == raw["schema_version"]:
            unsigned = dict(invalid)
            unsigned.pop("content_hash", None)
            import hashlib
            import json

            invalid["content_hash"] = hashlib.sha256(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        with pytest.raises(CheckpointValidationError):
            WorkflowCheckpoint.from_dict(invalid)
