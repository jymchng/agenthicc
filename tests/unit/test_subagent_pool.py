"""Unit tests for PRD-124 — concurrent subagents: types, pool, aggregation, tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agenthicc.subagents.pool import (
    AggregatedResult,
    SubagentPool,
    SubagentResult,
    SubagentTask,
    _aggregate,
    _make_child_app_state,
    _make_child_workspace_access,
    _UnknownTypeWorker,
)
from agenthicc.subagents.tool import _find_cached_result, _tasks_fingerprint
from agenthicc.subagents.types import (
    DEFAULT_REGISTRY,
    SubagentTypeRegistry,
    SubagentTypeSpec,
    _build_default_registry,
)
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def test_subagent_policy_state_inherits_parent_mode_without_mutating_signal() -> None:
    parent = AppState.create()
    parent.active_mode.set(build_default_registry().get("Plan"))

    from agenthicc.subagents.policy import SubagentExecutionPolicy

    child = _make_child_app_state(SubagentExecutionPolicy.from_app_state(parent))

    assert child is not None
    assert child is not parent
    assert child.active_mode().name == "Plan"
    assert parent.active_mode().name == "Plan"


def test_headless_subagent_policy_state_remains_absent() -> None:
    assert _make_child_app_state(None) is None


@pytest.mark.asyncio
async def test_subagent_workspace_policy_inherits_safe_without_changing_parent(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    scope = WorkspaceScope.create(workspace)

    parent = AppState.create()
    parent.active_mode.set(build_default_registry().get("Safe"))

    class DenyApproval:
        async def request_approval(self, _request: object) -> object:
            from agenthicc.tools.approval import ApprovalResponse

            return ApprovalResponse(allowed=False)

    parent_policy = WorkspaceAccessPolicy(
        scope,
        mode_provider=parent.active_mode,
        approval_service=DenyApproval(),  # type: ignore[arg-type]
    )
    from agenthicc.subagents.policy import SubagentExecutionPolicy

    child = _make_child_app_state(SubagentExecutionPolicy.from_app_state(parent))
    child_policy = _make_child_workspace_access(parent_policy, child, DenyApproval())

    assert child is not None
    assert child_policy is not None
    result = await child_policy.authorize_tool("write_file", {"path": str(outside / "created.txt")})

    assert result.allowed is False
    assert child_policy.mode_name == "Safe"
    assert parent_policy.mode_name == "Safe"


# ── SubagentTypeRegistry ──────────────────────────────────────────────────────


class TestSubagentTypeRegistry:
    def test_default_registry_has_nine_types(self) -> None:
        # Use a fresh registry to avoid pollution from plugin tests.
        fresh = _build_default_registry()
        assert len(fresh.names()) == 9

    def test_all_builtin_types_present(self) -> None:
        fresh = _build_default_registry()
        expected = {
            "explorer",
            "planner",
            "implementer",
            "executor",
            "tester",
            "reviewer",
            "documenter",
            "verifier",
            "researcher",
        }
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

    def test_executor_can_build_and_verify(self) -> None:
        spec = DEFAULT_REGISTRY.get("executor")
        assert spec is not None
        assert {"write_file", "run_bash", "run_command", "run_tests"} <= spec.allowed_tools
        assert spec.max_turn_time_s == 300.0

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

    def test_output_schema_is_normalized_on_type_registration(self) -> None:
        schema: dict[str, object] = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        }

        spec = SubagentTypeSpec("reporter", frozenset(), 1, "", output_schema=schema)
        schema["properties"] = {}

        assert spec.output_schema == {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        }

    def test_invalid_output_schema_is_rejected_on_type_registration(self) -> None:
        with pytest.raises(ValueError, match="secret-shaped"):
            SubagentTypeSpec(
                "reporter",
                frozenset(),
                1,
                "",
                output_schema={
                    "type": "object",
                    "properties": {"access_token": {"type": "string"}},
                },
            )

    def test_max_turn_time_default(self) -> None:
        spec = SubagentTypeSpec("t", frozenset(), 5, "")
        assert spec.max_turn_time_s == 3_600.0


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
        r = SubagentResult(
            "t1", "explorer", "explorer #1", ok=False, text="", error="timeout after 120s"
        )
        agg = _aggregate("p", [r])
        assert "timeout after 120s" in agg.text

    def test_text_preserves_complete_worker_output(self) -> None:
        long_text = "x" * 5_000
        results = [self._r(1, True, long_text)]
        agg = _aggregate("p", results)
        # Only the TUI/kernel display projections are bounded. The parent
        # receives the complete artefact so it can persist or apply it.
        assert long_text in agg.text

    def test_structured_values_survive_aggregation_in_order(self) -> None:
        first = SubagentResult(
            task_id="task-1",
            agent_type="reporter",
            label="reporter #1",
            ok=True,
            text='{"count":1}',
            value={"count": 1},
        )
        second = SubagentResult(
            task_id="task-2",
            agent_type="reporter",
            label="reporter #2",
            ok=True,
            text='{"count":2}',
            value={"count": 2},
        )

        aggregated = _aggregate("pool", [first, second])

        assert aggregated.values == (
            {
                "task_id": "task-1",
                "agent_type": "reporter",
                "label": "reporter #1",
                "value": {"count": 1},
            },
            {
                "task_id": "task-2",
                "agent_type": "reporter",
                "label": "reporter #2",
                "value": {"count": 2},
            },
        )

    def test_failed_results_do_not_contribute_structured_values(self) -> None:
        result = SubagentResult(
            task_id="task-1",
            agent_type="reporter",
            label="reporter #1",
            ok=False,
            text="",
            error="invalid result",
            value={"count": 1},
        )

        assert _aggregate("pool", [result]).values == ()

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

    def __init__(
        self, task_id: str, agent_type: str, text: str, ok: bool = True, delay: float = 0.0
    ) -> None:
        self._task_id = task_id
        self._agent_type = agent_type
        self._text = text
        self._ok = ok
        self._delay = delay
        self.label = f"{agent_type} #1"

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
        self.pool_id = "mock-pool"
        self._tasks = []
        self._parent_runner = None  # type: ignore[assignment]
        self._parent_model = ""
        self._all_tools = []
        self._max_concurrent = kwargs.get("max_concurrent", 4)
        self._app_state = None
        self._processor = None
        self._registry = DEFAULT_REGISTRY
        self._mock_workers = mock_workers

    async def run(self) -> AggregatedResult:
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def _bounded(w: _MockWorker) -> SubagentResult:
            async with semaphore:
                return await w.run()

        raw = await asyncio.gather(
            *[_bounded(w) for w in self._mock_workers], return_exceptions=True
        )
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
            _MockWorker("t2", "tester", "", ok=False),
            _MockWorker("t3", "reviewer", "all good", ok=True),
        ]
        pool = _MockPool(workers)
        result = await pool.run()
        assert result.succeeded == 2
        assert result.failed == 1

    async def test_result_order_matches_spawn_order(self) -> None:
        """Results appear in spawn order regardless of completion order."""
        workers = [
            _MockWorker("t1", "explorer", "first", delay=0.05),
            _MockWorker("t2", "tester", "second", delay=0.0),
            _MockWorker("t3", "reviewer", "third", delay=0.02),
        ]
        pool = _MockPool(workers, max_concurrent=3)
        result = await pool.run()
        # Workers t2 and t3 finish before t1 but should still appear in order
        assert result.text.index("explorer #1") < result.text.index("tester #1")
        assert result.text.index("tester #1") < result.text.index("reviewer #1")


class TestSubagentPoolGovernance:
    @staticmethod
    def _pool(**overrides: object) -> SubagentPool:
        options: dict[str, object] = {
            "tasks": [SubagentTask("t1", "explorer", "inspect")],
            "parent_runner": SimpleNamespace(_transport=object()),
            "parent_model": "model",
            "all_tools": [],
        }
        options.update(overrides)
        return SubagentPool(**options)  # type: ignore[arg-type]

    def test_depth_over_limit_is_refused_before_worker_construction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agenthicc.subagents import pool as pool_module

        def unexpected_worker(*_args: object, **_kwargs: object) -> object:
            pytest.fail("workers must not be constructed for a refused pool")

        monkeypatch.setattr(pool_module, "SubagentWorker", unexpected_worker)

        with pytest.raises(
            pool_module.SubagentAdmissionError,
            match=r"current depth 2, limit 1.*subagents.max_depth",
        ):
            self._pool(depth=2, max_depth=1)

    def test_depth_specific_task_limit_is_refused_before_worker_construction(self) -> None:
        from agenthicc.config import SubagentDepthLimit, SubagentSettings
        from agenthicc.subagents.pool import SubagentAdmissionError

        settings = SubagentSettings(
            max_tasks=64,
            depth_limits={2: SubagentDepthLimit(max_tasks=1)},
        )
        task_limit, _, task_source, _ = settings.limits_for_depth(2)
        assert task_limit == 1
        assert task_source == "subagents.depth_limits.2.max_tasks"

        tasks = [
            SubagentTask("t1", "explorer", "first"),
            SubagentTask("t2", "explorer", "second"),
        ]
        depth_one_pool = self._pool(tasks=tasks, depth=1, max_depth=2, max_tasks=64)
        assert len(depth_one_pool._tasks) == 2

        with pytest.raises(
            SubagentAdmissionError,
            match=r"requested 2, limit 1 from subagents.depth_limits.2.max_tasks",
        ):
            self._pool(
                tasks=tasks,
                depth=2,
                max_depth=2,
                max_tasks=task_limit,
                max_tasks_source=task_source,
            )

    async def test_process_wide_pool_limit_reports_active_pools(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from agenthicc.memory.journal import ConversationJournal
        from agenthicc.subagents import pool as pool_module

        journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
        monkeypatch.setattr(
            pool_module,
            "_ACTIVE_POOLS",
            {"running-pool": SimpleNamespace(_max_active_pools=1)},
        )
        pool = self._pool(
            max_active_pools=1,
            conversation_journal=journal,
            task_fingerprint="pool-fingerprint",
            parent_run_id="parent-run",
        )

        with pytest.raises(
            pool_module.SubagentAdmissionError,
            match=r"Active subagent pool limit 1 reached; running pools: running-pool",
        ):
            await pool.run()
        assert journal.fold_subagent_pool_lifecycle() == []

    def test_repeated_task_signature_is_refused_with_ancestry_chain(self) -> None:
        from agenthicc.subagents.pool import SubagentAdmissionError

        with pytest.raises(
            SubagentAdmissionError,
            match=r"Repeated subagent task in ancestry: explorer: inspect -> explorer: inspect",
        ):
            self._pool(
                depth=2,
                max_depth=2,
                ancestry=(("explorer", "inspect"),),
            )


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


class TestSubagentWorkerTimeout:
    async def test_timeout_is_a_failed_result_with_actionable_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agenthicc.subagents import pool as pool_module

        async def block(_worker: object) -> str:
            await asyncio.sleep(0.05)
            return "never reached"

        monkeypatch.setattr(pool_module.SubagentWorker, "_execute", block)
        spec = SubagentTypeSpec(
            name="short",
            allowed_tools=frozenset(),
            max_turns=1,
            system_prompt="",
        )
        worker = pool_module.SubagentWorker(
            task=SubagentTask("t1", "short", "wait"),
            spec=spec,
            index=1,
            parent_runner=object(),  # type: ignore[arg-type]
            parent_model="model",
            all_tools=[],
            timeout_s=0.001,
        )

        result = await worker.run()

        assert result.ok is False
        assert "timed out after" in result.error


class TestSubagentWorkerRouting:
    async def _run_with_capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        worker: object,
        *,
        first_error: Exception | None = None,
    ) -> dict[str, object]:
        import lauren_ai._agents as agents_module
        from lauren_ai._agents._runner import AgentRunnerBase
        from agenthicc.runners import tool_populator

        captured: dict[str, object] = {}

        def agent_decorator(*, model: str, system: str) -> object:
            captured["model"] = model
            captured["system"] = system
            return lambda cls: cls

        def use_tools(*tools: object) -> object:
            captured["tools"] = tools
            return lambda cls: cls

        async def run(
            _runner: object,
            _agent: object,
            _message: str,
            **kwargs: object,
        ) -> SimpleNamespace:
            captured["memory"] = kwargs["memory"]
            captured["config"] = kwargs["config_override"]
            configs = captured.setdefault("configs", [])
            assert isinstance(configs, list)
            configs.append(kwargs["config_override"])
            if first_error is not None and len(configs) == 1:
                raise first_error
            return SimpleNamespace(content="worker output")

        monkeypatch.setattr(agents_module, "agent", agent_decorator)
        monkeypatch.setattr(agents_module, "use_tools", use_tools)
        monkeypatch.setattr(tool_populator, "populate_agent_tools", lambda *_args: None)
        monkeypatch.setattr(AgentRunnerBase, "run", run)
        assert await worker._execute() == "worker output"  # type: ignore[attr-defined]
        return captured

    async def test_spawn_tool_visibility_tracks_worker_depth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agenthicc.config import SubagentSettings
        from agenthicc.subagents.pool import SubagentWorker
        from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
        from agenthicc.tools.base import tool_name

        spec = SubagentTypeSpec(
            name="delegating",
            allowed_tools=frozenset({"spawn_subagents"}),
            max_turns=1,
            system_prompt="Delegate focused work.",
        )
        registry = SubagentTypeRegistry()
        registry.register(spec)

        async def visible_tool_names(*, depth: int, max_depth: int) -> set[str]:
            worker = SubagentWorker(
                task=SubagentTask("t1", "delegating", "inspect"),
                spec=spec,
                index=1,
                parent_runner=SimpleNamespace(_transport=object()),
                parent_model="model",
                all_tools=[],
                depth=depth,
                pool_id="pool-parent",
                max_depth=max_depth,
                subagent_settings=SubagentSettings(max_depth=max_depth),
                subagent_registry=registry,
            )
            captured = await self._run_with_capture(monkeypatch, worker)
            tools = captured["tools"]
            assert isinstance(tools, tuple)
            return {tool_name(tool) for tool in tools}

        assert "spawn_subagents" in await visible_tool_names(depth=1, max_depth=2)
        assert "spawn_subagents" not in await visible_tool_names(depth=2, max_depth=2)

    async def test_builtin_workers_never_expose_spawn_tool_at_default_depth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agenthicc.subagents.pool import SubagentWorker
        from agenthicc.tools.base import tool_name

        tool_names = set().union(
            *(
                spec.allowed_tools
                for spec in (DEFAULT_REGISTRY.get(name) for name in DEFAULT_REGISTRY.names())
                if spec is not None
            )
        )
        tool_names.add("spawn_subagents")
        all_tools = [SimpleNamespace(name=name) for name in sorted(tool_names)]

        for name in DEFAULT_REGISTRY.names():
            spec = DEFAULT_REGISTRY.get(name)
            assert spec is not None
            worker = SubagentWorker(
                task=SubagentTask(f"task-{name}", name, "inspect"),
                spec=spec,
                index=1,
                parent_runner=SimpleNamespace(_transport=object()),
                parent_model="model",
                all_tools=all_tools,
            )
            captured = await self._run_with_capture(monkeypatch, worker)
            tools = captured["tools"]
            assert isinstance(tools, tuple)
            assert "spawn_subagents" not in {tool_name(tool) for tool in tools}

    async def test_depth_tier_narrows_worker_tools_and_models(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agenthicc.config import SubagentDepthTier, SubagentSettings
        from agenthicc.subagents.pool import SubagentWorker
        from agenthicc.subagents.types import SubagentTypeSpec
        from agenthicc.tools.base import tool_name

        spec = SubagentTypeSpec(
            name="tiered_worker",
            allowed_tools=frozenset({"read_file", "write_file"}),
            max_turns=1,
            system_prompt="Use only tools allowed at this depth.",
            model="inherit",
        )
        settings = SubagentSettings(
            allowed_models=("model-a", "model-b"),
            max_depth=3,
            depth_tiers={
                2: {
                    "tiered_worker": SubagentDepthTier(
                        allowed_tools=("read_file",),
                        allowed_models=("model-a",),
                    )
                }
            },
        )
        worker = SubagentWorker(
            task=SubagentTask("tiered-task", "tiered_worker", "inspect"),
            spec=spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="model-a",
            all_tools=[SimpleNamespace(name="read_file"), SimpleNamespace(name="write_file")],
            depth=2,
            max_depth=3,
            allowed_models=frozenset({"model-a", "model-b"}),
            subagent_settings=settings,
        )

        captured = await self._run_with_capture(monkeypatch, worker)
        tools = captured["tools"]
        assert isinstance(tools, tuple)
        assert {tool_name(tool) for tool in tools} == {"read_file"}
        assert worker._allowed_models == frozenset({"model-a"})

        inherited_outside_tier = SubagentWorker(
            task=SubagentTask("outside-tier", "tiered_worker", "inspect"),
            spec=spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="model-b",
            all_tools=[],
            depth=2,
            max_depth=3,
            allowed_models=frozenset({"model-a", "model-b"}),
            subagent_settings=settings,
        )
        assert "model 'model-b' is not allowed" in inherited_outside_tier._model_error

    async def test_defaults_inherit_parent_model_and_legacy_memory_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lauren_ai._transport import RequestOptions
        from agenthicc.subagents.pool import SubagentWorker

        worker = SubagentWorker(
            task=SubagentTask("t1", "explorer", "inspect"),
            spec=DEFAULT_REGISTRY.get("explorer"),
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="parent-model",
            all_tools=[],
            provider_options={
                "request_options": RequestOptions(
                    provider={"reasoning_effort": "medium", "custom_option": "preserved"}
                )
            },
        )

        captured = await self._run_with_capture(monkeypatch, worker)

        assert captured["model"] == "parent-model"
        assert captured["memory"]._max_tokens == 8_000
        assert captured["config"].memory_window_tokens == 8_000
        assert captured["config"].request_options.provider == {
            "reasoning_effort": "medium",
            "custom_option": "preserved",
        }

    async def test_operator_routing_defaults_and_explicit_model_inherit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lauren_ai._transport import RequestOptions
        from agenthicc.config import ExecutionSettings
        from agenthicc.subagents.pool import SubagentWorker

        settings = ExecutionSettings(context_windows={"routed-model": 12_000})
        spec = SubagentTypeSpec(
            name="operator_defaulted",
            allowed_tools=frozenset(),
            max_turns=1,
            system_prompt="",
        )
        worker = SubagentWorker(
            task=SubagentTask("t1", "operator_defaulted", "inspect"),
            spec=spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="parent-model",
            all_tools=[],
            provider="openai",
            default_model="routed-model",
            default_effort="high",
            default_context=16_000,
            execution_settings=settings,
            provider_options={
                "request_options": RequestOptions(
                    provider={"reasoning_effort": "none", "custom_option": "preserved"}
                )
            },
        )

        captured = await self._run_with_capture(monkeypatch, worker)
        config = captured["config"]
        assert captured["model"] == "routed-model"
        assert captured["memory"]._max_tokens == 12_000
        assert config.context_window == 12_000
        assert config.request_options.provider == {
            "reasoning_effort": "high",
            "custom_option": "preserved",
        }

        inherit_spec = SubagentTypeSpec(
            name="parent_model_worker",
            allowed_tools=frozenset(),
            max_turns=1,
            system_prompt="",
            model="inherit",
        )
        inherit_worker = SubagentWorker(
            task=SubagentTask("t2", "parent_model_worker", "inspect"),
            spec=inherit_spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="parent-model",
            all_tools=[],
            default_model="routed-model",
        )

        inherited = await self._run_with_capture(monkeypatch, inherit_worker)
        assert inherited["model"] == "parent-model"

        call_spec = SubagentTypeSpec(
            name="call_override_worker",
            allowed_tools=frozenset(),
            max_turns=1,
            system_prompt="",
            model="type-model",
            effort="high",
        )
        call_worker = SubagentWorker(
            task=SubagentTask("t3", "call_override_worker", "inspect"),
            spec=call_spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="parent-model",
            all_tools=[],
            call_model="call-model",
            call_effort="low",
        )

        overridden = await self._run_with_capture(monkeypatch, call_worker)
        assert overridden["model"] == "call-model"
        assert overridden["config"].request_options.provider == {"reasoning_effort": "low"}

    async def test_type_model_effort_and_context_are_applied_and_context_is_clamped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lauren_ai._transport import RequestOptions
        from agenthicc.config import ExecutionSettings
        from agenthicc.subagents.pool import SubagentWorker

        spec = SubagentTypeSpec(
            name="routed",
            allowed_tools=frozenset(),
            max_turns=1,
            system_prompt="",
            model="routed-model",
            effort="high",
            context=9_000,
        )
        worker = SubagentWorker(
            task=SubagentTask("t1", "routed", "inspect"),
            spec=spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="parent-model",
            all_tools=[],
            provider="openai",
            execution_settings=ExecutionSettings(context_windows={"routed-model": 5_000}),
            provider_options={
                "request_options": RequestOptions(
                    provider={"reasoning_effort": "none", "custom_option": "preserved"}
                )
            },
        )

        captured = await self._run_with_capture(monkeypatch, worker)
        config = captured["config"]

        assert captured["model"] == "routed-model"
        assert captured["memory"]._max_tokens == 5_000
        assert config.memory_window_tokens == 5_000
        assert config.context_window == 5_000
        assert config.request_options.provider == {
            "reasoning_effort": "high",
            "custom_option": "preserved",
        }

    async def test_unsupported_effort_retries_once_without_only_that_option(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from lauren_ai._transport import RequestOptions
        from agenthicc.subagents.pool import SubagentWorker

        spec = SubagentTypeSpec(
            name="effort_worker",
            allowed_tools=frozenset(),
            max_turns=1,
            system_prompt="",
            effort="high",
        )
        worker = SubagentWorker(
            task=SubagentTask("t1", "effort_worker", "inspect"),
            spec=spec,
            index=1,
            parent_runner=SimpleNamespace(_transport=object()),
            parent_model="parent-model",
            all_tools=[],
            provider_options={
                "request_options": RequestOptions(
                    provider={"reasoning_effort": "none", "custom_option": "preserved"}
                )
            },
        )

        captured = await self._run_with_capture(
            monkeypatch,
            worker,
            first_error=ValueError("reasoning_effort is unsupported"),
        )
        configs = captured["configs"]
        assert isinstance(configs, list)
        first_options = configs[0].request_options.provider
        retry_options = configs[1].request_options.provider

        assert len(configs) == 2
        assert first_options == {"reasoning_effort": "high", "custom_option": "preserved"}
        assert retry_options == {"custom_option": "preserved"}
        assert "does not support reasoning effort 'high'" in caplog.text


# ── spawn_subagents tool validation ──────────────────────────────────────────


class TestSpawnSubagentsTool:
    def _make_tool(self, registry: SubagentTypeRegistry | None = None) -> object:
        from agenthicc.subagents.tool import make_spawn_subagents_tool

        class FakeRunner:
            _transport = None

        return make_spawn_subagents_tool(FakeRunner(), "test-model", [], registry=registry)

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

    async def test_research_is_a_distinct_registered_type(self) -> None:
        from agenthicc.subagents.pool import SubagentPool
        from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec

        registry = SubagentTypeRegistry()
        registry.register(
            SubagentTypeSpec(
                name="research",
                description="Research a focused technical question.",
                allowed_tools=frozenset(),
                max_turns=1,
                system_prompt="Research the requested topic.",
            )
        )
        fn = self._make_tool(registry)
        captured_tasks: list[list[SubagentTask]] = []
        original_init = SubagentPool.__init__

        def capture_init(pool: SubagentPool, *args: object, **kwargs: object) -> None:
            raw_tasks = kwargs.get("tasks", args[0] if args else [])
            captured_tasks.append(raw_tasks if isinstance(raw_tasks, list) else [])
            original_init(pool, *args, **kwargs)

        fake_result = SimpleNamespace(
            pool_id="pool-research",
            total=1,
            succeeded=1,
            failed=0,
            text="research complete",
            values=(),
            budget_exceeded_reason="",
        )
        with (
            patch.object(SubagentPool, "__init__", new=capture_init),
            patch.object(SubagentPool, "run", new=AsyncMock(return_value=fake_result)),
        ):
            result = await fn(tasks=[{"type": "research", "task": "research Chapter 1"}])

        assert result["ok"] is True
        assert result["results"] == "research complete"
        assert captured_tasks[0][0].agent_type == "research"

    def test_tool_schema_advertises_builtin_names_without_research_alias(self) -> None:
        fn = self._make_tool()
        meta = getattr(fn, "__lauren_ai_tool__", None)
        assert meta is not None
        schema = meta.parameters["input_schema"]
        type_schema = schema["properties"]["tasks"]["items"]["properties"]["type"]
        assert "researcher" in type_schema["enum"]
        assert "research" not in type_schema["enum"]

    def test_tool_description_lists_registered_type_descriptions(self) -> None:
        from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec

        registry = SubagentTypeRegistry()
        registry.register(
            SubagentTypeSpec(
                name="focused_reviewer",
                description="Reviews API compatibility risks.",
                allowed_tools=frozenset(),
                max_turns=1,
                system_prompt="Review APIs.",
            )
        )
        fn = self._make_tool(registry)
        meta = getattr(fn, "__lauren_ai_tool__", None)
        assert meta is not None
        schema = meta.parameters["input_schema"]
        type_schema = schema["properties"]["tasks"]["items"]["properties"]["type"]

        assert type_schema["enum"] == ["focused_reviewer"]
        assert "focused_reviewer: Reviews API compatibility risks." in meta.description
        assert (
            "focused_reviewer: Reviews API compatibility risks." in meta.parameters["description"]
        )

    def test_tool_schema_exposes_one_hour_default_timeout(self) -> None:
        fn = self._make_tool()
        meta = getattr(fn, "__lauren_ai_tool__", None)
        assert meta is not None
        timeout_schema = meta.parameters["input_schema"]["properties"]["timeout_s"]
        assert timeout_schema["default"] == 3_600.0

    def test_routing_changes_invalidate_resume_fingerprint(self) -> None:
        tasks = [SubagentTask("t1", "explorer", "inspect")]
        base = _tasks_fingerprint(tasks, routing_policy={"model": "cheap-model"})
        changed = _tasks_fingerprint(tasks, routing_policy={"model": "strong-model"})
        assert base != changed

    async def test_output_schema_changes_the_effective_task_fingerprint(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agenthicc.subagents.pool import SubagentPool
        from agenthicc.subagents.tool import make_spawn_subagents_tool

        fingerprints: list[str] = []
        original_init = SubagentPool.__init__

        def capture_init(pool: SubagentPool, *args: object, **kwargs: object) -> None:
            fingerprint = kwargs.get("task_fingerprint")
            assert isinstance(fingerprint, str)
            fingerprints.append(fingerprint)
            original_init(pool, *args, **kwargs)

        for summary_limit in (100, 200):
            registry = SubagentTypeRegistry()
            registry.register(
                SubagentTypeSpec(
                    "reporter",
                    frozenset(),
                    1,
                    "Return a report.",
                    output_schema={
                        "type": "object",
                        "properties": {"summary": {"type": "string", "maxLength": summary_limit}},
                        "required": ["summary"],
                        "additionalProperties": False,
                    },
                )
            )
            tool_fn = make_spawn_subagents_tool(object(), "test-model", [], registry=registry)
            fake_result = SimpleNamespace(
                pool_id="pool-schema",
                total=1,
                succeeded=1,
                failed=0,
                text="done",
                values=(),
            )
            with (
                patch.object(SubagentPool, "__init__", new=capture_init),
                patch.object(SubagentPool, "run", new=AsyncMock(return_value=fake_result)),
            ):
                result = await tool_fn(tasks=[{"type": "reporter", "task": "report"}])
            assert result["ok"] is True

        assert len(fingerprints) == 2
        assert fingerprints[0] != fingerprints[1]

    def test_call_override_schema_is_hidden_until_operator_enables_it(self) -> None:
        from agenthicc.config import SubagentSettings
        from agenthicc.subagents.tool import make_spawn_subagents_tool

        disabled = make_spawn_subagents_tool(object(), "model", [])
        disabled_schema = disabled.__lauren_ai_tool__.parameters["input_schema"]
        assert "model" not in disabled_schema["properties"]
        assert "effort" not in disabled_schema["properties"]

        enabled = make_spawn_subagents_tool(
            object(),
            "model",
            [],
            subagent_settings=SubagentSettings(allow_call_overrides=True),
        )
        enabled_schema = enabled.__lauren_ai_tool__.parameters["input_schema"]
        assert "model" in enabled_schema["properties"]
        assert enabled_schema["properties"]["effort"]["enum"] == [
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ]

    async def test_per_call_model_override_is_rejected_by_default(self) -> None:
        result = await self._make_tool()(
            tasks=[{"type": "explorer", "task": "inspect"}],
            model="other-model",
        )

        assert result["ok"] is False
        assert "PRD-197" in result["error"]

    async def test_operator_enabled_call_overrides_reach_pool(self) -> None:
        from agenthicc.config import SubagentSettings
        from agenthicc.subagents.pool import SubagentPool
        from agenthicc.subagents.tool import make_spawn_subagents_tool

        captured: dict[str, object] = {}
        original_init = SubagentPool.__init__

        def capture_init(pool: SubagentPool, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            original_init(pool, *args, **kwargs)

        fake_result = SimpleNamespace(
            pool_id="pool-routing",
            total=1,
            succeeded=1,
            failed=0,
            text="routed",
            values=(),
        )
        fn = make_spawn_subagents_tool(
            object(),
            "parent-model",
            [],
            provider="anthropic",
            subagent_settings=SubagentSettings(allow_call_overrides=True),
            allowed_models=frozenset({"anthropic/routed-model"}),
        )
        with (
            patch.object(SubagentPool, "__init__", new=capture_init),
            patch.object(SubagentPool, "run", new=AsyncMock(return_value=fake_result)),
        ):
            result = await fn(
                tasks=[{"type": "explorer", "task": "inspect"}],
                model="anthropic/routed-model",
                effort="high",
            )

        assert result["ok"] is True
        assert captured["call_model"] == "routed-model"
        assert captured["call_effort"] == "high"

    async def test_timeout_override_is_forwarded_to_pool(self) -> None:
        from agenthicc.subagents.pool import SubagentPool

        fn = self._make_tool()
        captured: dict[str, object] = {}
        original_init = SubagentPool.__init__

        def capture_init(pool: SubagentPool, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            original_init(pool, *args, **kwargs)

        fake_result = SimpleNamespace(
            pool_id="pool-timeout",
            total=1,
            succeeded=1,
            failed=0,
            text="done",
            values=(),
            budget_exceeded_reason="",
        )
        with (
            patch.object(SubagentPool, "__init__", new=capture_init),
            patch.object(SubagentPool, "run", new=AsyncMock(return_value=fake_result)),
        ):
            result = await fn(
                tasks=[{"type": "researcher", "task": "research the chapters"}],
                timeout_s=7_200,
            )

        assert result["ok"] is True
        assert captured["timeout_s"] == 7_200

    @pytest.mark.parametrize("timeout_s", [0, -1, float("inf"), float("nan"), True])
    async def test_invalid_timeout_is_rejected(self, timeout_s: object) -> None:
        fn = self._make_tool()
        result = await fn(
            tasks=[{"type": "researcher", "task": "research the chapters"}],
            timeout_s=timeout_s,
        )
        assert result["ok"] is False
        assert "timeout_s" in result["error"]

    def test_tool_schema_uses_dynamic_registry_names(self) -> None:
        from agenthicc.subagents.tool import make_spawn_subagents_tool
        from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec

        registry = SubagentTypeRegistry()
        registry.register(
            SubagentTypeSpec(
                name="security_researcher",
                description="Review security-sensitive changes and report evidence.",
                allowed_tools=frozenset(),
                max_turns=1,
                system_prompt="research",
            )
        )
        fn = make_spawn_subagents_tool(object(), "model", [], registry=registry)
        meta = getattr(fn, "__lauren_ai_tool__", None)
        assert meta is not None
        type_schema = meta.parameters["input_schema"]["properties"]["tasks"]["items"]["properties"][
            "type"
        ]
        assert type_schema["enum"] == ["security_researcher"]
        assert "security_researcher: Review security-sensitive changes and report evidence." in (
            meta.description
        )
        assert "security_researcher" in meta.parameters["description"]

    async def test_executor_role_is_accepted(self) -> None:
        from agenthicc.subagents.tool import make_spawn_subagents_tool

        class FakeRunner:
            _transport = None

        fn = make_spawn_subagents_tool(FakeRunner(), "test-model", [])
        fake_result = SimpleNamespace(
            pool_id="pool-executor",
            total=1,
            succeeded=1,
            failed=0,
            text="compiled",
            values=(),
            budget_exceeded_reason="",
        )
        with patch(
            "agenthicc.subagents.pool.SubagentPool.run",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await fn(tasks=[{"type": "executor", "task": "compile the project"}])

        assert result["ok"] is True
        assert result["succeeded"] == 1
        assert result["results"] == "compiled"

    async def test_failed_pool_is_not_reported_as_success_or_cached(self) -> None:
        from agenthicc.subagents.tool import make_spawn_subagents_tool
        from agenthicc.tui.conversation_store import ConversationStore

        class FakeRunner:
            _transport = None

        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        fn = make_spawn_subagents_tool(FakeRunner(), "test-model", [], conv_store=conv)
        fake_result = SimpleNamespace(
            pool_id="pool-timeout",
            total=1,
            succeeded=0,
            failed=1,
            text="=== executor #1 (✗ timed out after 300s) ===",
            values=(),
            budget_exceeded_reason="",
        )
        with patch(
            "agenthicc.subagents.pool.SubagentPool.run",
            new=AsyncMock(return_value=fake_result),
        ):
            result = await fn(tasks=[{"type": "executor", "task": "compile the project"}])

        assert result["ok"] is False
        assert result["failed"] == 1
        assert "failed" in result["error"]
        assert (
            _find_cached_result(
                conv, _tasks_fingerprint([SubagentTask("t0", "executor", "compile the project")])
            )
            is None
        )

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

    async def test_non_list_tasks_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(tasks=None)  # type: ignore[arg-type]
        assert not result["ok"]
        assert "list" in result["error"]

    async def test_non_integer_concurrency_returns_error(self) -> None:
        fn = self._make_tool()
        result = await fn(
            tasks=[{"type": "explorer", "task": "inspect"}],
            max_concurrent="2",  # type: ignore[arg-type]
        )
        assert not result["ok"]
        assert "max_concurrent" in result["error"]

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
