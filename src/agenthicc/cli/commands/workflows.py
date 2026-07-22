"""Workflow discovery and headless execution commands."""

from __future__ import annotations

import json as json_module
from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group


@group("workflows", help="Discover and run workflow plugins")
def _() -> None: ...


def _workflow_payload(plugin_cls: type[object], source: str) -> dict[str, object]:
    phases = getattr(plugin_cls, "phases", [])
    phase_payload: list[dict[str, object]] = []
    for phase in phases:
        phase_payload.append(
            {
                "name": str(getattr(phase, "name", "")),
                "agent_type": str(getattr(phase, "agent_type", "auto")),
                "next": getattr(phase, "next", None),
                "on_reject": getattr(phase, "on_reject", None),
                "parallel_with": list(getattr(phase, "parallel_with", ())),
            }
        )
    return {
        "name": str(getattr(plugin_cls, "name", "")),
        "description": str(getattr(plugin_cls, "description", "")),
        "source": source,
        "mode_bindings": list(getattr(plugin_cls, "mode_bindings", [])),
        "phases": phase_payload,
    }


def _workflow_registry():
    from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415

    return build_workflow_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )


@command("workflows", "list", help="List available workflow plugins")
def workflows_list(ctx: CLIContext, json: bool = False) -> None:
    """List built-in, user, and project workflows with their phase topology."""
    registry = _workflow_registry()
    payload = [
        _workflow_payload(plugin_cls, registry.get_entry(plugin_cls.name).source)  # type: ignore[union-attr]
        for plugin_cls in sorted(registry.all(), key=lambda item: item.name)
    ]
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload:
        print("No workflows found.")
        return
    for workflow in payload:
        phases = (
            " → ".join(
                str(phase["name"])
                for phase in workflow["phases"]  # type: ignore[index]
            )
            or "(no phases)"
        )
        bindings = ", ".join(str(item) for item in workflow["mode_bindings"])  # type: ignore[index]
        print(
            f"{workflow['name']} [{workflow['source']}] — "
            f"{workflow['description'] or 'no description'}"
        )
        print(f"  phases: {phases}")
        print(f"  modes: {bindings or 'manual'}")


@command("workflows", "run", help="Run a workflow headlessly for one intent")
async def workflows_run(
    ctx: CLIContext,
    workflow_name: str,
    intent: str = "",
    json: bool = False,
) -> None:
    """Run WORKFLOW_NAME with --intent TEXT and report its durable outcome."""
    from agenthicc.runners.headless import run_headless_workflow  # noqa: PLC0415

    try:
        result = await run_headless_workflow(ctx, workflow_name, intent)
    except (RuntimeError, ValueError) as exc:
        if json:
            print(json_module.dumps({"status": "failed", "error": str(exc)}))
        else:
            print(f"Workflow failed: {exc}")
        return

    if json:
        print(json_module.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    print(
        f"Workflow {result.workflow_name} {result.status} "
        f"(session {result.session_id}, run {result.run_id or 'unknown'})"
    )
    if result.phases:
        print(f"Phases: {' → '.join(result.phases)}")
    if result.error:
        print(f"Error: {result.error}")
