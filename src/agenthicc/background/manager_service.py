"""Bounded asynchronous services used by the background-session TUI."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable

from .deletion import DeleteResult
from .input_inbox import BackgroundInput
from .model import BackgroundSession
from .store import BackgroundPage, BackgroundStore
from .supervisor import BackgroundSupervisor


def _redacted_error(exc: Exception) -> str:
    """Return a bounded operation error with credential-shaped values masked."""

    from agenthicc.tui.runtime.session_export import _Redactor  # noqa: PLC0415

    value = _Redactor().value(f"{type(exc).__name__}: {exc}", "message")
    return str(value)[:2_000]


@dataclass(frozen=True)
class ManagerOperationResult:
    """Structured completion state for an asynchronous manager operation."""

    operation_id: str
    target_id: str | None
    phase: str
    ok: bool
    category: str = ""
    message: str = ""
    value: object | None = None


class BackgroundManagerService:
    """Serialize blocking registry/process work behind one bounded adapter.

    The service owns its executor and every submitted operation is awaited by
    its caller.  It is deliberately client-neutral: the CLI can continue to
    use synchronous store/supervisor APIs while the interactive manager uses
    this boundary to keep terminal input and Rich rendering on the event loop.
    """

    def __init__(
        self,
        store: BackgroundStore,
        supervisor: BackgroundSupervisor,
        *,
        max_workers: int = 4,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.store = store
        self.supervisor = supervisor
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="agenthicc-background-manager",
        )
        self._closed = False

    async def _run_blocking(
        self,
        function: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        if self._closed:
            raise RuntimeError("background manager service is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: function(*args, **kwargs))

    async def refresh_page_async(self, **kwargs: object) -> BackgroundPage:
        value = await self._run_blocking(self.store.query_page, **kwargs)
        if not isinstance(value, BackgroundPage):
            raise TypeError("background store returned an invalid page")
        return value

    async def run_blocking(
        self,
        function: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        """Run an injected compatibility callback on the bounded adapter."""

        return await self._run_blocking(function, *args, **kwargs)

    async def maintain_async(self, *, force: bool = False) -> list[BackgroundSession]:
        del force  # cadence and single-flight ownership belong to the manager.
        recover: Callable[[], list[BackgroundSession]]
        try:
            recover = self.supervisor.recover_stale_batch
        except AttributeError:
            recover = self.supervisor.recover_stale
        value = await self._run_blocking(recover)
        return list(value) if isinstance(value, list) else []

    async def _invoke(
        self,
        target_id: str | None,
        method: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> ManagerOperationResult:
        operation_id = uuid.uuid4().hex
        try:
            value = await self._run_blocking(method, *args, **kwargs)
            return ManagerOperationResult(
                operation_id=operation_id,
                target_id=target_id,
                phase="completed",
                ok=True,
                value=value,
            )
        except Exception as exc:  # noqa: BLE001
            return ManagerOperationResult(
                operation_id=operation_id,
                target_id=target_id,
                phase="failed",
                ok=False,
                category=type(exc).__name__,
                message=_redacted_error(exc),
            )

    async def cancel_async(self, session_id: str) -> ManagerOperationResult:
        return await self._invoke(session_id, self.supervisor.cancel, session_id)

    async def archive_async(self, session_id: str) -> ManagerOperationResult:
        return await self._invoke(session_id, self.supervisor.archive, session_id)

    async def restore_async(self, session_id: str) -> ManagerOperationResult:
        return await self._invoke(session_id, self.supervisor.restore_deleted, session_id)

    async def approve_async(self, session_id: str, decision: bool) -> ManagerOperationResult:
        return await self._invoke(session_id, self.supervisor.approve, session_id, decision)

    async def provide_input_async(self, session_id: str, value: str) -> ManagerOperationResult:
        return await self._invoke(session_id, self.supervisor.provide_input, session_id, value)

    async def enqueue_input_async(
        self,
        session_id: str,
        text: str,
        *,
        owner_attempt: int,
        lease_token: str,
        message_id: str,
    ) -> ManagerOperationResult:
        result = await self._invoke(
            session_id,
            self.supervisor.enqueue_input,
            session_id,
            text,
            owner_attempt=owner_attempt,
            lease_token=lease_token,
            message_id=message_id,
        )
        if result.ok and not isinstance(result.value, BackgroundInput):
            return ManagerOperationResult(
                operation_id=result.operation_id,
                target_id=session_id,
                phase="failed",
                ok=False,
                category="invalid_receipt",
                message="Background input service returned an invalid receipt",
            )
        return result

    async def pin_async(self, session_id: str, pinned: bool) -> ManagerOperationResult:
        operation_id = uuid.uuid4().hex
        try:
            updated = await self._run_blocking(self.store.update, session_id, pinned=pinned)
            return ManagerOperationResult(
                operation_id=operation_id,
                target_id=session_id,
                phase="completed",
                ok=True,
                value=updated,
            )
        except Exception as exc:  # noqa: BLE001
            return ManagerOperationResult(
                operation_id=operation_id,
                target_id=session_id,
                phase="failed",
                ok=False,
                category=type(exc).__name__,
                message=_redacted_error(exc),
            )

    async def attach_prepare_async(self, session_id: str) -> ManagerOperationResult:
        """Validate one exact attach target without handing it off yet."""

        operation_id = uuid.uuid4().hex
        try:
            value = await self._run_blocking(self.store.get, session_id, include_deleted=True)
            if not isinstance(value, BackgroundSession):
                raise TypeError("background store returned an invalid session")
            if value.status.value == "deleted":
                raise ValueError("deleted background sessions cannot be attached")
            return ManagerOperationResult(
                operation_id=operation_id,
                target_id=session_id,
                phase="validated",
                ok=True,
                value=value,
            )
        except Exception as exc:  # noqa: BLE001
            return ManagerOperationResult(
                operation_id=operation_id,
                target_id=session_id,
                phase="failed",
                ok=False,
                category=type(exc).__name__,
                message=_redacted_error(exc),
            )

    async def delete_async(
        self,
        session_ids: tuple[str, ...],
        *,
        operation_id: str,
        requested_by: str = "agents",
        progress: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> DeleteResult:
        try:
            delete_async = self.supervisor.delete_async
        except AttributeError:
            delete_async = None
        if callable(delete_async):
            value = delete_async(
                session_ids,
                operation_id=operation_id,
                requested_by=requested_by,
                progress=progress,
            )
            if inspect.isawaitable(value):
                result = await value
            else:
                result = value
            if isinstance(result, DeleteResult):
                return result

        # Legacy/custom supervisors are isolated from the event loop as one
        # bounded operation and retain the existing exact-target contract.
        def fallback() -> DeleteResult:
            deleted: list[str] = []
            failures = []
            for session_id in session_ids:
                try:
                    self.supervisor.delete(session_id)
                except Exception as exc:  # noqa: BLE001
                    from .deletion import DeleteFailure  # noqa: PLC0415

                    failures.append(
                        DeleteFailure(
                            session_id=session_id,
                            code="delete_failed",
                            message=f"{type(exc).__name__}: {exc}",
                        )
                    )
                else:
                    deleted.append(session_id)
            return DeleteResult(
                operation_id=operation_id,
                deleted=tuple(deleted),
                failures=tuple(failures),
            )

        fallback_result = await self._run_blocking(fallback)
        assert isinstance(fallback_result, DeleteResult)
        return fallback_result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
