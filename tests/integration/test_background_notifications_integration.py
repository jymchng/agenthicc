"""Durability and recovery integration tests for background notifications."""

from __future__ import annotations

import json
from functools import partial
import hashlib
import hmac
import shutil
import ssl
import subprocess
import time
from pathlib import Path

from aiohttp import web
import pytest

import agenthicc.background.notifications as notifications
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.notifications import (
    BackgroundNotificationOutbox,
    NotificationDelivery,
    _post_webhook,
    dispatch_outbox,
)
from agenthicc.background.settings import BackgroundNotificationSettings
from agenthicc.background.store import BackgroundStore
from agenthicc.cli.commands import background as background_cli
from agenthicc.cli.context import CLIContext

pytestmark = pytest.mark.integration


def test_cli_notification_status_exposes_safe_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = BackgroundStore(tmp_path / "background")
    outbox = BackgroundNotificationOutbox(store.root)
    outbox.enqueue(
        {
            "event": {
                "event_id": "evt-cli-safe",
                "type": "completed",
                "session_id": "cli-session",
            },
            "destinations": [
                {
                    "name": "ops",
                    "kind": "webhook",
                    "url": "https://hooks.example.test/private-route",
                    "secret_env": "AGENTHICC_WEBHOOK_OPS",
                }
            ],
            "policy": {},
        }
    )
    monkeypatch.setattr(background_cli, "_store_and_supervisor", lambda _ctx: (store, object()))

    background_cli.agents_notifications(CLIContext(), json=True)

    output = capsys.readouterr().out
    assert "evt-cli-safe" in output
    assert "pending" in output
    assert "private-route" not in output
    assert "AGENTHICC_WEBHOOK_OPS" not in output
    assert '"payload"' not in output


def _settings() -> BackgroundNotificationSettings:
    return BackgroundNotificationSettings.from_mapping(
        {
            "webhooks": [
                {
                    "name": "ci",
                    "url": "https://hooks.example.test/agenthicc",
                    "secret_env": "AGENTHICC_WEBHOOK_CI",
                    "events": ["completed", "waiting_input"],
                }
            ],
            "retry_base_delay_s": 0.1,
            "retry_max_delay_s": 1.0,
            "max_attempts": 3,
        }
    )


@pytest.mark.asyncio
async def test_lifecycle_transition_survives_restart_and_delivery_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[Path] = []
    settings = _settings()
    store = BackgroundStore(tmp_path / "background")
    store.configure_notifications(settings, dispatcher_launcher=launches.append)
    session = BackgroundSession.create(
        "integration-session",
        title="Sensitive title",
        cwd=str(tmp_path / "private-workspace"),
        workflow_name="code_plan",
        intent="do not place this prompt in webhook data",
        run_id="run-integration",
    ).evolve(status=SessionStatus.RUNNING)
    store.create(session)

    waiting = store.transition("integration-session", SessionStatus.WAITING_INPUT)
    assert waiting.status is SessionStatus.WAITING_INPUT
    outbox = BackgroundNotificationOutbox(store.root)
    rows = outbox.list_deliveries()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "waiting_input"
    assert rows[0]["status"] == "pending"
    assert launches
    assert store.configure_notifications(settings) is store._notification_coordinator
    assert len(launches) == 1

    descriptor = next(
        item
        for item in store.iter_events()
        if isinstance(item.get("payload"), dict)
        and item["payload"].get("current_status") == "waiting_input"
    )
    event_descriptor = descriptor["payload"]["notification"]  # type: ignore[index]
    assert "do not place this prompt" not in json.dumps(event_descriptor)
    assert str(tmp_path) not in json.dumps(event_descriptor)

    async def successful_delivery(
        delivery: NotificationDelivery, *, now: float | None = None
    ) -> tuple[str, int, None, bool]:
        del delivery, now
        return "delivered", 202, None, True

    monkeypatch.setattr("agenthicc.background.notifications.deliver_one", successful_delivery)
    assert await dispatch_outbox(outbox, max_events=1) == 1
    assert outbox.list_deliveries()[0]["status"] == "delivered"
    assert store.get("integration-session").status is SessionStatus.WAITING_INPUT

    reopened = BackgroundStore(store.root)
    restarted = reopened.configure_notifications(settings, dispatcher_launcher=launches.append)
    assert restarted is not None
    assert len(restarted.list_deliveries()) == 1
    assert restarted.list_deliveries()[0]["status"] == "delivered"
    assert reopened.get("integration-session").status is SessionStatus.WAITING_INPUT


