"""CLI inspection and recovery commands for parallel worker orchestration."""

from __future__ import annotations

import json as json_module

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group
from agenthicc.worktrees import (
    ManifestStore,
    ParallelCoordinator,
    ParallelManifest,
    WorktreeManager,
)


@group("worktrees", help="Inspect and recover parallel worker orchestration")
def _worktrees_group() -> None: ...


def _store(ctx: CLIContext) -> ManifestStore:
    del ctx
    return ManifestStore()


def _payload(manifest: ParallelManifest) -> dict[str, object]:
    return manifest.to_dict()


@command("worktrees", "list", help="List durable parallel orchestrations")
def worktrees_list(ctx: CLIContext, json: bool = False) -> None:
    manifests = _store(ctx).list()
    payload = [_payload(item) for item in manifests]
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload:
        print("No parallel orchestrations.")
        return
    for item in payload:
        tasks = item.get("tasks", [])
        count = len(tasks) if isinstance(tasks, list) else 0
        print(
            f"{str(item.get('orchestration_id', ''))[:16]}  "
            f"{str(item.get('status', 'unknown')):12}  "
            f"{count} task(s)  parent={item.get('parent_session_id', '')}"
        )


@command("worktrees", "show", help="Show one orchestration manifest")
def worktrees_show(ctx: CLIContext, orchestration_id: str, json: bool = False) -> None:
    try:
        manifest = _store(ctx).get(orchestration_id)
    except KeyError:
        print(f"Orchestration not found: {orchestration_id}")
        raise SystemExit(1) from None
    payload = _payload(manifest)
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json_module.dumps(payload, indent=2, sort_keys=True))


@command("worktrees", "recover", help="Reconcile one orchestration with Git worktrees")
def worktrees_recover(ctx: CLIContext, orchestration_id: str, repository: str = ".") -> None:
    try:
        manifest = _store(ctx).get(orchestration_id)
        recovered = ParallelCoordinator(
            repository,
            parent_session_id=manifest.parent_session_id,
            store=_store(ctx),
            manager=WorktreeManager(repository),
        ).recover(orchestration_id)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to recover orchestration: {exc}")
        raise SystemExit(1) from exc
    print(json_module.dumps(_payload(recovered), indent=2, sort_keys=True))


@command("worktrees", "integrate", help="Integrate a completed worker task")
def worktrees_integrate(
    ctx: CLIContext,
    orchestration_id: str,
    task_id: str,
    repository: str = ".",
) -> None:
    try:
        manifest = _store(ctx).get(orchestration_id)
        result = ParallelCoordinator(
            repository,
            parent_session_id=manifest.parent_session_id,
            store=_store(ctx),
            manager=WorktreeManager(repository),
        ).integrate_task(orchestration_id, task_id)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to integrate task: {exc}")
        raise SystemExit(1) from exc
    print(json_module.dumps(result.__dict__, indent=2, sort_keys=True))
