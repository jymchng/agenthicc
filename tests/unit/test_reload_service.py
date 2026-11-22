"""Session reload reports, serialization, and concurrent request behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

import pytest

from agenthicc.commands import build_builtin_registry
from agenthicc.runners.reload import ReloadService
from agenthicc.skills.loader import SkillDiscoveryResult
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.workflows.registry import WorkflowRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agenthicc.commands.command import Command
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.runners.reload import ReloadSurface, _Candidate
    from agenthicc.skills.loader import SkillDef

pytestmark = pytest.mark.unit


@pytest.fixture
def reloader(tmp_path: Path, running_processor: EventProcessor) -> ReloadService:
    return ReloadService(
        processor=running_processor,
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "user",
        workflow_registry=WorkflowRegistry(),
        command_registry=build_builtin_registry(),
        subagent_registry=DEFAULT_REGISTRY.copy(),
    )


async def test_preview_leaves_generation_surfaces_and_events_unchanged(
    reloader: ReloadService, running_processor: EventProcessor
) -> None:
    events = running_processor.subscribe_events()
    old = reloader.skills.current()

    report = await reloader.preview(frozenset({"skills"}))
    await running_processor.drain()

    assert report.requested == frozenset({"skills"})
    assert report.published == frozenset()
    assert report.generation == reloader.generation == 0
    assert reloader.skills.current() is old
    assert events.empty()
    await reloader.close()


async def test_covered_reload_calls_share_one_discovery_and_report(
    reloader: ReloadService, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = Event()
    release = Event()
    calls: list[str] = []

    def discover(*, project_dir: Path, user_dir: Path) -> SkillDiscoveryResult:
        calls.append("skills")
        started.set()
        assert release.wait(5)
        return SkillDiscoveryResult({})

    monkeypatch.setattr("agenthicc.skills.loader.discover_skills_with_diagnostics", discover)
    first = asyncio.create_task(reloader.reload(frozenset({"skills", "commands"}), reason="api"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        # The first request is in the discovery thread. Enter the second
        # request before allowing that thread to finish.
        second = asyncio.create_task(reloader.reload(frozenset({"skills"}), reason="command"))
        await asyncio.sleep(0)
    finally:
        release.set()
    result, joined = await asyncio.gather(first, second)

    assert result is joined
    assert calls == ["skills"]
    assert result.generation == 1
    assert result.published == frozenset({"skills", "commands"})
    assert result.ok
    await reloader.close()


async def test_reload_order_and_replay_generations(
    reloader: ReloadService,
    running_processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agenthicc.kernel.events import Event as KernelEvent
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.kernel.reducer import root_reducer
    from agenthicc.kernel.state import AppState

    observed_order: list[str] = []
    discover = reloader._discover

    def observe(
        surface: ReloadSurface,
        commands: tuple[Command, ...],
        skills: Mapping[str, SkillDef],
        staged_skills: Mapping[str, SkillDef] | None,
    ) -> _Candidate:
        observed_order.append(surface)
        return discover(surface, commands, skills, staged_skills)

    monkeypatch.setattr(reloader, "_discover", observe)
    events = running_processor.subscribe_events()
    await reloader.reload(frozenset({"commands", "skills", "workflows", "tools"}), reason="api")
    await reloader.reload(frozenset({"skills"}), reason="api")
    await running_processor.drain()

    assert observed_order == ["skills", "tools", "workflows", "commands", "skills"]
    received = [events.get_nowait(), events.get_nowait()]
    encoded = [json.dumps(event.to_dict()) for event in received]
    log = tmp_path / "reload.jsonl"
    log.write_text("\n".join(encoded) + "\n", encoding="utf-8")
    initial = AppState.create()
    state = initial
    for generation, item in enumerate(encoded, 1):
        state, effects = root_reducer(state, KernelEvent.from_dict(json.loads(item)))
        assert state.reload_generation == generation
        assert effects == []
    assert (await restore_from_log(str(log), initial)).reload_generation == 2
    assert events.empty()
    await reloader.close()


async def test_cancelled_preview_keeps_discovery_exclusive_until_the_thread_returns(
    reloader: ReloadService, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_seen = Event()
    finish_first = Event()
    calls: list[int] = []

    def discover(*, project_dir: Path, user_dir: Path) -> SkillDiscoveryResult:
        calls.append(len(calls) + 1)
        if calls == [1]:
            first_seen.set()
            assert finish_first.wait(5)
        return SkillDiscoveryResult({})

    monkeypatch.setattr("agenthicc.skills.loader.discover_skills_with_diagnostics", discover)
    preview = asyncio.create_task(reloader.preview(frozenset({"skills"})))
    try:
        assert await asyncio.to_thread(first_seen.wait, 5)
        preview.cancel()
        following = asyncio.create_task(reloader.reload(frozenset({"skills"}), reason="api"))
        await asyncio.sleep(0)
        assert calls == [1]
    finally:
        finish_first.set()
    with pytest.raises(asyncio.CancelledError):
        await preview
    assert (await following).generation == 1
    assert calls == [1, 2]
    await reloader.close()


def test_reload_migration_has_no_previous_callback_names() -> None:
    root = Path(__file__).resolve().parents[2]
    surfaces = ("skills", "tools", "workflows", "commands")
    removed = tuple(f"_reload_{surface}" for surface in surfaces) + tuple(
        f"reload_{surface}=" for surface in surfaces
    )
    found: list[str] = []
    for directory in ("src", "tests", "docs"):
        for source in (root / directory).rglob("*"):
            if source.suffix not in {".md", ".py", ".txt"} or not source.is_file():
                continue
            if any(name in source.read_text(encoding="utf-8") for name in removed):
                found.append(source.relative_to(root).as_posix())
    assert found == []
