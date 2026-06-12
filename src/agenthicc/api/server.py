"""Headless FastAPI server for Agenthicc (PRD-07).

Exposes intent submission, status polling, a state summary, and a
WebSocket stream of state changes, all backed by the kernel's
:class:`EventProcessor`. The app lifespan starts and stops the
processor's run loop, so test clients that drive lifespan (e.g.
``fastapi.testclient.TestClient``) get a fully running kernel.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from agenthicc.kernel import AppState, Event, EventProcessor

__all__ = ["create_app"]


class IntentIn(BaseModel):
    text: str


def _summarize(state: AppState, last_event: str | None) -> dict:
    return {
        "intents": len(state.intents),
        "workflows": len(state.workflows),
        "agents": len(state.agents),
        "last_event": last_event,
    }


def create_app(processor: EventProcessor, api_key: str | None = None) -> FastAPI:
    """Build the headless API app around a kernel ``EventProcessor``.

    When ``api_key`` is set, every HTTP endpoint requires
    ``Authorization: Bearer <api_key>`` (401 otherwise) and the WebSocket
    endpoint checks the same header at accept time.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(processor.run())
        try:
            yield
        finally:
            await processor.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Agenthicc Headless API", version="0.7.0", lifespan=lifespan)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if api_key is None:
            return
        if authorization != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.post("/v1/intents")
    async def submit_intent(body: IntentIn, _: None = Depends(require_auth)) -> dict:
        intent_id = uuid4().hex
        await processor.emit(
            Event.create(
                "IntentCreated",
                {"intent_id": intent_id, "raw_text": body.text},
            )
        )
        return {"intent_id": intent_id}

    @app.get("/v1/intents/{intent_id}")
    async def get_intent(intent_id: str, _: None = Depends(require_auth)) -> dict:
        intent = processor.get_state().intents.get(intent_id)
        if intent is None:
            raise HTTPException(status_code=404, detail="intent_not_found")
        return {
            "intent_id": intent.intent_id,
            "status": intent.status.value,
            "raw_text": intent.raw_text,
            "workflow_id": intent.workflow_id,
            "created_at": intent.created_at,
            "error": intent.error,
        }

    @app.get("/v1/state/summary")
    async def state_summary(_: None = Depends(require_auth)) -> dict:
        log = processor.event_log
        return _summarize(processor.get_state(), log[-1].event_type if log else None)

    @app.websocket("/v1/ws")
    async def state_stream(websocket: WebSocket) -> None:
        if api_key is not None:
            authorization = websocket.headers.get("authorization")
            if authorization != f"Bearer {api_key}":
                # Reject the handshake before accepting (401-equivalent).
                await websocket.close(code=1008)
                return
        queue = processor.subscribe()
        await websocket.accept()
        try:
            while True:
                state = await queue.get()
                log = processor.event_log
                await websocket.send_json(
                    _summarize(state, log[-1].event_type if log else None)
                )
        except WebSocketDisconnect:
            pass
        finally:
            processor.unsubscribe(queue)

    return app
