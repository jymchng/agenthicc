"""Publish runner-approved artifacts through the active session surfaces."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agenthicc.kernel.events import Event
from agenthicc.plugins.authored import AuthoredArtifactStore, AuthoredError
from agenthicc.plugins.trust import inspect_pending_publication, override_extension_trust

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.plugins.authored import _PUBLISH_APPROVAL
    from agenthicc.plugins.authored_types import (
        ArtifactKind,
        ArtifactState,
        AuthoredPolicy,
        AuthoredRun,
        PublishedArtifact,
    )
    from agenthicc.runners.reload import ReloadReport, ReloadService, ReloadSurface


_SURFACES: dict[ArtifactKind, ReloadSurface] = {
    "command": "commands",
    "skill": "skills",
    "tool": "tools",
    "workflow": "workflows",
}


@dataclass(frozen=True, slots=True)
class AuthoredPublication:
    """The outcome of the session publish step for one run."""

    run_id: str
    artifacts: tuple[PublishedArtifact, ...]
    reload_report: ReloadReport | None = None
    error_code: str | None = None
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "reload": self.reload_report.to_dict() if self.reload_report is not None else None,
            "error_code": self.error_code,
            "diagnostic": self.diagnostic,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if self.diagnostic is not None:
            return self.diagnostic
        lines: list[str] = []
        for artifact in self.artifacts:
            label = f"{artifact.kind} {artifact.name} · {artifact.digest[:12]} · {artifact.state}"
            if artifact.validation == "static_only":
                label += " · static_only"
            details = (
                ("capabilities", artifact.declared_capabilities),
                ("hosts", artifact.network_hosts),
                ("advisories", artifact.advisories),
            )
            for title, values in details:
                if values:
                    label += f" · {title} {', '.join(values)}"
            if artifact.state == "loaded":
                label += f" · /authored rollback {artifact.name}"
            lines.append(label)
        return "\n".join(lines)


def _verify_loaded(
    report: ReloadReport, workspace: Path, artifacts: tuple[PublishedArtifact, ...]
) -> None:
    offered = {(item.surface, item.name) for item in report.changes if item.kind != "removed"}
    for artifact in artifacts:
        surface = _SURFACES[artifact.kind]
        name = f"/{artifact.name}" if artifact.kind == "command" else artifact.name
        paths = tuple(workspace / relative for relative in artifact.files)
        for item in report.diagnostics:
            if item.surface != surface:
                continue
            source = Path(item.path)
            if any(source == path or path.is_relative_to(source) for path in paths):
                raise AuthoredError(item.code, f"{artifact.name}: {item.message}")
        if surface not in report.published or (surface, name) not in offered:
            raise AuthoredError("load_failed", f"Reload did not register {artifact.name}.")


def _verify_removed(report: ReloadReport, artifacts: tuple[PublishedArtifact, ...]) -> None:
    offered = {(item.surface, item.name) for item in report.changes if item.kind != "removed"}
    for artifact in artifacts:
        name = f"/{artifact.name}" if artifact.kind == "command" else artifact.name
        if not report.ok or (_SURFACES[artifact.kind], name) in offered:
            raise AuthoredError("load_failed", f"Reload could not remove {artifact.name}.")


class _ReloadFailed(AuthoredError):
    def __init__(self, error: AuthoredError, report: ReloadReport) -> None:
        super().__init__(error.code, str(error))
        self.report = report


class AuthoredPublisher:
    """Own a session's stage, publish, reload, and notification sequence."""

    def __init__(
        self,
        workspace: Path,
        reload_service: ReloadService,
        processor: EventProcessor,
    ) -> None:
        self._workspace = workspace
        self._reload = reload_service
        self._processor = processor
        self._lock = asyncio.Lock()

    def store(self) -> AuthoredArtifactStore:
        """Return the operator store for this workspace on demand."""
        return AuthoredArtifactStore(self._workspace)

    async def publish(
        self,
        run: AuthoredRun,
        *,
        policy: AuthoredPolicy,
        source_root: Path | None = None,
    ) -> AuthoredPublication:
        """Apply the configured policy and wait for the final publication state."""
        task = asyncio.create_task(
            self._publish(run, policy=policy, source_root=source_root, approved_by="policy"),
            name="publish-authored",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def approve(self, run_id: str) -> AuthoredPublication:
        run = await asyncio.to_thread(self.store().staged, run_id)
        task = asyncio.create_task(
            self._publish(run, policy="auto", source_root=None, approved_by="user"),
            name="approve-authored",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def reject(self, run_id: str) -> AuthoredPublication:
        async with self._lock:
            run = await asyncio.to_thread(self.store().reject_staged, run_id)
            result = AuthoredPublication(
                run_id, tuple(replace(item.artifact, state="rejected") for item in run.candidates)
            )
            self._notify(result)
            return result

    async def change_version(
        self, action: Literal["rollback", "remove"], name: str, digest: str | None = None
    ) -> str:
        """Apply an operator version decision and reload its surface."""
        task = asyncio.create_task(
            self._change_version(action, name, digest), name=f"authored-{action}"
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _change_version(
        self, action: Literal["rollback", "remove"], name: str, digest: str | None
    ) -> str:
        async with self._lock:
            store = self.store()
            prior = await asyncio.to_thread(store.record, name)
            mutation = store.rollback(name, digest) if action == "rollback" else store.remove(name)
            installed, report = await self._reload_transaction(
                store,
                mutation,
                frozenset({_SURFACES[prior.artifact.kind]}),
                removing=action == "remove",
            )
            for artifact in installed:
                self._processor.emit_nowait(
                    Event.create(
                        "AuthoredArtifactStateChanged",
                        {
                            "run_id": prior.run_id,
                            "artifact": artifact.to_dict(),
                            "operation": action,
                        },
                    )
                )
            changed = ", ".join(item.name for item in installed)
            verb = "Restored" if action == "rollback" else "Removed"
            return f"{verb} {changed}. {report.summary()}"

    async def _publish(
        self,
        run: AuthoredRun,
        *,
        policy: AuthoredPolicy,
        source_root: Path | None,
        approved_by: _PUBLISH_APPROVAL,
    ) -> AuthoredPublication:
        async with self._lock:
            try:
                store = self.store()
                if policy == "stage_only":
                    staged = await asyncio.to_thread(store.stage, run, source_root=source_root)
                    result = AuthoredPublication(run.run_id, staged)
                else:
                    result = await self._commit(
                        store, run, source_root=source_root, approved_by=approved_by
                    )
            except AuthoredError as exc:
                result = _failed(run, exc.code, str(exc), "rejected")
            except (OSError, ValueError):
                result = _failed(run, "publish_failed", "Authored publication failed.", "rejected")
            self._notify(result)
            return result

    async def _commit(
        self,
        store: AuthoredArtifactStore,
        run: AuthoredRun,
        *,
        source_root: Path | None,
        approved_by: _PUBLISH_APPROVAL,
    ) -> AuthoredPublication:
        try:
            installed, report = await self._reload_transaction(
                store,
                store.commit(run, source_root=source_root, approved_by=approved_by),
                frozenset(_SURFACES[item.artifact.kind] for item in run.candidates),
            )
        except _ReloadFailed as exc:
            failed = _failed(run, exc.code, str(exc), "load_failed")
            return replace(failed, reload_report=exc.report)
        return AuthoredPublication(run.run_id, installed, report)

    async def _reload_transaction(
        self,
        store: AuthoredArtifactStore,
        mutation: AbstractContextManager[tuple[PublishedArtifact, ...]],
        surfaces: frozenset[ReloadSurface],
        *,
        removing: bool = False,
    ) -> tuple[tuple[PublishedArtifact, ...], ReloadReport]:
        stack = ExitStack()
        installed: tuple[PublishedArtifact, ...] = ()

        async def prepare() -> None:
            nonlocal installed
            installed = await asyncio.to_thread(stack.enter_context, mutation)

        async def verify(proposed: ReloadReport) -> bool:
            try:
                if removing:
                    _verify_removed(proposed, installed)
                else:
                    _verify_loaded(proposed, store.workspace, installed)
            except AuthoredError as exc:
                await asyncio.to_thread(stack.__exit__, type(exc), exc, exc.__traceback__)
                refused = replace(
                    proposed, published=frozenset(), generation=self._reload.generation
                )
                raise _ReloadFailed(exc, refused) from exc
            await asyncio.to_thread(stack.close)
            return True

        with override_extension_trust(store.trust), inspect_pending_publication(store.workspace):
            try:
                report = await self._reload.reload(
                    surfaces,
                    reason="workflow_return",
                    prepare=prepare,
                    before_publish=verify,
                )
            except BaseException as exc:
                await asyncio.to_thread(stack.__exit__, type(exc), exc, exc.__traceback__)
                raise
        return installed, report

    def _notify(self, result: AuthoredPublication) -> None:
        for artifact in result.artifacts:
            self._processor.emit_nowait(
                Event.create(
                    "AuthoredArtifactStateChanged",
                    {"run_id": result.run_id, "artifact": artifact.to_dict()},
                )
            )
        if result.artifacts and all(item.state == "loaded" for item in result.artifacts):
            self._processor.emit_nowait(Event.create("AuthoredPublished", result.to_dict()))


def _failed(
    run: AuthoredRun, code: str, diagnostic: str, state: ArtifactState
) -> AuthoredPublication:
    return AuthoredPublication(
        run.run_id,
        tuple(replace(item.artifact, state=state) for item in run.candidates),
        error_code=code,
        diagnostic=diagnostic,
    )
