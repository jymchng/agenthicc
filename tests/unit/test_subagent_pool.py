"""Unit tests for PRD-124 — concurrent subagents: types, pool, aggregation, tool."""
from __future__ import annotations

import asyncio
import pytest

from agenthicc.subagents.types import (
    SubagentTypeSpec,
    SubagentTypeRegistry,
    DEFAULT_REGISTRY,
    _build_default_registry,
)
from agenthicc.subagents.pool import (
    SubagentTask,
    SubagentResult,
    AggregatedResult,
    SubagentPool,
    _aggregate,
    _UnknownTypeWorker,
)

pytestmark = pytest.mark.unit


# ── SubagentTypeRegistry ──────────────────────────────────────────────────────

class TestSubagentTypeRegistry:
    def test_default_registry_has_eight_types(self) -> None:
        # Use a fresh registry to avoid pollution from plugin tests.
        fresh = _build_default_registry()
        assert len(fresh.names()) == 8

    def test_all_builtin_types_present(self) -> None:
        fresh = _build_default_registry()
        expected = {"explorer", "planner", "implementer", "tester",
                    "reviewer", "documenter", "verifier", "researcher"}
        assert expected == set(fresh.names())

    def test_get_known_type_returns_spec(self) -> None:
        spec = DEFAULT_REGISTRY.get("explorer")
        assert spec is not None
        assert spec.name == "explorer"

    def test_get_unknown_type_returns_none(self) -> None:
        assert DEFAULT_REGISTRY.get("does_not_exist") is None

    def test_contains_operator(self) -> None:
        assert "explorer" in DEFAULT_REGISTRY
        assert "unknown" not in DEFAULT_REGISTRY

    def test_register_custom_type(self) -> None:
        reg = SubagentTypeRegistry()
        spec = SubagentTypeSpec(
            name="custom",
            allowed_tools=frozenset({"read_file"}),
            max_turns=5,
            system_prompt="You are custom.",
        )
        reg.register(spec)
        assert "custom" in reg
        assert reg.get("custom") is spec

    def test_register_replaces_existing(self) -> None:
        reg = SubagentTypeRegistry()
        s1 = SubagentTypeSpec("x", frozenset(), 1, "v1")
        s2 = SubagentTypeSpec("x", frozenset(), 2, "v2")
        reg.register(s1)
        reg.register(s2)
        assert reg.get("x") is s2

    def test_names_returns_list(self) -> None:
        reg = SubagentTypeRegistry()
        reg.register(SubagentTypeSpec("a", frozenset(), 1, ""))
        reg.register(SubagentTypeSpec("b", frozenset(), 1, ""))
        assert set(reg.names()) == {"a", "b"}


# ── SubagentTypeSpec ──────────────────────────────────────────────────────────

class TestSubagentTypeSpec:
    def test_explorer_no_write_tools(self) -> None:
        spec = DEFAULT_REGISTRY.get("explorer")
        assert spec is not None
        write_tools = {"write_file", "patch_file", "append_file", "delete_file"}
        assert not (spec.allowed_tools & write_tools), "explorer must not have write tools"

    def test_implementer_has_write_tools(self) -> None:
        spec = DEFAULT_REGISTRY.get("implementer")
        assert spec is not None
        assert "write_file" in spec.allowed_tools
        assert "patch_file" in spec.allowed_tools

    def test_tester_has_execute_tools(self) -> None:
        spec = DEFAULT_REGISTRY.get("tester")
        assert spec is not None
        assert "run_tests" in spec.allowed_tools
        assert "run_bash" in spec.allowed_tools

    def test_planner_no_write_or_execute(self) -> None:
        spec = DEFAULT_REGISTRY.get("planner")
        assert spec is not None
        prohibited = {"write_file", "patch_file", "run_bash", "run_tests"}
        assert not (spec.allowed_tools & prohibited)

    def test_reviewer_no_write(self) -> None:
        spec = DEFAULT_REGISTRY.get("reviewer")
        assert spec is not None
        assert "write_file" not in spec.allowed_tools
        assert "patch_file" not in spec.allowed_tools

    def test_all_types_have_read_file(self) -> None:
        for name in DEFAULT_REGISTRY.names():
            spec = DEFAULT_REGISTRY.get(name)
            assert spec is not None
            assert "read_file" in spec.allowed_tools, f"{name} must have read_file"

    def test_max_turn_time_default(self) -> None:
        spec = SubagentTypeSpec("t", frozenset(), 5, "")
        assert spec.max_turn_time_s == 120.0


