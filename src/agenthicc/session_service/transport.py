"""Local authenticated HTTP/SSE adapter for the session service (PRD-150)."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aiohttp import web

from .models import SessionCommand, SessionError
from .service import SessionService, SessionSubscription

__all__ = ["LocalSessionServer"]


def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


class LocalSessionServer:
    """Loopback-first HTTP/SSE transport over :class:`SessionService`.

    The server is intentionally an adapter: it never constructs an agent
    runner and it never writes session state outside the service.
    """

    def __init__(
        self,
        service: SessionService,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        auth_token: str | None = None,
        cors_origins: frozenset[str] = frozenset(),
    ) -> None:
        if not _loopback(host) and not auth_token:
            raise ValueError("non-loopback session servers require auth_token")
        self.service = service
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.cors_origins = cors_origins
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._stream_subscriptions: set[SessionSubscription] = set()

    @property
    def app(self) -> web.Application:
        app = web.Application(client_max_size=1_048_576)
        app.router.add_get("/health", self._health)
        app.router.add_post("/v1/sessions", self._create_session)
        app.router.add_get("/v1/sessions/{session_id}", self._snapshot)
        app.router.add_get("/v1/sessions/{session_id}/events", self._events)
        app.router.add_get("/v1/sessions/{session_id}/stream", self._stream)
        app.router.add_post("/v1/sessions/{session_id}/commands", self._command)
        app.router.add_options("/{tail:.*}", self._options)
        return app

    def _capabilities(self, request: web.Request) -> frozenset[str]:
        if self.auth_token is not None:
            supplied = request.headers.get("Authorization", "")
            if supplied != f"Bearer {self.auth_token}":
                raise SessionError("unauthorized", "valid bearer token required", status=401)
        return frozenset({"read", "control", "workspace"})

    def _client_id(self, request: web.Request) -> str:
        return request.headers.get("X-Agenthicc-Client", "http-client")[:128]

    def _response(
        self, request: web.Request, body: Mapping[str, object], *, status: int = 200
    ) -> web.Response:
        response = web.json_response(dict(body), status=status)
        origin = request.headers.get("Origin")
        if origin and origin in self.cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    async def _health(self, request: web.Request) -> web.Response:
        try:
            self._capabilities(request)
        except SessionError as exc:
            return self._response(request, {"ok": False, "error": exc.to_dict()}, status=exc.status)
        return self._response(
            request, {"ok": True, "service": "agenthicc-session", "schema_version": 1}
        )

    async def _create_session(self, request: web.Request) -> web.Response:
        try:
            capabilities = self._capabilities(request)
            data = await request.json()
            if not isinstance(data, Mapping):
                raise SessionError("invalid_request", "request body must be an object")
            snapshot = await self.service.create_session(
                project_root=data.get("project_root", ".")
                if isinstance(data.get("project_root", "."), str)
                else ".",
                client_id=self._client_id(request),
                capabilities=capabilities,
                agent=data.get("agent") if isinstance(data.get("agent"), Mapping) else None,
                workflow=data.get("workflow")
                if isinstance(data.get("workflow"), Mapping)
                else None,
                parent_session_id=data.get("parent_session_id")
                if isinstance(data.get("parent_session_id"), str)
                else None,
            )
            return self._response(request, {"ok": True, "snapshot": snapshot.to_dict()}, status=201)
        except SessionError as exc:
            return self._response(request, {"ok": False, "error": exc.to_dict()}, status=exc.status)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._response(
                request,
                {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}},
                status=400,
            )

    async def _snapshot(self, request: web.Request) -> web.Response:
        try:
            snapshot = await self.service.snapshot(
                request.match_info["session_id"], capabilities=self._capabilities(request)
            )
            return self._response(request, {"ok": True, "snapshot": snapshot.to_dict()})
        except SessionError as exc:
            return self._response(request, {"ok": False, "error": exc.to_dict()}, status=exc.status)

    async def _events(self, request: web.Request) -> web.Response:
        try:
            raw_after = request.query.get("after", "0")
            after = int(raw_after)
            events = await self.service.events(
                request.match_info["session_id"],
                after_sequence=after,
                capabilities=self._capabilities(request),
            )
            return self._response(
                request, {"ok": True, "events": [event.to_dict() for event in events]}
            )
        except (ValueError, SessionError) as exc:
            error = (
                exc.to_dict()
                if isinstance(exc, SessionError)
                else {"code": "invalid_request", "message": str(exc)}
            )
            status = exc.status if isinstance(exc, SessionError) else 400
            return self._response(request, {"ok": False, "error": error}, status=status)

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        subscription: SessionSubscription | None = None
        try:
            after = int(request.query.get("after", "0"))
            subscription = await self.service.subscribe(
                request.match_info["session_id"],
                after_sequence=after,
                client_id=self._client_id(request),
                capabilities=self._capabilities(request),
            )
            self._stream_subscriptions.add(subscription)
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await response.prepare(request)
            async for event in subscription:
                data = json.dumps(event.to_dict(), separators=(",", ":"))
                await response.write(
                    f"id: {event.sequence}\nevent: session\ndata: {data}\n\n".encode()
                )
            return response
        except (ValueError, SessionError) as exc:
            if isinstance(exc, SessionError):
                return self._response(
                    request, {"ok": False, "error": exc.to_dict()}, status=exc.status
                )
            return self._response(
                request,
                {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}},
                status=400,
            )
        finally:
            if subscription is not None:
                self._stream_subscriptions.discard(subscription)
                await subscription.close()

    async def _command(self, request: web.Request) -> web.Response:
        try:
            capabilities = self._capabilities(request)
            data = await request.json()
            if not isinstance(data, Mapping):
                raise SessionError("invalid_request", "request body must be an object")
            command = SessionCommand.from_mapping(
                data,
                client_id=self._client_id(request),
                capabilities=capabilities,
            )
            command = SessionCommand(
                kind=command.kind,
                session_id=request.match_info["session_id"],
                client_id=command.client_id,
                command_id=command.command_id,
                idempotency_key=command.idempotency_key,
                expected_sequence=command.expected_sequence,
                payload=command.payload,
                capabilities=command.capabilities,
            )
            result = await self.service.submit(command)
            return self._response(request, result.to_dict())
        except SessionError as exc:
            return self._response(request, {"ok": False, "error": exc.to_dict()}, status=exc.status)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._response(
                request,
                {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}},
                status=400,
            )

    async def _options(self, request: web.Request) -> web.Response:
        origin = request.headers.get("Origin", "")
        if origin and origin not in self.cors_origins:
            return web.Response(status=403)
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Agenthicc-Client",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            },
        )

    async def start(self) -> str:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        addresses = getattr(self._site, "_server", None)
        sockets = getattr(addresses, "sockets", None) if addresses is not None else None
        if not sockets:
            raise RuntimeError("session server did not expose a listening socket")
        actual_port = sockets[0].getsockname()[1]
        self.port = int(actual_port)
        return f"http://{self.host}:{self.port}"

    async def stop(self) -> None:
        for subscription in list(self._stream_subscriptions):
            await subscription.close()
        self._stream_subscriptions.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
