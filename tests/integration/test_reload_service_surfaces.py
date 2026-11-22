"""Reload the real extension loaders against a temporary project."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest

from agenthicc.commands import build_builtin_registry
from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.runners.reload import ReloadService
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tools.base import tool_name
from agenthicc.workflows.registry import WorkflowRegistry

if TYPE_CHECKING:
    from agenthicc.kernel.processor import EventProcessor

pytestmark = pytest.mark.integration


@pytest.fixture
def reloader(tmp_path: Path, running_processor: EventProcessor) -> ReloadService:
    return ReloadService(
        processor=running_processor,
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "user",
        workflow_registry=WorkflowRegistry(),
        command_registry=build_builtin_registry(),
        subagent_registry=DEFAULT_REGISTRY.copy(),
    )


def _tool_source(name: str) -> str:
    return (
        "from lauren_ai._tools import tool\n"
        "@tool()\n"
        f"def {name}() -> str:\n"
        f'    """Return the tool name."""\n'
        f"    return {name!r}\n"
        f"TOOLS = [{name}]\n"
    )


async def test_tools_publish_healthy_files_and_preserve_failed_surfaces(
    tmp_path: Path, reloader: ReloadService
) -> None:
    plugin_dir = tmp_path / ".agenthicc" / "tools"
    plugin_dir.mkdir(parents=True)
    trust = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=(plugin_dir,))
    for name in ("alpha", "beta"):
        (plugin_dir / f"{name}.py").write_text(_tool_source(name), encoding="utf-8")
    # Loader exception text is deliberately absent from the diagnostic.
    (plugin_dir / "broken.py").write_text(
        'raise RuntimeError("sensitive-value")\n', encoding="utf-8"
    )
    with override_extension_trust(trust):
        report = await reloader.reload(frozenset({"tools"}), reason="command")
        assert report.ok
        assert report.published == frozenset({"tools"})
        assert [item.code for item in report.diagnostics] == ["import_error"]
        assert "sensitive-value" not in str(report.to_dict())
        assert {tool_name(item) for item in reloader.tools.current().value} == {"alpha", "beta"}

        # The healthy skills surface can still publish if tools can no longer load.
        for name in ("alpha", "beta"):
            (plugin_dir / f"{name}.py").write_text("invalid syntax!\n", encoding="utf-8")
        previous = reloader.tools.current()
        partial = await reloader.reload(frozenset({"skills", "tools"}), reason="api")
    assert not partial.ok
    assert partial.published == frozenset({"skills"})
    assert reloader.tools.current() is previous
    await reloader.close()


async def test_skill_reader_sees_one_complete_snapshot_during_reload(
    tmp_path: Path, reloader: ReloadService
) -> None:
    skills_dir = tmp_path / ".agenthicc" / "skills"
    for name in ("alpha", "beta"):
        target = skills_dir / name
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Read {name}\n---\nDo the task.\n",
            encoding="utf-8",
        )
    first = await reloader.reload(frozenset({"skills"}), reason="api")
    original = reloader.skills.current().value
    assert first.ok and set(original) == {"alpha", "beta"}
    target = skills_dir / "gamma"
    target.mkdir()
    (target / "SKILL.md").write_text(
        "---\nname: gamma\ndescription: Read gamma\n---\nDo the task.\n",
        encoding="utf-8",
    )
    ready = Event()
    finished = Event()

    def poll() -> set[frozenset[str]]:
        ready.set()
        observed: set[frozenset[str]] = set()
        for _ in range(10_000):
            observed.add(frozenset(reloader.skills.current().value))
        while not finished.is_set():
            observed.add(frozenset(reloader.skills.current().value))
        observed.add(frozenset(reloader.skills.current().value))
        return observed

    reading = asyncio.create_task(asyncio.to_thread(poll))
    try:
        assert await asyncio.to_thread(ready.wait, 5)
        second = await reloader.reload(frozenset({"skills"}), reason="api")
    finally:
        finished.set()
    assert second.changes[-1].name == "gamma"
    assert await reading == {frozenset({"alpha", "beta"}), frozenset({"alpha", "beta", "gamma"})}
    assert set(original) == {"alpha", "beta"}
    assert isinstance(original, MappingProxyType)
    await reloader.close()


async def test_tool_reload_uses_source_even_when_timestamp_and_size_match(
    tmp_path: Path, reloader: ReloadService
) -> None:
    import os

    plugin = tmp_path / ".agenthicc" / "tools" / "label.py"
    plugin.parent.mkdir(parents=True)
    original = _tool_source("label").replace("'label'", "'first'")
    updated = original.replace("'first'", "'later'")
    assert len(updated) == len(original)
    plugin.write_text(original, encoding="utf-8")
    stamp = plugin.stat()
    trust = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=(plugin.parent,))

    with override_extension_trust(trust):
        assert (await reloader.reload(frozenset({"tools"}), reason="api")).ok
        old = reloader.tools.current()
        assert callable(old.value[0])
        assert old.value[0]() == "first"
        plugin.write_text(updated, encoding="utf-8")
        os.utime(plugin, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        result = await reloader.reload(frozenset({"tools"}), reason="api")

    assert result.ok
    assert result.changes[0].kind == "updated"
    new = reloader.tools.current()
    assert callable(new.value[0])
    assert new.value[0]() == "later"
    assert old.value[0]() == "first"
    await reloader.close()