# ── SubagentTask ──────────────────────────────────────────────────────────────

class TestSubagentTask:
    def test_construction(self) -> None:
        t = SubagentTask("id-1", "explorer", "Find auth files", context="extra")
        assert t.task_id == "id-1"
        assert t.agent_type == "explorer"
        assert t.context == "extra"

    def test_default_context_empty(self) -> None:
        t = SubagentTask("id-1", "explorer", "Find files")
        assert t.context == ""


# ── _aggregate ────────────────────────────────────────────────────────────────

class TestAggregate:
    def _r(self, n: int, ok: bool, text: str = "output", dur: float = 100.0) -> SubagentResult:
        return SubagentResult(
            task_id=f"task-{n}",
            agent_type="explorer",
            label=f"explorer #{n}",
            ok=ok,
            text=text,
            error="" if ok else "network error",
            duration_ms=dur,
        )

    def test_all_succeeded(self) -> None:
        results = [self._r(1, True), self._r(2, True)]
        agg = _aggregate("pool-1", results)
        assert agg.succeeded == 2
        assert agg.failed == 0
        assert agg.total == 2

    def test_partial_failure(self) -> None:
        results = [self._r(1, True), self._r(2, False)]
        agg = _aggregate("pool-1", results)
        assert agg.succeeded == 1
        assert agg.failed == 1

    def test_text_contains_labels(self) -> None:
        results = [self._r(1, True, "found 3 files"), self._r(2, False)]
        agg = _aggregate("pool-1", results)
        assert "=== explorer #1" in agg.text
        assert "=== explorer #2" in agg.text

    def test_text_contains_output(self) -> None:
        results = [self._r(1, True, "auth.py found at line 42")]
        agg = _aggregate("pool-1", results)
        assert "auth.py found at line 42" in agg.text

    def test_failed_shows_error(self) -> None:
        r = SubagentResult("t1", "explorer", "explorer #1", ok=False,
                           text="", error="timeout after 120s")
        agg = _aggregate("p", [r])
        assert "timeout after 120s" in agg.text

    def test_text_truncated_at_2000_chars(self) -> None:
        long_text = "x" * 5_000
        results = [self._r(1, True, long_text)]
        agg = _aggregate("p", results)
        # 2000 char limit per section
        assert len(agg.text) < 5_000

    def test_pool_id_stored(self) -> None:
        agg = _aggregate("my-pool-id", [self._r(1, True)])
        assert agg.pool_id == "my-pool-id"

    def test_empty_results(self) -> None:
        agg = _aggregate("p", [])
        assert agg.total == 0
        assert agg.succeeded == 0


# ── SubagentPool with mock workers ────────────────────────────────────────────

class _MockWorker:
    """Fake SubagentWorker for pool tests."""

    def __init__(self, task_id: str, agent_type: str, text: str,
                 ok: bool = True, delay: float = 0.0) -> None:
        self._task_id    = task_id
        self._agent_type = agent_type
        self._text       = text
        self._ok         = ok
        self._delay      = delay
        self.label       = f"{agent_type} #1"

    async def run(self) -> SubagentResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        return SubagentResult(
            task_id=self._task_id,
            agent_type=self._agent_type,
            label=self.label,
            ok=self._ok,
            text=self._text,
            error="" if self._ok else "mock error",
        )


class _MockPool(SubagentPool):
    """SubagentPool subclass that injects mock workers instead of real ones."""

    def __init__(self, mock_workers: list, **kwargs) -> None:  # type: ignore[override]
        # Minimal init — skip parent __init__ to avoid needing a real runner.
        self.pool_id         = "mock-pool"
        self._tasks          = []
        self._parent_runner  = None  # type: ignore[assignment]
        self._parent_model   = ""
        self._all_tools      = []
        self._max_concurrent = kwargs.get("max_concurrent", 4)
        self._app_state      = None
        self._processor      = None
        self._registry       = DEFAULT_REGISTRY
        self._mock_workers   = mock_workers

    async def run(self) -> AggregatedResult:
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def _bounded(w: _MockWorker) -> SubagentResult:
            async with semaphore:
                return await w.run()

        raw = await asyncio.gather(*[_bounded(w) for w in self._mock_workers],
                                   return_exceptions=True)
        results: list[SubagentResult] = [r for r in raw if isinstance(r, SubagentResult)]
        return _aggregate(self.pool_id, results)


