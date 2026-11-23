"""Inspect and approve staged authored extensions from the command line."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.cli.context import CLIContext
from agenthicc.plugins.authored import AuthoredArtifactStore, AuthoredError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agenthicc.runners.authored import AuthoredPublisher


@asynccontextmanager
async def _publisher() -> AsyncIterator[AuthoredPublisher]:
    from agenthicc.commands import build_builtin_registry
    from agenthicc.kernel import AppState, EventProcessor
    from agenthicc.runners.authored import AuthoredPublisher
    from agenthicc.runners.reload import ReloadService
    from agenthicc.subagents.types import DEFAULT_REGISTRY
    from agenthicc.workflows.registry import WorkflowRegistry

    workspace = Path.cwd()
    processor = EventProcessor(initial_state=AppState.create(), persist=False)
    reload = ReloadService(
        processor=processor,
        project_dir=workspace / ".agenthicc",
        user_dir=Path.home() / ".agenthicc",
        workflow_registry=WorkflowRegistry(),
        command_registry=build_builtin_registry(),
        subagent_registry=DEFAULT_REGISTRY.copy(),
    )
    task = asyncio.create_task(processor.run(), name="authored-cli-processor")
    try:
        yield AuthoredPublisher(workspace, reload, processor, policy="stage_only")
    finally:
        await reload.close()
        await processor.drain()
        await processor.stop()
        await task


def authored_list(ctx: CLIContext) -> None:
    """List authored extensions and runs awaiting approval."""
    try:
        print(AuthoredArtifactStore(Path.cwd()).describe())
    except AuthoredError as exc:
        raise SystemExit(str(exc)) from exc


async def authored_approve(ctx: CLIContext, run_id: str) -> None:
    """Publish the verified set staged by RUN_ID."""
    try:
        async with _publisher() as publisher:
            result = await publisher.approve(run_id)
    except AuthoredError as exc:
        raise SystemExit(str(exc)) from exc
    if result.error_code is not None:
        raise SystemExit(result.summary())
    print(result.summary())


async def authored_reject(ctx: CLIContext, run_id: str) -> None:
    """Discard the candidate files staged by RUN_ID."""
    try:
        async with _publisher() as publisher:
            result = await publisher.reject(run_id)
    except AuthoredError as exc:
        raise SystemExit(str(exc)) from exc
    print(result.summary())
