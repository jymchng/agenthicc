"""Terminal view and controller lifetime for a hosted TUI runtime."""

from __future__ import annotations

import asyncio
import contextvars
import io
import queue
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agenthicc.background.model import BackgroundSession, LaunchSpec, SessionStatus
from agenthicc.background.queue import current_owner_id
from agenthicc.background.store import InvalidSessionTransition
from agenthicc.background.supervisor import LiveControllerInputPending, QueueFullError
from agenthicc.tui.cbreak_reader import Key

from . import hosted_transport
from .hosted_transport import (
    LOOPBACK,
    MAX_FRAME_BYTES,
    ControllerEndpoint,
    ControllerProtocolError,
    ControllerStream,
    authenticate_controller,
)

if TYPE_CHECKING:
    from agenthicc.background.hosted_runtime import HostedBackground
    from agenthicc.background.supervisor import BackgroundSupervisor
    from agenthicc.runners.tui_session import SessionSwitch, TUISession


@dataclass
class _View:
    stream: ControllerStream
    generation: int
    accepted_sequence: int = 0
    modal_input: str = ""
    disconnected: bool = False


class _HostedTerminal:
    """Bridge a terminal-owning pager while the controller holds the physical TTY."""

    def __init__(self, view: HostedView) -> None:
        self._view = view
        self._keys: queue.Queue[tuple[Key, str] | None] = queue.Queue(maxsize=512)

    def feed(self, key: Key, text: str) -> None:
        self._keys.put_nowait((key, text))

    def is_interactive(self) -> bool:
        return self._view.attached

    def enter_raw_mode(self) -> AbstractContextManager[None]:
        return nullcontext()

    def read_key(self) -> tuple[Key, str]:
        value = self._keys.get()
        if value is None:
            raise OSError("Hosted terminal reader was interrupted")
        return value

    def restore(self) -> None:
        self.close()
        self._keys.put_nowait(None)

    def close(self) -> None:
        while True:
            try:
                self._keys.get_nowait()
            except queue.Empty:
                return

    def query_background(self, timeout: float) -> tuple[int, int, int] | None:
        return None

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        self._view.reporting(focus=focus, mouse=mouse)

    def write_control(self, sequence: str) -> None:
        self._view.write(sequence)

    def supports_progress(self) -> bool:
        return False

    def supports_hyperlinks(self) -> bool:
        from agenthicc.tui.terminal.osc import hyperlinks_supported
        import os

        return hyperlinks_supported(os.environ)


