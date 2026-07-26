"""Client adapters for in-process, web, and IDE session consumers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Protocol

import httpx

from .models import CommandResult, SessionCommand, SessionEvent, SessionSnapshot
from .service import SessionService, SessionSubscription

__all__ = [
    "HttpSessionClient",
    "IdeSessionAdapter",
    "InProcessSessionClient",
    "SessionClient",
    "WebSessionAdapter",
]


class SessionClient(Protocol):
    """Transport-neutral contract consumed by web and IDE adapters."""

    async def create_session(
        self,
        *,
        project_root: str = ".",
        agent: Mapping[str, object] | None = None,
        workflow: Mapping[str, object] | None = None,
    ) -> SessionSnapshot: ...

    async def snapshot(self, session_id: str) -> SessionSnapshot: ...

    async def submit(self, command: SessionCommand) -> CommandResult: ...

    async def events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]: ...


class InProcessSessionClient:
    """Client implementation used by TUI, tests, and local automation."""

    def __init__(
        self,
        service: SessionService,
        *,
        client_id: str = "in-process",
        capabilities: frozenset[str] = frozenset({"read", "control", "workspace"}),
    ) -> None:
        self.service = service
        self.client_id = client_id
        self.capabilities = capabilities

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        return await self.service.snapshot(session_id, capabilities=self.capabilities)

    async def create_session(
        self,
        *,
        project_root: str = ".",
        agent: Mapping[str, object] | None = None,
        workflow: Mapping[str, object] | None = None,
    ) -> SessionSnapshot:
        return await self.service.create_session(
            project_root=project_root,
            client_id=self.client_id,
            capabilities=self.capabilities,
            agent=agent,
            workflow=workflow,
        )

    async def submit(self, command: SessionCommand) -> CommandResult:
        normalized = SessionCommand(
            kind=command.kind,
            session_id=command.session_id,
            client_id=self.client_id,
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            expected_sequence=command.expected_sequence,
            payload=command.payload,
            capabilities=self.capabilities,
        )
        return await self.service.submit(normalized)

    async def events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        return await self.service.events(
            session_id, after_sequence=after_sequence, capabilities=self.capabilities
        )

    async def subscribe(self, session_id: str, *, after_sequence: int = 0) -> SessionSubscription:
        return await self.service.subscribe(
            session_id,
            after_sequence=after_sequence,
            client_id=self.client_id,
            capabilities=self.capabilities,
        )


class HttpSessionClient:
    """Small async client for the local HTTP/SSE transport."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        client_id: str = "http-client",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.client_id = client_id
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None

    def _headers(self) -> dict[str, str]:
        headers = {"X-Agenthicc-Client": self.client_id}
        if self.auth_token is not None:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        response = await self._client.get(
            f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers()
        )
        data = self._decode(response)
        raw = data.get("snapshot")
        if not isinstance(raw, Mapping):
            raise ValueError("session server returned invalid snapshot")
        return SessionSnapshot.from_mapping(raw)

    async def create_session(
        self,
        *,
        project_root: str = ".",
        agent: Mapping[str, object] | None = None,
        workflow: Mapping[str, object] | None = None,
    ) -> SessionSnapshot:
        response = await self._client.post(
            f"{self.base_url}/v1/sessions",
            headers=self._headers(),
            json={
                "project_root": project_root,
                "agent": dict(agent or {}),
                "workflow": dict(workflow or {}),
            },
        )
        data = self._decode(response)
        raw = data.get("snapshot")
        if not isinstance(raw, Mapping):
            raise ValueError("session server returned invalid snapshot")
        return SessionSnapshot.from_mapping(raw)

    async def submit(self, command: SessionCommand) -> CommandResult:
        if not command.session_id:
            raise ValueError("HTTP session commands require session_id")
        response = await self._client.post(
            f"{self.base_url}/v1/sessions/{command.session_id}/commands",
            headers=self._headers(),
            json=command.to_dict(),
        )
        data = self._decode(response)
        raw_result_data = data.get("data", {})
        result_data = dict(raw_result_data) if isinstance(raw_result_data, Mapping) else {}
        return CommandResult(
            ok=bool(data.get("ok")),
            command_id=str(data.get("command_id", command.command_id)),
            session_id=(
                str(data["session_id"])
                if isinstance(data.get("session_id"), str)
                else command.session_id
            ),
            code=str(data.get("code", "ok")),
            message=str(data.get("message", "")),
            data=result_data,
            replayed=bool(data.get("replayed", False)),
        )

    async def events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        response = await self._client.get(
            f"{self.base_url}/v1/sessions/{session_id}/events",
            params={"after": after_sequence},
            headers=self._headers(),
        )
        data = self._decode(response)
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError("session server returned invalid events")
        return [SessionEvent.from_mapping(item) for item in raw_events if isinstance(item, Mapping)]

    async def stream(
        self, session_id: str, *, after_sequence: int = 0
    ) -> AsyncIterator[SessionEvent]:
        async with self._client.stream(
            "GET",
            f"{self.base_url}/v1/sessions/{session_id}/stream",
            params={"after": after_sequence},
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_lines.append(line[6:])
                elif not line and data_lines:
                    payload = json.loads("".join(data_lines))
                    yield SessionEvent.from_mapping(payload)
                    data_lines = []

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, object]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(f"session server returned non-JSON response: {response.text}") from exc
        if not isinstance(data, dict):
            raise ValueError("session server returned a non-object response")
        if response.status_code >= 400:
            error = data.get("error")
            message = (
                error.get("message", "request failed")
                if isinstance(error, Mapping)
                else "request failed"
            )
            raise RuntimeError(str(message))
        return data


class WebSessionAdapter(HttpSessionClient):
    """Named web-client adapter; the browser UI remains transport-only."""


class IdeSessionAdapter(HttpSessionClient):
    """Named IDE/ACP adapter sharing the same client-neutral contract."""