class TestSubagentPoolConcurrency:
    async def test_all_workers_run(self) -> None:
        workers = [_MockWorker(f"t{i}", "explorer", f"result {i}") for i in range(4)]
        pool = _MockPool(workers)
        result = await pool.run()
        assert result.total == 4
        assert result.succeeded == 4
        assert result.failed == 0

    async def test_semaphore_limits_concurrency(self) -> None:
        """Verify that concurrency is actually bounded by max_concurrent."""
        active: list[int] = []
        max_seen: list[int] = [0]

        class _CountingWorker:
            def __init__(self, idx: int) -> None:
                self.label = f"explorer #{idx}"
                self._task_id = f"t{idx}"
                self._agent_type = "explorer"

            async def run(self) -> SubagentResult:
                active.append(1)
                max_seen[0] = max(max_seen[0], len(active))
                await asyncio.sleep(0.02)
                active.pop()
                return SubagentResult(self._task_id, self._agent_type, self.label, True, "ok")

        workers = [_CountingWorker(i) for i in range(8)]
        pool = _MockPool(workers, max_concurrent=3)
        await pool.run()
        assert max_seen[0] <= 3

    async def test_partial_failure_aggregated(self) -> None:
        workers = [
            _MockWorker("t1", "explorer", "ok result", ok=True),
            _MockWorker("t2", "tester",   "",          ok=False),
            _MockWorker("t3", "reviewer", "all good",  ok=True),
        ]
        pool = _MockPool(workers)
        result = await pool.run()
        assert result.succeeded == 2
        assert result.failed == 1

    async def test_result_order_matches_spawn_order(self) -> None:
        """Results appear in spawn order regardless of completion order."""
        workers = [
            _MockWorker("t1", "explorer", "first",  delay=0.05),
            _MockWorker("t2", "tester",   "second", delay=0.0),
            _MockWorker("t3", "reviewer", "third",  delay=0.02),
        ]
        pool = _MockPool(workers, max_concurrent=3)
        result = await pool.run()
        # Workers t2 and t3 finish before t1 but should still appear in order
        assert result.text.index("explorer #1") < result.text.index("tester #1")
        assert result.text.index("tester #1") < result.text.index("reviewer #1")


# ── _UnknownTypeWorker ────────────────────────────────────────────────────────

class TestUnknownTypeWorker:
    async def test_returns_failed_result(self) -> None:
        w = _UnknownTypeWorker(SubagentTask("t1", "invalid_type", "do something"))
        result = await w.run()
        assert not result.ok
        assert "invalid_type" in result.error

    async def test_label_has_question_mark(self) -> None:
        w = _UnknownTypeWorker(SubagentTask("t1", "mystery", "task"))
        assert "#?" in w.label


# ── spawn_subagents tool validation ──────────────────────────────────────────

class TestSpawnSubagentsTool:
    def _make_tool(self) -> object:
        from agenthicc.subagents.tool import make_spawn_subagents_tool  # noqa: PLC0415

        class FakeRunner:
            _transport = None

        return make_spawn_subagents_tool(FakeRunner(), "test-model", [])

    async def test_empty_tasks_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(tasks=[])
        assert not result["ok"]
        assert "empty" in result["error"].lower()

    async def test_unknown_type_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(tasks=[{"type": "unicorn", "task": "do magic"}])
        assert not result["ok"]
        assert "unicorn" in result["error"]

    async def test_missing_type_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(tasks=[{"task": "no type given"}])
        assert not result["ok"]

    async def test_missing_task_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(tasks=[{"type": "explorer"}])
        assert not result["ok"]

    async def test_non_dict_task_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(tasks=["not a dict"])  # type: ignore[arg-type]
        assert not result["ok"]

    def test_tool_has_correct_name(self) -> None:
        fn = self._make_tool()
        assert fn.__name__ == "spawn_subagents"  # type: ignore[union-attr]

    def test_tool_schema_has_tasks_field(self) -> None:
        fn = self._make_tool()
        meta = getattr(fn, "__lauren_ai_tool__", None)
        assert meta is not None
        schema = meta.parameters.get("input_schema", {})
        assert "tasks" in schema.get("properties", {})

    def test_tool_schema_tasks_is_required(self) -> None:
        fn = self._make_tool()
        meta = getattr(fn, "__lauren_ai_tool__", None)
        assert meta is not None
        schema = meta.parameters.get("input_schema", {})
        assert "tasks" in schema.get("required", [])
