"""CLI inspection and recovery commands for parallel worker orchestration."""

from __future__ import annotations

import json as json_module

from agenthicc.background.model import BackgroundSession
from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import optional_positionals
from agenthicc.worktrees import (
    ManifestStore,
    ParallelCoordinator,
    ParallelManifest,
    WorktreeManager,
)
from agenthicc.worktrees.environment import (
    EnvironmentTrustRequired,
    EnvironmentTrustStore,
    WorktreeEnvironment,
)
from agenthicc.worktrees.environment_actions import (
    available_session_actions,
    run_session_action,
    run_session_setup,
)


def _store(ctx: CLIContext) -> ManifestStore:
    del ctx
    return ManifestStore()


def _payload(manifest: ParallelManifest) -> dict[str, object]:
    return manifest.to_dict()


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


def worktrees_dispatch(
    ctx: CLIContext,
    orchestration_id: str,
    task_id: str,
    approve: bool = False,
    json: bool = False,
) -> None:
    store = _store(ctx)
    try:
        manifest = store.get(orchestration_id)
    except KeyError:
        print(f"Orchestration not found: {orchestration_id}")
        raise SystemExit(1) from None
    from agenthicc.cli.commands.background import _store_and_supervisor

    _, supervisor = _store_and_supervisor(ctx)
    coordinator = ParallelCoordinator(
        manifest.repository,
        parent_session_id=manifest.parent_session_id,
        run_id=manifest.run_id,
        store=store,
        manager=WorktreeManager(manifest.repository),
        supervisor=supervisor,
        worker_workflow_name="",
    )
    try:
        task = coordinator.dispatch(orchestration_id, task_id, environment_approved=approve)
    except EnvironmentTrustRequired as exc:
        print(str(exc))
        print("Review the command above, then re-run with --approve for one-time setup approval.")
        raise SystemExit(2) from exc
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to dispatch task: {exc}")
        raise SystemExit(1) from exc
    payload = task.to_dict()
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Task {task.task_id}: {task.status.value} · session {task.worker_session_id}")


def _worker_session(ctx: CLIContext, session_id: str) -> BackgroundSession:
    from agenthicc.cli.commands.background import _store_and_supervisor

    store, _ = _store_and_supervisor(ctx)
    try:
        return store.get(session_id, include_deleted=True)
    except KeyError:
        print(f"Worker session not found: {session_id}")
        raise SystemExit(1) from None


def worktrees_actions(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    session = _worker_session(ctx, session_id)
    try:
        actions = available_session_actions(session)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to load worker environment actions: {exc}")
        raise SystemExit(1) from exc
    payload = [
        {
            "id": item.command_id,
            "command": item.command,
            "cwd": item.cwd,
            "timeout_s": item.timeout_s,
            "network": item.network,
        }
        for item in actions
    ]
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    elif not actions:
        print("No named actions are declared for this worker.")
    else:
        for item in actions:
            print(
                f"{item.command_id}: {item.command}\n"
                f"  cwd={item.cwd} network={item.network} timeout={item.timeout_s:g}s"
            )


def worktrees_action(
    ctx: CLIContext,
    session_id: str,
    action_id: str,
    approve: bool = False,
    json: bool = False,
) -> None:
    _worker_session(ctx, session_id)
    from agenthicc.cli.commands.background import _store_and_supervisor

    sessions, _ = _store_and_supervisor(ctx)
    try:
        receipt = run_session_action(
            session_id,
            action_id,
            approved=approve,
            sessions=sessions,
        )
    except EnvironmentTrustRequired as exc:
        print(str(exc))
        print("Review the command above, then re-run with --approve for one-time approval.")
        raise SystemExit(2) from exc
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to run worker action: {exc}")
        raise SystemExit(1) from exc
    payload = receipt.to_dict()
    if json:
        payload["commands"] = [
            {
                **item.to_dict(),
                "stdout": item.stdout,
                "stderr": item.stderr,
            }
            for item in receipt.commands
        ]
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"{receipt.kind}: {receipt.result}")
    for item in receipt.commands:
        print(f"{item.command_id}: {item.result} (exit {item.exit_code})")
        if item.error:
            print(f"  {item.error}")
        if item.stdout:
            print(item.stdout, end="" if item.stdout.endswith("\n") else "\n")
        if item.stderr:
            print(item.stderr, end="" if item.stderr.endswith("\n") else "\n")
        if item.truncated:
            print("  [captured output truncated]")


def worktrees_setup(
    ctx: CLIContext,
    session_id: str,
    approve: bool = False,
    json: bool = False,
) -> None:
    _worker_session(ctx, session_id)
    from agenthicc.cli.commands.background import _store_and_supervisor

    sessions, _ = _store_and_supervisor(ctx)
    try:
        receipt = run_session_setup(
            session_id,
            approved=approve,
            sessions=sessions,
        )
    except EnvironmentTrustRequired as exc:
        print(str(exc))
        print("Review the command above, then re-run with --approve for one-time approval.")
        raise SystemExit(2) from exc
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"Unable to run worker setup: {exc}")
        raise SystemExit(1) from exc
    if receipt is None:
        print("No setup commands are declared for this worker.")
        return
    payload = receipt.to_dict()
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"setup: {receipt.result}")
        for item in receipt.commands:
            print(f"{item.command_id}: {item.result} (exit {item.exit_code})")
            if item.error:
                print(f"  {item.error}")
            if item.stdout:
                print(item.stdout, end="" if item.stdout.endswith("\n") else "\n")
            if item.stderr:
                print(item.stderr, end="" if item.stderr.endswith("\n") else "\n")
            if item.truncated:
                print("  [captured output truncated]")


@optional_positionals("repository")
def worktrees_trust(ctx: CLIContext, repository: str = ".", confirm: bool = False) -> None:
    del ctx
    environment = WorktreeEnvironment(repository, "trust-preview")
    manifest = environment.load()
    if manifest is None:
        print("No .agenthicc/environment.toml manifest found.")
        raise SystemExit(1)
    print("\n".join(manifest.preview()))
    if not confirm:
        print("No trust was recorded. Review the manifest, then repeat with --confirm.")
        return
    EnvironmentTrustStore().trust(manifest)
    print("Trusted this exact manifest digest for this Git repository.")


def agents_actions(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    worktrees_actions(ctx, session_id, json=json)


def agents_action(
    ctx: CLIContext,
    session_id: str,
    action_id: str,
    approve: bool = False,
    json: bool = False,
) -> None:
    worktrees_action(ctx, session_id, action_id, approve=approve, json=json)