class HostedView(io.TextIOBase):
    """Project the session to one terminal while its tasks live independently."""

    def __init__(self, supervisor: BackgroundSupervisor, columns: int, rows: int) -> None:
        super().__init__()
        self._supervisor = supervisor
        self.columns = columns
        self.rows = rows
        self._active: _View | None = None
        self._generation = 0
        self._record: BackgroundSession | None = None
        self._background: HostedBackground | None = None
        self._cutover: asyncio.Task[None] | None = None
        self._session: TUISession | None = None
        self._context: contextvars.Context | None = None
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._finished = asyncio.Event()
        self._initial_pending = True
        self._initial_timeout: asyncio.TimerHandle | None = None
        self._reconnect_timeout: asyncio.TimerHandle | None = None
        self._startup_output = ""
        self._server: asyncio.AbstractServer | None = None
        self._secret = secrets.token_hex(32)
        self.terminal = _HostedTerminal(self)
        self._terminal_operation = False
        self._editor: asyncio.Future[int] | None = None

    @property
    def attached(self) -> bool:
        return self._active is not None

    def reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        active = self._active
        if active is not None:
            active.stream.write({"type": "reporting", "focus": focus, "mouse": mouse})

    async def run_terminal_operation(self, operation: Callable[[], Awaitable[None]]) -> None:
        if self._active is None or self._cutover is not None or self._terminal_operation:
            raise RuntimeError("The hosted terminal is not available")
        self._terminal_operation = True
        try:
            await operation()
        finally:
            self._terminal_operation = False
            self.terminal.close()

    async def edit_prompt(self, path: Path) -> int:
        return await self.edit_file(path, scope="prompt")

    async def edit_file(self, path: Path, *, scope: Literal["project", "user", "prompt"]) -> int:
        active = self._active
        if active is None or not self._terminal_operation or self._editor is not None:
            raise OSError("The attached editor is unavailable")
        editor: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._editor = editor
        try:
            target = await asyncio.to_thread(path.resolve)
            await active.stream.send(
                {
                    "type": "editor",
                    "path": str(target),
                    "scope": scope,
                    "cwd": str(Path.cwd()),
                }
            )
            return await editor
        finally:
            self._editor = None

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        active = self._active
        if active is None:
            if self._initial_pending:
                self._startup_output = (self._startup_output + text)[-64_000:]
            return len(text)
        writer = active.stream.writer
        if writer.transport.get_write_buffer_size() > MAX_FRAME_BYTES:
            writer.close()
            return len(text)
        for offset in range(0, len(text), 32_000):
            active.stream.write({"type": "output", "text": text[offset : offset + 32_000]})
        return len(text)

    async def publish(self, bootstrap_endpoint: Path, session_endpoint: Path) -> None:
        server = await asyncio.start_server(self._accept, LOOPBACK, 0, limit=MAX_FRAME_BYTES)
        self._server = server
        endpoint = ControllerEndpoint(server.sockets[0].getsockname()[1], self._secret)
        endpoint.publish(session_endpoint)
        endpoint.publish(bootstrap_endpoint)

    async def run(self, session: TUISession) -> None:
        self._session = session
        # The context this session runs in, with its terminal manager and other bindings.
        self._context = contextvars.copy_context()
        session._input_session.hosted_input_ready()
        self._ready.set()
        if self._initial_pending:
            # The window for the first controller opens when the session can serve one. It
            # used to open when the endpoint was published, so a session that took 20 s to
            # build (a large journal, a loaded host) was closed before anyone could attach.
            self._initial_timeout = asyncio.get_running_loop().call_later(
                hosted_transport.INITIAL_CONTROLLER_WINDOW_S, self._closed.set
            )
        await self._closed.wait()

    async def finish(self, switch: SessionSwitch | None) -> None:
        await asyncio.to_thread(self._finish_record)
        try:
            if self._active is not None:
                await self._active.stream.send(
                    {"type": "finished", "switch": asdict(switch) if switch else None}
                )
        finally:
            self._finished.set()

    def _finish_record(self) -> None:
        session = self._session
        if session is None or session._ctx.owner_lease is None:
            return
        lease = session._ctx.owner_lease
        from agenthicc.background.store import SessionNotFound

        try:
            record = self._supervisor.store.get(lease.session_id)
        except SessionNotFound:
            return
        handoff = record.live_handoff
        if handoff is None or handoff.owner_id != lease.owner_id:
            return
        adopted = self._record
        if adopted is None:
            # A disconnected controller can roll back a durable reservation
            # before this runtime ever adopted a background attempt.
            if (
                record.status is not SessionStatus.ATTACHED
                or handoff.generation != self._generation
            ):
                return
        elif handoff != adopted.live_handoff or record.attempt != adopted.attempt:
            return
        if record.status is SessionStatus.CANCELLING:
            status = SessionStatus.CANCELLED
            activity = "Live session cancelled"
        elif record.status in {SessionStatus.ATTACHED, SessionStatus.RUNNING}:
            status = SessionStatus.COMPLETED
            activity = (
                "Interactive session closed"
                if record.status is SessionStatus.ATTACHED
                else "Live session finished"
            )
        else:
            return
        self._supervisor.store.transition(
            record.session_id,
            status,
            expected_status=record.status,
            expected_attempt=record.attempt,
            expected_lease_token=record.lease_token,
            expected_live_handoff=handoff,
            latest_activity=activity,
            lease_token="",
        )

    async def stop(self) -> None:
        if self._initial_timeout is not None:
            self._initial_timeout.cancel()
        if self._reconnect_timeout is not None:
            self._reconnect_timeout.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._active is not None:
            await self._active.stream.close()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Authenticate a controller and wait for the session, then serve it in the session's context."""
        stream = ControllerStream(reader, writer)
        try:
            try:
                await authenticate_controller(stream, self._secret)
                async with asyncio.timeout(20):
                    hello = await stream.receive()
                await self._wait_until_ready(
                    stream, keepalive=hello.get("type") in ("open", "attach")
                )
            except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                return
            except (ControllerProtocolError, InvalidSessionTransition) as exc:
                await stream.send({"type": "error", "message": str(exc)})
                return
            # asyncio runs this handler in the context the controller server was started in,
            # which is before the session exists: the session's terminal manager is bound to
            # a context variable after. A turn started from here (a controller's keys, a
            # detach, the background control loop) would see no manager, and ``run_bash`` with
            # ``background`` would answer "background terminals are unavailable in this
            # execution context". The controller is served in the session's context instead.
            context = self._context
            assert context is not None  # ready is set only by run(), after it captures this
            await asyncio.create_task(self._serve_controller(stream, hello), context=context.copy())
        finally:
            await stream.close()

    async def _serve_controller(self, stream: ControllerStream, hello: dict[str, object]) -> None:
        view: _View | None = None
        try:
            if hello.get("type") == "status":
                await stream.send(self._status())
                return
            if hello.get("type") == "stop_worker":
                await stream.send(await self._stop_worker(hello))
                return
            if hello.get("type") not in ("open", "attach"):
                raise ControllerProtocolError("Invalid controller request")
            view = await self._open_view(stream, hello)
            await self._read_keys(view)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except (ControllerProtocolError, InvalidSessionTransition) as exc:
            await stream.send({"type": "error", "message": str(exc)})
        finally:
            current = self._active
            if view is None and current is not None and current.stream is stream:
                view = current
            if view is not None:
                await self._drop_view(view)
            await stream.close()

    async def _wait_until_ready(self, stream: ControllerStream, *, keepalive: bool) -> None:
        """Hold a connected controller until the session is built, however long that takes.

        A controller that is opening or attaching hears ``starting`` while it waits, so it can
        tell a busy runtime from a dead one. A status query is answered by the caller within its
        own short deadline and is sent nothing.
        """
        while not self._ready.is_set():
            if self._closed.is_set():
                raise ControllerProtocolError("The session runtime is closing")
            try:
                await asyncio.wait_for(
                    self._ready.wait(), timeout=hosted_transport.STARTING_INTERVAL_S
                )
            except TimeoutError:
                if keepalive:
                    await stream.send({"type": "starting"})

    def _status(self) -> dict[str, object]:
        from agenthicc.subagents.communication import registry_for_session

        session = self._session
        assert session is not None
        ctx = session._ctx
        lease = ctx.owner_lease
        assert lease is not None
        registry = registry_for_session(ctx.session_id, ctx.app_state.conversation)
        pools: list[dict[str, object]] = []
        source = session._subagent_notices
        admitted = (
            {item["pool_id"] for item in source.journal.fold_subagent_pool_lifecycle()}
            if source is not None
            else set()
        )
        for handle in registry.handles():
            if handle.pool is None or handle.pool_id not in admitted:
                continue
            if len(pools) >= 16:
                break
            state = handle.pool.state
            attempts = {item.worker_id: item.attempt for item in state.lifecycles}
            pools.append(
                {
                    "pool_id": handle.pool_id,
                    "phase_owner": handle.phase_owner.record() if handle.phase_owner else None,
                    "workers": [
                        {
                            "worker_id": worker.worker_id,
                            "status": worker.status,
                            "attempt": attempts.get(worker.worker_id, 1),
                            "started_at": worker.started_at,
                        }
                        for worker in state.workers[:64]
                    ],
                }
            )
        transcript = lease.path.parent / "conversation.jsonl"
        try:
            cursor = transcript.stat().st_size
        except FileNotFoundError:
            cursor = 0
        return {
            "type": "status",
            "session_id": ctx.session_id,
            "generation": self._generation,
            "attached": self._active is not None,
            "active_turn": session._agent_task is not None and not session._agent_task.done(),
            "pid": lease.info.pid,
            "transcript_cursor": cursor,
            "pools": pools,
        }

    async def _stop_worker(self, frame: dict[str, object]) -> dict[str, object]:
        from agenthicc.subagents.communication import registry_for_session

        session, background = self._session, self._background
        generation = frame.get("generation")
        pool_id, worker_id = frame.get("pool_id"), frame.get("worker_id")
        if (
            session is None
            or background is None
            or not isinstance(pool_id, str)
            or not isinstance(worker_id, str)
            or not 0 < len(pool_id) <= 128
            or not 0 < len(worker_id) <= 128
        ):
            raise ControllerProtocolError("Invalid live worker stop request")
        async with background.lock:
            record = self._record
            if (
                self._active is not None
                or self._cutover is not None
                or record is None
                or record.live_handoff is None
                or type(generation) is not int
                or generation != self._generation
            ):
                raise ControllerProtocolError("Live controller generation is stale")
            current = await asyncio.to_thread(self._supervisor.store.get, record.session_id)
            if current.lease_token != record.lease_token or current.status not in {
                SessionStatus.RUNNING,
                SessionStatus.WAITING_INPUT,
                SessionStatus.WAITING_APPROVAL,
            }:
                raise ControllerProtocolError("The session is no longer accepting worker controls")
            registry = registry_for_session(record.session_id, session._ctx.app_state.conversation)
            handle = registry.get(pool_id)
            if handle is None or handle.pool is None:
                raise ControllerProtocolError("Unknown pool for this live session")
            request = handle.pool.request_stop(
                worker_id, source="background", reason="operator stopped the worker"
            )
            if request is None:
                raise ControllerProtocolError("The worker is not active")
            return {"type": "worker_stop", "request_id": request.request_id}

    async def _open_view(self, stream: ControllerStream, hello: dict[str, object]) -> _View:
        session = self._session
        assert session is not None
        columns, rows = self._terminal_size(hello)
        if self._closed.is_set():
            raise ControllerProtocolError("The session runtime is closing")
        if self._active is not None or self._cutover is not None:
            raise ControllerProtocolError("An interactive controller already owns this session")
        if hello["type"] == "open":
            if not self._initial_pending:
                raise ControllerProtocolError("The initial controller is already assigned")
            self._initial_pending = False
            if self._initial_timeout is not None:
                self._initial_timeout.cancel()
        else:
            record = self._record
            lease = session._ctx.owner_lease
            if record is None or record.live_handoff is None or lease is None:
                raise ControllerProtocolError("There is no live background controller to attach")
            generation = hello.get("generation")
            if type(generation) is not int or generation != self._generation:
                raise ControllerProtocolError("The controller generation changed")
            background = self._background
            assert background is not None
            async with background.lock:
                if self._closed.is_set():
                    raise ControllerProtocolError("The session runtime is closing")
                deadline = time.monotonic() + 5.0
                while True:
                    current = await asyncio.to_thread(self._supervisor.store.get, record.session_id)
                    if current.live_handoff != record.live_handoff:
                        raise ControllerProtocolError("The controller generation changed")
                    if current.status is not SessionStatus.ATTACHED:
                        await background.ingest(current)
                    try:
                        # A timeout around the thread would abandon the result
                        # of a transfer that can still commit after the timeout.
                        self._record = await asyncio.to_thread(
                            self._supervisor.attach_live_runtime, lease, generation=self._generation
                        )
                    except LiveControllerInputPending:
                        if time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(0)
                    else:
                        break
                self._generation += 1

        if self._reconnect_timeout is not None:
            self._reconnect_timeout.cancel()
            self._reconnect_timeout = None
        view = _View(stream, self._generation)
        self._active = view
        stream.write(
            {"type": "ready", "session_id": session._ctx.session_id, "generation": self._generation}
        )
        if self._startup_output:
            self.write(self._startup_output)
            self._startup_output = ""
        self._resize(columns, rows)
        if hello["type"] == "attach":
            from agenthicc.tui.runtime.session_log import load_recent_transcript_page

            session._input_session.set_text("")
            if session._workspace.fullscreen:
                self.write("\x1b[?1049h")
            session._workspace.resume_after_external_process()
            behaviour = session._ctx.cfg.behaviour
            page = await asyncio.to_thread(
                load_recent_transcript_page,
                session._ctx.session_id,
                last_turns=behaviour.resume_transcript_turns,
                max_events=behaviour.resume_transcript_max_events,
                max_bytes=behaviour.resume_transcript_max_bytes,
                max_event_bytes=behaviour.resume_transcript_max_event_bytes,
            )
            await session._workspace.replay_transcript(page.events)
        await stream.writer.drain()
        return view

    @staticmethod
    def _terminal_size(frame: dict[str, object]) -> tuple[int, int]:
        columns = frame.get("columns")
        rows = frame.get("rows")
        if type(columns) is not int or type(rows) is not int:
            raise ControllerProtocolError("Invalid terminal size")
        if not 20 <= columns <= 500 or not 5 <= rows <= 200:
            raise ControllerProtocolError("Terminal size outside supported limits")
        return columns, rows

    def _resize(self, columns: int, rows: int) -> None:
        self.columns, self.rows = columns, rows
        if self._session is not None:
            console = self._session._ctx.console
            console.width, console.height = columns, rows
            self._session._workspace.repaint()

    async def _read_keys(self, view: _View) -> None:
        session = self._session
        assert session is not None
        while self._active is view:
            frame = await view.stream.receive()
            generation = frame.get("generation")
            if (
                view.generation != self._generation
                or type(generation) is not int
                or generation != self._generation
                or self._cutover is not None
            ):
                await view.stream.send({"type": "rejected", "code": "stale_controller"})
                continue
            if frame.get("type") == "editor_done":
                status = frame.get("exit_status")
                if type(status) is not int or self._editor is None or self._editor.done():
                    raise ControllerProtocolError("Invalid editor result")
                self._editor.set_result(status)
                continue
            if frame.get("type") == "resize":
                self._resize(*self._terminal_size(frame))
                continue
            if frame.get("type") == "detach":
                view.accepted_sequence += 1
                if not self.request_detach(session):
                    await view.stream.send(
                        {"type": "rejected", "code": "physical_terminal_required"}
                    )
                continue
            if frame.get("type") != "key" or not isinstance(frame.get("key"), str):
                raise ControllerProtocolError("Invalid terminal input")
            text = frame.get("text")
            if not isinstance(text, str):
                raise ControllerProtocolError("Invalid terminal input text")
            try:
                key = Key(frame["key"])
            except ValueError as exc:
                raise ControllerProtocolError("Unknown terminal key") from exc
            view.accepted_sequence += 1
            if self._terminal_operation:
                try:
                    self.terminal.feed(key, text)
                except queue.Full as exc:
                    raise ControllerProtocolError(
                        "The terminal operation input queue is full"
                    ) from exc
                continue
            if not await self._dispatch_key(session, view, key, text):
                self._closed.set()
                await self._finished.wait()
                return

    async def _dispatch_key(self, session: TUISession, view: _View, key: Key, text: str) -> bool:
        from agenthicc.runners.tui_session import _BACKGROUND_HANDOFF_COMMANDS

        dispatch = session._input_session.dispatch_hosted_key
        if session._ctx.app_state.pending_approval() is not None:
            if key is Key.CHAR and (view.modal_input or text == "/"):
                candidate = view.modal_input + text
                if any(alias.startswith(candidate) for alias in _BACKGROUND_HANDOFF_COMMANDS):
                    view.modal_input = candidate
                    return True
            if key is Key.ENTER and view.modal_input in _BACKGROUND_HANDOFF_COMMANDS:
                view.modal_input = ""
                return self.request_detach(session)
        if view.modal_input:
            held, view.modal_input = view.modal_input, ""
            for character in held:
                if not await dispatch(Key.CHAR, character):
                    return False
        return await dispatch(key, text)

    async def _drop_view(self, view: _View) -> None:
        view.disconnected = True
        if self._editor is not None and not self._editor.done():
            self._editor.set_exception(OSError("The terminal disconnected"))
        if self._terminal_operation:
            self.terminal.restore()
        cutover = self._cutover
        if cutover is not None:
            await asyncio.shield(cutover)
        if self._active is view:
            self._active = None
            if self._record is not None and self._record.status is SessionStatus.ATTACHED:
                if self._session is not None:
                    self._session._workspace.suspend_for_external_process()
                self._reconnect_timeout = asyncio.get_running_loop().call_later(
                    15.0, self._closed.set
                )
            else:
                self._closed.set()

    def request_detach(self, session: TUISession) -> bool:
        """Close admission at the existing input boundary and begin adoption."""

        if self._cutover is not None:
            return True
        if self._terminal_operation:
            session._ctx.app_state.conversation.notify_transient(
                "Close the terminal view before detaching (physical_terminal_required)."
            )
            return False
        view = self._active
        if view is None:
            return False
        session._ctx.app_state.conversation.handoff_pending.set(True)
        self._cutover = session._tasks.start(
            self._detach(session, view), name="live-controller-handoff"
        )
        return True

    async def _detach(self, session: TUISession, view: _View) -> None:
        from agenthicc.background.hosted_runtime import HostedBackground
        from agenthicc.background.integration import _last_user_text

        ctx = session._ctx
        lease = ctx.owner_lease
        assert lease is not None
        try:
            started = time.monotonic()
            intent = _last_user_text(session)
            if not intent:
                ctx.app_state.conversation.notify_transient(
                    "Cannot background this session before it has a user request."
                )
                return
            handle = session._workflow_handle
            mode = ctx.app_state.active_mode().name
            record = BackgroundSession.create(
                ctx.session_id,
                title="Foreground session",
                cwd=str(Path.cwd()),
                workflow_name=ctx.workflow_stack.base_dispatch_name or "",
                intent=intent,
                artifact_dir=str(lease.path.parent),
                run_id=handle.run_id if handle is not None else ctx.goal_run_id,
                requested_mode_name=mode,
                owner_id=current_owner_id(),
            )
            flags = ctx.app_state.cli_flags
            launch = LaunchSpec(
                config_path=ctx.config_path,
                set_overrides=tuple(ctx.cfg_overrides),
                set_secret_overrides=tuple(ctx.cfg_secret_overrides),
                dangerously_skip_permissions=flags.dangerously_skip_permissions,
            )
            request_id = uuid.uuid4().hex
            committed: BackgroundSession | None
            try:
                handoff = await asyncio.to_thread(
                    self._supervisor.prepare_live_handoff,
                    lease,
                    record,
                    launch=launch,
                    request_id=request_id,
                    generation=view.generation,
                    accepted_sequence=view.accepted_sequence,
                    now=time.time(),
                )
                if view.disconnected:
                    await asyncio.to_thread(
                        self._supervisor.reconcile_live_handoff, lease, request_id=request_id
                    )
                    return
                committed = await asyncio.to_thread(
                    self._supervisor.commit_live_handoff, lease, handoff, now=time.time()
                )
            except (OSError, InvalidSessionTransition):
                committed = await asyncio.to_thread(
                    self._supervisor.reconcile_live_handoff, lease, request_id=request_id
                )
                if committed is None:
                    raise
            assert committed.live_handoff is not None
            self._generation = committed.live_handoff.generation
            self._record = committed
            session._workspace.suspend_for_external_process()
            if session._workspace.fullscreen:
                self.write("\x1b[?1049l")
            self._active = None
            background = HostedBackground(self._supervisor.store, session, self._closed.set)
            self._background = background
            session._tasks.start(background.run(committed), name="hosted-background-control")
            source = session._subagent_notices
            live = (
                [handle for handle in source.registry.handles() if not handle.task.done()]
                if source is not None
                else []
            )
            worker_count = sum(
                sum(worker.live for worker in handle.pool.state.workers)
                for handle in live
                if handle.pool is not None
            )
            await view.stream.send(
                {
                    "type": "detached",
                    "session_id": ctx.session_id,
                    "status": SessionStatus.RUNNING.value,
                    "live_pools": len(live),
                    "live_workers": worker_count,
                    "elapsed_s": time.monotonic() - started,
                }
            )
            await view.stream.close()
        except (OSError, QueueFullError, InvalidSessionTransition) as exc:
            if self._active is view:
                ctx.app_state.conversation.notify_transient(str(exc))
        finally:
            ctx.app_state.conversation.handoff_pending.set(False)
            self._cutover = None
