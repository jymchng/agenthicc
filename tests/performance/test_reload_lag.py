"""The WFC-05 event-loop budget under real extension discovery."""

from __future__ import annotations

import gc
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.commands import build_builtin_registry
from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
from agenthicc.runners.reload import ReloadService
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tui.loop_lag import LoopLagMonitor
from agenthicc.workflows.registry import WorkflowRegistry

if TYPE_CHECKING:
    from agenthicc.kernel.processor import EventProcessor


async def test_reload_discovering_two_hundred_tools_and_fifty_skills_meets_lag_budget(
    tmp_path: Path, running_processor: EventProcessor
) -> None:
    project = tmp_path / ".agenthicc"
    tool_dir = project / "tools"
    tool_dir.mkdir(parents=True)
    for number in range(200):
        name = f"read_thing_{number}"
        (tool_dir / f"{name}.py").write_text(
            "from lauren_ai._tools import tool\n"
            f"@tool()\ndef {name}() -> int:\n"
            '    """Return the fixture value."""\n'
            f"    return {number}\n"
            f"TOOLS = [{name}]\n",
            encoding="utf-8",
        )
    for number in range(50):
        path = project / "skills" / f"skill-{number}"
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: skill-{number}\ndescription: Test skill\n---\nRead the fixture.\n",
            encoding="utf-8",
        )

    trust = ExtensionTrust(store_path=tmp_path / "trust.json", trusted_roots=(tool_dir,))
    service = ReloadService(
        processor=running_processor,
        project_dir=project,
        user_dir=tmp_path / "user",
        workflow_registry=WorkflowRegistry(),
        command_registry=build_builtin_registry(),
        subagent_registry=DEFAULT_REGISTRY.copy(),
    )
    monitor = LoopLagMonitor(interval_s=0.005, threshold_s=0.05)
    gc.collect()
    gc.freeze()
    try:
        with override_extension_trust(trust):
            monitor.start()
            try:
                report = await service.reload(frozenset({"tools", "skills"}), reason="api")
                await running_processor.drain()
            finally:
                await monitor.stop()
    finally:
        gc.unfreeze()
        await service.close()

    assert report.ok
    assert len(service.tools.current().tools) == 200
    assert len(service.skills.current().value) == 50
    summary = monitor.summary()
    assert summary["samples"] > 0, summary
    assert summary["max_ms"] < 50, summary
    assert summary["p95_ms"] < 10, summary
