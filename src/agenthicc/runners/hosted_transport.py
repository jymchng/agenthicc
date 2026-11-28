"""Authenticated local controller transport for hosted interactive sessions."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agenthicc.runners.process_lease import atomic_replace, read_json_object

LOOPBACK = "127.0.0.1"
MAX_FRAME_BYTES = 512 * 1024
#: Seconds a peer has to answer the authentication handshake. A runtime that is still
#: importing and building its session answers late on a busy host; 5 s was too tight.
HANDSHAKE_TIMEOUT = 15.0
#: While a runtime builds its session, a connected controller hears ``starting`` this often.
STARTING_INTERVAL_S = 2.0
#: How long a controller waits in silence for a runtime that is starting. This bounds
#: *silence*, not the build: a runtime that is working says so every STARTING_INTERVAL_S.
STARTUP_SILENCE_TIMEOUT_S = 30.0
#: How long the launcher waits for a new runtime process to publish its controller endpoint.
LAUNCH_TIMEOUT_S = 60.0
#: How long a ready runtime waits for its first controller before it closes itself.
INITIAL_CONTROLLER_WINDOW_S = 20.0
_HEX_TOKEN = re.compile("[0-9a-f]{64}")


class ControllerProtocolError(ValueError):
    """A peer sent an invalid or unauthenticated controller frame."""


class HostedStartupError(RuntimeError):
    """A hosted session runtime did not become usable; the message says which step and why."""


class LocalControllerUnavailable(OSError):
    """The runtime could not publish a local controller before it started."""


def _token(value: object) -> str:
    if not isinstance(value, str) or _HEX_TOKEN.fullmatch(value) is None:
        raise ControllerProtocolError("Invalid controller authentication token")
    return value


@dataclass(frozen=True)
class ControllerEndpoint:
    port: int
    secret: str

    def publish(self, path: Path) -> None:
        payload = json.dumps({"port": self.port, "secret": self.secret}).encode("utf-8")
        atomic_replace(path, payload, prefix=".controller-")

    @classmethod
    def load(cls, path: Path) -> ControllerEndpoint:
        payload = read_json_object(path)
        port = payload.get("port") if payload is not None else None
        if type(port) is not int or not 0 < port < 65_536:
            raise ControllerProtocolError("Invalid controller endpoint")
        assert payload is not None
        return cls(port, _token(payload.get("secret")))


class ControllerStream:
    """One bounded JSON stream, authenticated before it accepts control."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def receive(self) -> dict[str, object]:
        try:
            data = await self.reader.readuntil(b"\n")
            frame: object = json.loads(data)
        except (asyncio.LimitOverrunError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControllerProtocolError("Invalid controller frame") from exc
        if not isinstance(frame, dict):
            raise ControllerProtocolError("Controller frame must be an object")
        return frame

    def write(self, frame: Mapping[str, object]) -> None:
        encoded = json.dumps(dict(frame), ensure_ascii=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_FRAME_BYTES:
            raise ControllerProtocolError("Controller frame is too large")
        self.writer.write(encoded + b"\n")

    async def send(self, frame: Mapping[str, object]) -> None:
        self.write(frame)
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


def _proof(secret: str, role: str, client_nonce: str, server_nonce: str) -> str:
    challenge = f"agenthicc.controller.1/{role}/{client_nonce}/{server_nonce}".encode()
    return hmac.digest(bytes.fromhex(secret), challenge, "sha256").hex()


async def authenticate_controller(stream: ControllerStream, secret: str) -> None:
    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
        hello = await stream.receive()
        client_nonce = _token(hello.get("nonce"))
        server_nonce = secrets.token_hex(32)
        await stream.send(
            {"nonce": server_nonce, "proof": _proof(secret, "runtime", client_nonce, server_nonce)}
        )
        reply = await stream.receive()
        expected = _proof(secret, "client", client_nonce, server_nonce)
        if not hmac.compare_digest(_token(reply.get("proof")), expected):
            raise ControllerProtocolError("Controller authentication failed")


async def connect_controller(endpoint: ControllerEndpoint) -> ControllerStream:
    async with asyncio.timeout(HANDSHAKE_TIMEOUT):
        reader, writer = await asyncio.open_connection(
            LOOPBACK, endpoint.port, limit=MAX_FRAME_BYTES
        )
        stream = ControllerStream(reader, writer)
        try:
            client_nonce = secrets.token_hex(32)
            await stream.send({"nonce": client_nonce})
            hello = await stream.receive()
            server_nonce = _token(hello.get("nonce"))
            expected = _proof(endpoint.secret, "runtime", client_nonce, server_nonce)
            if not hmac.compare_digest(_token(hello.get("proof")), expected):
                raise ControllerProtocolError("Runtime authentication failed")
            await stream.send(
                {"proof": _proof(endpoint.secret, "client", client_nonce, server_nonce)}
            )
        except BaseException:
            await stream.close()
            raise
        return stream
