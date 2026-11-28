"""Start a durable interactive runtime and attach a local terminal client."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from agenthicc.cli.context import CLIContext, CLIFlags
from agenthicc.runners.process_lease import atomic_replace, read_json_object
from agenthicc.tui.terminal.backend import TerminalBackend, get_backend, terminal_stream

from . import hosted_transport
from .hosted_transport import (
    ControllerEndpoint,
    ControllerProtocolError,
    ControllerStream,
    HostedStartupError,
    LocalControllerUnavailable,
    connect_controller,
)

if TYPE_CHECKING:
    from agenthicc.background.model import BackgroundSession
    from agenthicc.runners.tui_session import SessionSwitch

_ENDPOINT = ".controller"


@dataclass(frozen=True)
class _EditRequest:
    path: Path
    cwd: Path
    scope: Literal["prompt", "project", "user"]

    def run(self, session_id: str) -> int:
        if self.scope == "prompt":
            from agenthicc.runners.session_lease import SessionOwnerStore
            from agenthicc.tui.input.unified_session import _run_editor

            expected = SessionOwnerStore().session_path(session_id)
            if (
                self.path.parent != expected
                or self.path.suffix != ".md"
                or not self.path.name.startswith("prompt-")
            ):
                raise ValueError("Invalid prompt editor path")
            return _run_editor(self.path)
        from agenthicc.agents.external_editor import open_definition_in_editor

        open_definition_in_editor(self.path, scope=self.scope, project_root=self.cwd)
        return 0


def _terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    return max(20, min(size.columns, 500)), max(5, min(size.lines, 200))


async def _send_keys(backend: TerminalBackend, stream: ControllerStream, generation: int) -> None:
    while True:
        read = asyncio.create_task(asyncio.to_thread(backend.read_key))
        try:
            key, text = await asyncio.shield(read)
        except asyncio.CancelledError:
            await asyncio.gather(read, return_exceptions=True)
            raise
        await stream.send({"type": "key", "generation": generation, "key": key.value, "text": text})


async def _send_size(stream: ControllerStream, generation: int, initial: tuple[int, int]) -> None:
    previous = initial
    while True:
        await asyncio.sleep(0.1)
        size = _terminal_size()
        if size != previous:
            previous = size
            await stream.send(
                {"type": "resize", "generation": generation, "columns": size[0], "rows": size[1]}
            )


def _switch(value: object) -> SessionSwitch | None:
    from agenthicc.runners.tui_session import SessionSwitch

    if not isinstance(value, Mapping):
        return None
    action = value.get("action")
    target = value.get("target")
    origin = value.get("origin")
    origin_cwd = value.get("origin_cwd")
    if (
        action in ("attach", "open_saved", "hub", "directory", "fork")
        and isinstance(target, str)
        and isinstance(origin, str)
        and isinstance(origin_cwd, str)
    ):
        return SessionSwitch(action, target, origin, origin_cwd)
    raise ControllerProtocolError("Invalid session switch")


async def _display(
    stream: ControllerStream, backend: TerminalBackend
) -> SessionSwitch | _EditRequest | None:
    out = terminal_stream()
    while True:
        frame = await stream.receive()
        kind = frame.get("type")
        if kind == "output":
            body = frame.get("text")
            if not isinstance(body, str):
                raise ControllerProtocolError("Invalid terminal output")
            out.write(body)
            out.flush()
        elif kind == "reporting":
            focus, mouse = frame.get("focus"), frame.get("mouse")
            backend.set_reporting(
                focus=focus if isinstance(focus, bool) else None,
                mouse=mouse if isinstance(mouse, bool) else None,
            )
        elif kind == "editor":
            path, cwd, scope = frame.get("path"), frame.get("cwd"), frame.get("scope")
            if (
                not isinstance(path, str)
                or not isinstance(cwd, str)
                or scope not in ("prompt", "project", "user")
            ):
                raise ControllerProtocolError("Invalid terminal editor operation")
            return _EditRequest(Path(path), Path(cwd), scope)
        elif kind == "detached":
            session_id = frame.get("session_id")
            if not isinstance(session_id, str):
                raise ControllerProtocolError("Invalid detach acknowledgement")
            pools, workers = frame.get("live_pools"), frame.get("live_workers")
            counts = (
                f"  {workers} live workers across {pools} pools.\r\n"
                if type(pools) is int and pools > 0 and type(workers) is int
                else ""
            )
            out.write(f"\r\n● Session {session_id} is running in the background.\r\n{counts}")
            out.write(f"  Run agenthicc attach {session_id} to return.\r\n")
            out.flush()
            # ``/bg`` ends in the agents hub with the new agent selected, as it does for a
            # terminal-owned session. Where no hub can open (a pipe, background disabled) the
            # caller falls through to the shell, and the lines above are what the user sees.
            from agenthicc.runners.tui_session import SessionSwitch

            return SessionSwitch("hub", session_id, origin=session_id, origin_cwd=os.getcwd())
        elif kind == "finished":
            return _switch(frame.get("switch"))
        elif kind == "error":
            raise ControllerProtocolError(str(frame.get("message", "Controller disconnected")))
        elif kind != "rejected":
            raise ControllerProtocolError("Unknown terminal output")


async def _connect(endpoint: ControllerEndpoint) -> ControllerStream:
    """Connect and authenticate, naming a runtime that is too busy to answer."""
    try:
        return await connect_controller(endpoint)
    except TimeoutError as exc:
        raise HostedStartupError(
            f"The session runtime did not answer within {hosted_transport.HANDSHAKE_TIMEOUT:g} s; "
            "the machine may be busy. Try again."
        ) from exc


async def _await_ready(stream: ControllerStream) -> dict[str, object]:
    """Wait for ``ready`` while the runtime builds its session.

    The wait is bounded by silence, not by how long the build takes: a runtime that is working
    sends ``starting`` every :data:`hosted_transport.STARTING_INTERVAL_S`, so a large session or
    a loaded host takes as long as it takes, while a runtime that has stopped answering is
    reported after :data:`hosted_transport.STARTUP_SILENCE_TIMEOUT_S`.
    """
    announced = False
    while True:
        try:
            async with asyncio.timeout(hosted_transport.STARTUP_SILENCE_TIMEOUT_S):
                frame = await stream.receive()
        except TimeoutError as exc:
            raise HostedStartupError(
                "The session runtime sent nothing for "
                f"{hosted_transport.STARTUP_SILENCE_TIMEOUT_S:g} s while it was starting."
            ) from exc
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            raise HostedStartupError("The session runtime stopped before it was ready.") from exc
        if frame.get("type") != "starting":
            return frame
        if not announced:
            announced = True
            print("Starting the session…", file=sys.stderr, flush=True)


async def _run_controller(
    endpoint: ControllerEndpoint, *, generation: int | None
) -> SessionSwitch | None:
    stream = await _connect(endpoint)
    size = _terminal_size()
    try:
        await stream.send(
            {
                "type": "open" if generation is None else "attach",
                "generation": generation,
                "columns": size[0],
                "rows": size[1],
            }
        )
        ready = await _await_ready(stream)
        if ready.get("type") == "error":
            raise ControllerProtocolError(str(ready.get("message", "Unable to attach")))
        current = ready.get("generation")
        if ready.get("type") != "ready" or type(current) is not int:
            raise ControllerProtocolError("Runtime did not assign a controller")
        session_id = ready.get("session_id")
        if not isinstance(session_id, str):
            raise ControllerProtocolError("Missing controller session ID")
        while True:
            backend = get_backend()
            try:
                with backend.enter_raw_mode():
                    backend.set_reporting(focus=True, mouse=False)
                    keys = asyncio.create_task(_send_keys(backend, stream, current))
                    resize = asyncio.create_task(_send_size(stream, current, size))
                    display = asyncio.create_task(_display(stream, backend))
                    try:
                        completed, _ = await asyncio.wait(
                            (keys, display), return_when=asyncio.FIRST_COMPLETED
                        )
                        if display not in completed:
                            keys.result()
                            return None
                        result = display.result()
                    finally:
                        backend.restore()
                        for task in (keys, resize, display):
                            task.cancel()
                        await asyncio.gather(keys, resize, display, return_exceptions=True)
            finally:
                backend.close()
            if not isinstance(result, _EditRequest):
                return result
            try:
                code = await asyncio.to_thread(result.run, session_id)
            except (OSError, ValueError, RuntimeError):
                code = 1
            await stream.send({"type": "editor_done", "generation": current, "exit_status": code})
    finally:
        await stream.close()


async def hosted_terminal_status(record: BackgroundSession) -> dict[str, object]:
    return await _query_runtime_status(
        record.session_id, record.worker_pid, Path(record.artifact_dir)
    )


async def discover_interactive_terminal(session_id: str) -> dict[str, object] | None:
    """Discover a running session before its first background adoption."""

    from agenthicc.runners.session_lease import SessionOwnerStore

    owners = SessionOwnerStore()
    inspection = await asyncio.to_thread(owners.inspect, session_id)
    owner = inspection.owner
    if inspection.state != "active" or owner is None or owner.entrypoint != "hosted-tui":
        return None
    return await _query_runtime_status(session_id, owner.pid, owners.session_path(session_id))


async def _query_runtime_status(
    session_id: str, pid: int | None, artifact: Path
) -> dict[str, object]:
    endpoint = ControllerEndpoint.load(artifact / _ENDPOINT)
    stream = await connect_controller(endpoint)
    try:
        async with asyncio.timeout(2):
            await stream.send({"type": "status"})
            status = await stream.receive()
        if (
            status.pop("type", None) != "status"
            or status.get("session_id") != session_id
            or status.get("pid") != pid
        ):
            raise ControllerProtocolError("Unexpected hosted session identity")
        return status
    finally:
        await stream.close()


async def stop_hosted_worker(record: BackgroundSession, pool_id: str, worker_id: str) -> str:
    handoff = record.live_handoff
    if handoff is None:
        raise ControllerProtocolError("The session has no live worker controller")
    endpoint = ControllerEndpoint.load(Path(record.artifact_dir) / _ENDPOINT)
    stream = await connect_controller(endpoint)
    try:
        async with asyncio.timeout(5):
            await stream.send(
                {
                    "type": "stop_worker",
                    "generation": handoff.generation,
                    "pool_id": pool_id,
                    "worker_id": worker_id,
                }
            )
            response = await stream.receive()
        request_id = response.get("request_id")
        if response.get("type") != "worker_stop" or not isinstance(request_id, str):
            raise ControllerProtocolError(str(response.get("message", "Worker stop was refused")))
        return request_id
    finally:
        await stream.close()


async def attach_hosted_terminal(record: BackgroundSession) -> SessionSwitch | None:
    handoff = record.live_handoff
    if handoff is None or not get_backend().is_interactive():
        raise ControllerProtocolError("Live attachment requires an interactive terminal")
    endpoint = ControllerEndpoint.load(Path(record.artifact_dir) / _ENDPOINT)
    return await _run_controller(endpoint, generation=handoff.generation)


def _cli_wire(config: CLIContext) -> dict[str, object]:
    return {
        "resume_id": config.resume_id,
        "continue_session": config.continue_session,
        "config_path": config.config_path,
        "set_overrides": config.set_overrides,
        "set_secret_overrides": config.set_secret_overrides,
        "flags": asdict(config.flags),
        "record_cassette": config.record_cassette,
        "mode_name": config.mode_name,
        "workflow_name": config.workflow_name,
        "agent_name": config.agent_name,
        "size": _terminal_size(),
    }


async def run_hosted_terminal(config: CLIContext) -> SessionSwitch | None:
    from agenthicc.background.supervisor import BackgroundSupervisor

    with tempfile.TemporaryDirectory(prefix="agenthicc-controller-") as directory:
        bootstrap = Path(directory)
        atomic_replace(
            bootstrap / "config",
            json.dumps(_cli_wire(config)).encode(),
            prefix=".config-",
        )
        process = await asyncio.to_thread(
            BackgroundSupervisor.launch_interactive_runtime, bootstrap
        )
        path = bootstrap / "endpoint"
        try:
            async with asyncio.timeout(hosted_transport.LAUNCH_TIMEOUT_S):
                while not path.is_file():
                    if process.poll() is not None:
                        failure = read_json_object(bootstrap / "failure") or {}
                        message = str(
                            failure.get("message", "Unable to start the hosted session runtime")
                        )
                        if process.returncode == 3:
                            print(message, file=sys.stderr)
                            raise SystemExit(3)
                        if failure.get("code") == "local_controller_unavailable":
                            raise LocalControllerUnavailable(message)
                        raise ControllerProtocolError(message)
                    await asyncio.sleep(0.01)
        except TimeoutError as exc:
            # Leaving it running would strand a runtime that holds the session's lease.
            process.kill()
            await asyncio.to_thread(process.wait)
            raise HostedStartupError(
                "The session runtime did not publish its controller within "
                f"{hosted_transport.LAUNCH_TIMEOUT_S:g} s and was stopped."
            ) from exc
        return await _run_controller(ControllerEndpoint.load(path), generation=None)


async def _serve(bootstrap: Path) -> None:
    from agenthicc.background.settings import load_background_settings
    from agenthicc.background.store import BackgroundStore
    from agenthicc.background.supervisor import BackgroundSupervisor
    from agenthicc.runners.hosted_view import HostedView
    from agenthicc.runners.session_lease import (
        SessionOpenCoordinator,
        register_interactive_runtime,
        unregister_interactive_runtime,
    )
    from agenthicc.runners.tui_session import _run_tui_session_impl
    from agenthicc.tui.runtime.session_log import create_session_id

    config = read_json_object(bootstrap / "config")
    if config is None:
        raise ControllerProtocolError("Missing runtime configuration")

    def optional_text(key: str) -> str | None:
        value = config.get(key)
        return value if isinstance(value, str) else None

    def strings(key: str) -> list[str]:
        value = config.get(key)
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    resume_id = optional_text("resume_id")
    config_path = optional_text("config_path")
    overrides = strings("set_overrides")
    settings = load_background_settings(config_path=config_path, overrides=tuple(overrides))
    flags = config.get("flags")
    cli_flags = CLIFlags(
        dangerously_skip_permissions=isinstance(flags, Mapping)
        and flags.get("dangerously_skip_permissions") is True,
        debug=isinstance(flags, Mapping) and flags.get("debug") is True,
    )
    size = config.get("size")
    columns, rows = (80, 24)
    if isinstance(size, list) and len(size) == 2 and all(type(item) is int for item in size):
        columns, rows = size

    coordinator = SessionOpenCoordinator()
    selected = (
        coordinator.select_latest_for_cwd(Path.cwd(), entrypoint="hosted-tui")
        if resume_id is None and config.get("continue_session") is True
        else None
    )
    if selected is not None:
        resume_id, lease = selected
    else:
        lease = (
            coordinator.acquire_existing(resume_id, entrypoint="hosted-tui")
            if resume_id is not None
            else coordinator.acquire_new(create_session_id(), entrypoint="hosted-tui")
        )
    endpoint = lease.path.parent / _ENDPOINT
    view = HostedView(
        BackgroundSupervisor.from_settings(BackgroundStore(settings.store_root), settings),
        columns,
        rows,
    )
    original_stdout = sys.stdout
    try:
        sys.stdout = view
        try:
            await view.publish(bootstrap / "endpoint", endpoint)
        except OSError as exc:
            raise LocalControllerUnavailable(
                "The local controller endpoint is unavailable"
            ) from exc
        # The agents hub lists this runtime from now on, not only after a detach.
        await asyncio.to_thread(register_interactive_runtime, lease, cwd=Path.cwd())
        switch = await _run_tui_session_impl(
            resume_id=resume_id,
            cli_overrides=overrides,
            cli_secret_overrides=strings("set_secret_overrides"),
            record_cassette=optional_text("record_cassette"),
            cli_flags=cli_flags,
            config_path=config_path,
            mode_name=optional_text("mode_name"),
            workflow_name=optional_text("workflow_name"),
            agent_name=optional_text("agent_name"),
            owner_lease=lease,
            hosted_view=view,
        )
        await view.finish(switch)
    finally:
        sys.stdout = original_stdout
        await asyncio.to_thread(unregister_interactive_runtime, lease.session_id)
        if not lease.released:
            endpoint.unlink(missing_ok=True)
            lease.release()
        await view.stop()


if __name__ == "__main__":
    from agenthicc.runners.session_lease import SessionAlreadyActiveError

    path = Path(sys.argv[1])
    try:
        asyncio.run(_serve(path))
    except LocalControllerUnavailable as exc:
        atomic_replace(
            path / "failure",
            json.dumps({"code": "local_controller_unavailable", "message": str(exc)}).encode(),
            prefix=".failure-",
        )
        raise SystemExit(1) from exc
    except SessionAlreadyActiveError as exc:
        atomic_replace(
            path / "failure", json.dumps({"message": str(exc)}).encode(), prefix=".failure-"
        )
        raise SystemExit(3) from exc