@pytest.mark.asyncio
async def test_retry_receipt_is_durable_across_store_reopen(tmp_path: Path) -> None:
    outbox = BackgroundNotificationOutbox(tmp_path / "background")
    outbox.enqueue(
        {
            "event": {
                "event_id": "evt-retry",
                "type": "completed",
                "session_id": "session-retry",
            },
            "destinations": [
                {
                    "name": "ci",
                    "kind": "webhook",
                    "url": "https://hooks.example.test/agenthicc",
                    "secret_env": "AGENTHICC_WEBHOOK_CI",
                }
            ],
            "policy": {
                "max_attempts": 3,
                "retry_base_delay_s": 0.1,
                "retry_max_delay_s": 1.0,
            },
        }
    )
    base_time = time.time() + 1.0
    claimed = outbox.claim_due(now=base_time)
    assert claimed is not None
    assert outbox.finish(
        claimed.delivery_id,
        claimed.lease_token,
        category="transient_network_error",
        retry_at=base_time + 0.5,
        terminal=False,
        now=base_time + 0.1,
    )

    reopened = BackgroundNotificationOutbox(tmp_path / "background")
    before_due = reopened.claim_due(now=base_time + 0.49)
    assert before_due is None
    retry = reopened.claim_due(now=base_time + 0.5)
    assert retry is not None
    assert retry.event_id == "evt-retry"
    assert retry.attempts == 2
    assert reopened.finish(
        retry.delivery_id,
        retry.lease_token,
        category="delivered",
        retry_at=None,
        terminal=True,
        http_status=202,
        now=base_time + 0.6,
    )
    receipt = reopened.list_deliveries()[0]
    assert receipt["attempts"] == 2
    assert receipt["last_result_category"] == "delivered"
    assert receipt["last_http_status"] == 202


@pytest.mark.asyncio
async def test_signed_webhook_uses_local_https_server_and_blocks_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the actual HTTP stack against a local TLS server.

    The test-only connector disables certificate verification because the
    server uses an ephemeral self-signed certificate. Production delivery
    keeps aiohttp's normal TLS verification enabled.
    """
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required to create the ephemeral test certificate")

    cert_path = tmp_path / "webhook-test-cert.pem"
    key_path = tmp_path / "webhook-test-key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_path, key_path)

    received: list[tuple[dict[str, str], bytes]] = []
    redirected = False

    async def webhook(request: web.Request) -> web.Response:
        received.append((dict(request.headers), await request.read()))
        return web.Response(status=200, body=b"not-json: deliberately malformed response")

    async def redirect_target(_request: web.Request) -> web.Response:
        nonlocal redirected
        redirected = True
        return web.Response(status=200)

    async def redirect(_request: web.Request) -> web.Response:
        return web.Response(status=307, headers={"Location": "/redirect-target"})

    app = web.Application()
    app.router.add_post("/hook", webhook)
    app.router.add_post("/redirect-target", redirect_target)
    app.router.add_post("/redirect", redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
    await site.start()
    assert site._server is not None and site._server.sockets
    port = site._server.sockets[0].getsockname()[1]

    # Only the test connector opts out of verification for its self-signed
    # localhost certificate; the production ClientSession path is unchanged.
    actual_connector = notifications.aiohttp.TCPConnector
    monkeypatch.setattr(
        notifications.aiohttp,
        "TCPConnector",
        partial(actual_connector, ssl=False),
    )
    monkeypatch.setenv("AGENTHICC_WEBHOOK_TEST", "integration-secret")

    def leased_delivery(path: str) -> NotificationDelivery:
        outbox = BackgroundNotificationOutbox(tmp_path / f"outbox-{path}")
        outbox.enqueue(
            {
                "event": {
                    "event_id": f"evt-{path}",
                    "type": "completed",
                    "session_id": "local-server-session",
                },
                "destinations": [
                    {
                        "name": "local-test",
                        "kind": "webhook",
                        "url": f"https://127.0.0.1:{port}/{path}",
                        "secret_env": "AGENTHICC_WEBHOOK_TEST",
                    }
                ],
                "policy": {"allow_private_network": True},
            }
        )
        delivery = outbox.claim_due()
        assert delivery is not None
        return delivery

    try:
        delivered, status, _headers = await _post_webhook(leased_delivery("hook"), now=1_234.0)
        assert (delivered, status) == ("delivered", 200)
        assert len(received) == 1
        headers, body = received[0]
        assert json.loads(body)["event_id"] == "evt-hook"
        expected = hmac.new(b"integration-secret", b"1234." + body, hashlib.sha256).hexdigest()
        assert headers["X-Agenthicc-Timestamp"] == "1234"
        assert headers["X-Agenthicc-Signature"] == f"sha256={expected}"

        redirected_result = await _post_webhook(leased_delivery("redirect"), now=1_235.0)
        assert redirected_result[:2] == ("redirect_blocked", 307)
        assert not redirected
        assert len(received) == 1
    finally:
        await runner.cleanup()
