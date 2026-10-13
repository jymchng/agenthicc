from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path

import pytest

from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.notifications import (
    BackgroundNotificationCoordinator,
    BackgroundNotificationOutbox,
    NotificationDelivery,
    _PolicyResolver,
    _backoff,
    _post_webhook,
    _send_local_notification,
    build_notification_descriptor,
    classify_webhook_status,
    deliver_one,
)
from agenthicc.background.settings import (
    BackgroundNotificationSettings,
    BackgroundSettings,
    load_background_settings,
)
from agenthicc.background.store import BackgroundStore


def _webhook_settings(**overrides: object) -> BackgroundNotificationSettings:
    values: dict[str, object] = {
        "webhooks": [
            {
                "name": "ops",
                "url": "https://hooks.example.test/agenthicc",
                "secret_env": "AGENTHICC_WEBHOOK_SECRET",
                "events": [
                    "completed",
                    "failed",
                    "waiting_input",
                    "waiting_approval",
                    "recovery_required",
                ],
            }
        ]
    }
    values.update(overrides)
    return BackgroundNotificationSettings.from_mapping(values)


def _session(
    session_id: str = "session-a",
    *,
    status: SessionStatus = SessionStatus.RUNNING,
    cwd: str = "/home/user/private-project",
    intent: str = "secret prompt must never be sent",
    attempt: int = 2,
    run_id: str = "run-a",
) -> BackgroundSession:
    return BackgroundSession.create(
        session_id,
        title="private title",
        cwd=cwd,
        workflow_name="goal_flow",
        intent=intent,
        now=10.0,
    ).evolve(status=status, attempt=attempt, run_id=run_id, state_changed_at=42.5)


def _descriptor(
    event_id: str = "evt_123",
    *,
    event_type: str = "completed",
    destinations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": {
            "schema_version": 1,
            "event_id": event_id,
            "type": event_type,
            "session_id": "session-a",
            "run_id": "run-a",
            "workspace": "private-project",
            "state": "completed" if event_type == "completed" else event_type,
            "event_time": 42.5,
            "summary": "Background run completed.",
        },
        "destinations": destinations
        or [
            {
                "name": "ops",
                "kind": "webhook",
                "url": "https://hooks.example.test/agenthicc",
                "secret_env": "AGENTHICC_WEBHOOK_SECRET",
            }
        ],
        "policy": {
            "allow_private_network": False,
            "request_timeout_s": 5.0,
            "max_attempts": 3,
            "retry_base_delay_s": 0.5,
            "retry_max_delay_s": 10.0,
            "retry_rate_limits": False,
        },
    }


def _delivery(**overrides: object) -> NotificationDelivery:
    values: dict[str, object] = {
        "delivery_id": "evt_123:ops",
        "event_id": "evt_123",
        "payload": _descriptor()["event"],
        "destination_name": "ops",
        "destination_kind": "webhook",
        "target_url": "https://hooks.example.test/agenthicc",
        "secret_env": "AGENTHICC_WEBHOOK_SECRET",
        "local_backend": "auto",
        "allow_private_network": False,
        "request_timeout_s": 5.0,
        "max_attempts": 3,
        "retry_base_delay_s": 0.5,
        "retry_max_delay_s": 10.0,
        "retry_rate_limits": False,
        "attempts": 1,
        "lease_token": "lease-1",
    }
    values.update(overrides)
    return NotificationDelivery(**values)  # type: ignore[arg-type]


def test_notifications_are_opt_in_and_configuration_is_validated() -> None:
    settings = BackgroundNotificationSettings.from_mapping({})
    assert not settings.enabled
    assert not BackgroundSettings.from_mapping({}).notifications.enabled

    configured = _webhook_settings()
    assert configured.enabled
    assert configured.webhooks[0].events == (
        "completed",
        "failed",
        "waiting_input",
        "waiting_approval",
        "recovery_required",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.test/hook",
        "https://user:pass@hooks.example.test/hook",
        "https://hooks.example.test/hook?token=secret",
        "https://hooks.example.test/hook#fragment",
        "https:///missing-host",
    ],
)
def test_webhook_configuration_rejects_unsafe_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValueError):
        BackgroundNotificationSettings.from_mapping(
            {
                "webhooks": [
                    {
                        "name": "ops",
                        "url": url,
                        "secret_env": "AGENTHICC_SECRET",
                    }
                ]
            }
        )


def test_webhook_configuration_requires_env_reference_and_known_filters() -> None:
    base = {
        "name": "ops",
        "url": "https://hooks.example.test/agenthicc",
        "secret_env": "AGENTHICC_WEBHOOK_SECRET",
    }
    with pytest.raises(ValueError, match="environment-variable"):
        BackgroundNotificationSettings.from_mapping(
            {"webhooks": [{**base, "secret_env": "literal-secret-value"}]}
        )
    with pytest.raises(ValueError, match="environment-variable"):
        BackgroundNotificationSettings.from_mapping(
            {"webhooks": [{**base, "secret_env": "OPENAI_API_KEY"}]}
        )
    with pytest.raises(ValueError, match="unsupported events"):
        BackgroundNotificationSettings.from_mapping(
            {"webhooks": [{**base, "events": ["transcript"]}]}
        )
    with pytest.raises(ValueError, match="unique"):
        BackgroundNotificationSettings.from_mapping({"webhooks": [base, {**base, "name": "OPS"}]})


@pytest.mark.parametrize("value", [True, 0, float("inf"), "5"])
def test_notification_retry_limits_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        BackgroundNotificationSettings.from_mapping({"request_timeout_s": value})


def test_repository_config_cannot_grant_private_webhook_network_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".agenthicc").mkdir(parents=True)
    (project / ".agenthicc").mkdir(parents=True)
    (project / ".agenthicc" / "agenthicc.toml").write_text(
        "[background.notifications]\n"
        "local_enabled = true\n"
        "allow_private_network = true\n"
        "[[background.notifications.webhooks]]\n"
        'name = "untrusted-project-hook"\n'
        'url = "https://hooks.example.test/agenthicc"\n'
        'secret_env = "AGENTHICC_WEBHOOK_SECRET"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("agenthicc.background.settings.Path.home", lambda: home)
    loaded = load_background_settings(cwd=project)
    assert not loaded.notifications.allow_private_network
    assert loaded.notifications.local_enabled
    assert loaded.notifications.webhooks == ()


def test_user_global_config_can_explicitly_allow_private_webhook_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    global_config = home / ".agenthicc" / "agenthicc.toml"
    global_config.parent.mkdir(parents=True)
    project.mkdir()
    global_config.write_text(
        "[background.notifications]\n"
        "allow_private_network = true\n"
        "[[background.notifications.webhooks]]\n"
        'name = "trusted-global-hook"\n'
        'url = "https://hooks.example.test/agenthicc"\n'
        'secret_env = "AGENTHICC_WEBHOOK_SECRET"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("agenthicc.background.settings.Path.home", lambda: home)
    loaded = load_background_settings(cwd=project)
    assert loaded.notifications.allow_private_network
    assert [item.name for item in loaded.notifications.webhooks] == ["trusted-global-hook"]


def test_descriptor_is_redacted_and_only_created_for_meaningful_edges() -> None:
    settings = _webhook_settings()
    previous = _session()
    completed = previous.evolve(
        status=SessionStatus.COMPLETED,
        state_changed_at=50.0,
        latest_activity="provider response and secret prompt text",
        error="Authorization: secret-value",
    )
    descriptor = build_notification_descriptor(previous, completed, settings)
    assert descriptor is not None
    payload = descriptor["event"]
    assert isinstance(payload, dict)
    assert payload["event_id"].startswith("evt_")
    assert payload["type"] == "completed"
    assert payload["workspace"] == "private-project"
    assert payload["state"] == "completed"
    serialized = json.dumps(descriptor)
    for sensitive in (
        "secret prompt",
        "provider response",
        "Authorization:",
        "secret-value",
        "/home/user",
        "private title",
    ):
        assert sensitive not in serialized
    assert build_notification_descriptor(completed, completed, settings) is None
    assert (
        build_notification_descriptor(
            previous, previous.evolve(status=SessionStatus.CANCELLED), settings
        )
        is None
    )
    assert (
        build_notification_descriptor(
            previous, completed, BackgroundNotificationSettings.from_mapping({})
        )
        is None
    )


def test_descriptor_emits_all_supported_attention_states() -> None:
    settings = _webhook_settings()
    for status, event_type in (
        (SessionStatus.WAITING_APPROVAL, "waiting_approval"),
        (SessionStatus.WAITING_INPUT, "waiting_input"),
        (SessionStatus.ORPHANED, "recovery_required"),
        (SessionStatus.FAILED, "failed"),
        (SessionStatus.COMPLETED, "completed"),
    ):
        prior = _session()
        current = prior.evolve(status=status, state_changed_at=50.0)
        descriptor = build_notification_descriptor(prior, current, settings)
        assert descriptor is not None
        event = descriptor["event"]
        assert isinstance(event, dict) and event["type"] == event_type


def test_outbox_is_idempotent_and_operator_projection_hides_payload_and_target(
    tmp_path: Path,
) -> None:
    outbox = BackgroundNotificationOutbox(tmp_path)
    descriptor = _descriptor()
    assert outbox.enqueue(descriptor)
    assert not outbox.enqueue(descriptor)
    rows = outbox.list_deliveries()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "evt_123"
    assert row["status"] == "pending"
    assert "target_url" not in row
    assert "secret_env" not in row
    assert "payload" not in row
    assert outbox.has_pending()


def test_outbox_claim_is_fenced_and_stable_event_id_survives_lease_expiry(
    tmp_path: Path,
) -> None:
    outbox = BackgroundNotificationOutbox(tmp_path)
    outbox.enqueue(_descriptor())
    now = time.time()
    first = outbox.claim_due(now=now)
    assert first is not None and first.attempts == 1
    assert not outbox.finish(
        first.delivery_id,
        "wrong-owner",
        category="delivered",
        retry_at=None,
        terminal=True,
        now=now + 1.0,
    )
    second = outbox.claim_due(now=now + 31.0)
    assert second is not None and second.attempts == 2
    assert second.event_id == first.event_id
    assert not outbox.finish(
        first.delivery_id,
        first.lease_token,
        category="delivered",
        retry_at=None,
        terminal=True,
        now=now + 32.0,
    )
    assert outbox.finish(
        second.delivery_id,
        second.lease_token,
        category="delivered",
        retry_at=None,
        terminal=True,
        http_status=204,
        now=now + 33.0,
    )
    assert outbox.list_deliveries()[0]["status"] == "delivered"
    assert not outbox.has_pending()


def test_invalid_outbox_json_shape_is_terminal_not_left_leased(tmp_path: Path) -> None:
    outbox = BackgroundNotificationOutbox(tmp_path)
    outbox.enqueue(_descriptor())
    with sqlite3.connect(outbox.path) as connection:
        connection.execute("UPDATE events SET payload_json='[]'")
    assert outbox.claim_due(now=time.time()) is None
    receipt = outbox.list_deliveries()[0]
    assert receipt["status"] == "failed"
    assert receipt["last_result_category"] == "invalid_outbox_payload"


@pytest.mark.parametrize(
    ("status", "retry_limits", "category", "retryable"),
    [
        (200, False, "delivered", False),
        (302, True, "redirect_blocked", False),
        (400, True, "permanent_http_error", False),
        (429, False, "rate_limited", False),
        (429, True, "rate_limited", True),
        (503, False, "transient_http_error", True),
    ],
)
def test_http_status_retry_classification(
    status: int, retry_limits: bool, category: str, retryable: bool
) -> None:
    assert classify_webhook_status(status, retry_rate_limits=retry_limits) == (
        category,
        retryable,
    )


def test_retry_jitter_never_exceeds_configured_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agenthicc.background.notifications.secrets.randbelow", lambda _n: 400)
    delivery = _delivery(retry_base_delay_s=10.0, retry_max_delay_s=10.0)
    assert _backoff(delivery, now=100.0) == 110.0


@pytest.mark.asyncio
async def test_policy_resolver_rejects_private_addresses_and_allows_explicit_policy() -> None:
    class Resolver:
        async def resolve(self, host: str, port: int, family: int) -> list[dict[str, object]]:
            del host, port, family
            return [{"host": "127.0.0.1"}]

        async def close(self) -> None:
            return None

    blocked = _PolicyResolver("hooks.example.test", allow_private=False)
    blocked.delegate = Resolver()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="non-public"):
        await blocked.resolve("hooks.example.test", 443)
    allowed = _PolicyResolver("hooks.example.test", allow_private=True)
    allowed.delegate = Resolver()  # type: ignore[assignment]
    assert await allowed.resolve("hooks.example.test", 443) == [{"host": "127.0.0.1"}]
    with pytest.raises(ValueError, match="unconfigured host"):
        await allowed.resolve("other.example.test", 443)


@pytest.mark.asyncio
async def test_ip_literal_private_target_is_blocked_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTHICC_WEBHOOK_SECRET", "not-persisted")
    delivery = _delivery(target_url="https://127.0.0.1/hook")
    category, status, retry_at, terminal = await deliver_one(delivery, now=1_000.0)
    assert (category, status, retry_at, terminal) == (
        "destination_blocked",
        None,
        None,
        True,
    )


@pytest.mark.asyncio
async def test_signed_webhook_headers_cover_timestamp_and_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 202
        headers: dict[str, str] = {}

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def release(self) -> None:
            return None

    class Client:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            connector = self.kwargs.get("connector")
            close = getattr(connector, "close", None)
            if callable(close):
                await close()

        def post(self, url: str, **kwargs: object) -> Response:
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setenv("AGENTHICC_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr("agenthicc.background.notifications.aiohttp.ClientSession", Client)
    delivery = _delivery()
    category, status, _headers = await _post_webhook(delivery, now=1_234.0)
    assert category == "delivered" and status == 202
    headers = captured["headers"]
    assert isinstance(headers, dict)
    body = captured["data"]
    assert isinstance(body, bytes)
    expected = hmac.new(b"test-secret", b"1234." + body, hashlib.sha256).hexdigest()
    assert headers["X-Agenthicc-Timestamp"] == "1234"
    assert headers["X-Agenthicc-Event-ID"] == "evt_123"
    assert headers["X-Agenthicc-Signature"] == f"sha256={expected}"
    assert captured["allow_redirects"] is False
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_missing_secret_is_terminal_and_secret_never_appears_in_delivery_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTHICC_WEBHOOK_SECRET", raising=False)
    outbox = BackgroundNotificationOutbox(tmp_path)
    outbox.enqueue(_descriptor())
    delivery = outbox.claim_due()
    assert delivery is not None
    now = time.time()
    category, status, retry_at, terminal = await deliver_one(delivery, now=now)
    assert category == "missing_secret" and status is None and retry_at is None and terminal
    outbox.finish(
        delivery.delivery_id,
        delivery.lease_token,
        category=category,
        retry_at=retry_at,
        terminal=terminal,
        now=now + 0.1,
    )
    rendered = json.dumps(outbox.list_deliveries())
    assert "AGENTHICC_WEBHOOK_SECRET" not in rendered
    assert "test-secret" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retry_limits", "retryable"),
    [(503, False, True), (400, True, False), (429, False, False), (429, True, True)],
)
async def test_delivery_retries_only_retryable_responses(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    retry_limits: bool,
    retryable: bool,
) -> None:
    async def fake_post(
        _delivery: NotificationDelivery, *, now: float
    ) -> tuple[str, int, dict[str, str]]:
        del now
        return classify_webhook_status(status, retry_rate_limits=retry_limits)[0], status, {}

    monkeypatch.setattr("agenthicc.background.notifications._post_webhook", fake_post)
    monkeypatch.setattr("agenthicc.background.notifications.secrets.randbelow", lambda _n: 200)
    delivery = _delivery(retry_rate_limits=retry_limits, attempts=1)
    category, observed_status, retry_at, terminal = await deliver_one(delivery, now=100.0)
    assert observed_status == status
    assert terminal is not retryable
    if retryable:
        assert retry_at == pytest.approx(100.5)
        assert category in {"transient_http_error", "rate_limited"}
    else:
        assert retry_at is None
        assert category in {"permanent_http_error", "rate_limited"}


@pytest.mark.asyncio
async def test_delivery_stops_retrying_at_configured_attempt_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_post(
        _delivery: NotificationDelivery, *, now: float
    ) -> tuple[str, int, dict[str, str]]:
        del now
        return "transient_http_error", 503, {}

    monkeypatch.setattr("agenthicc.background.notifications._post_webhook", fake_post)
    result = await deliver_one(_delivery(attempts=3, max_attempts=3), now=100.0)
    assert result == ("attempts_exhausted", 503, None, True)


def test_local_notifications_degrade_quietly_without_a_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agenthicc.background.notifications.sys.platform", "linux")
    monkeypatch.setattr("agenthicc.background.notifications.shutil.which", lambda _name: None)
    assert _send_local_notification({"workspace": "demo", "summary": "Done"}, "desktop") == (
        "local_notifier_unavailable"
    )


def test_store_transition_persists_redacted_descriptor_and_reconciles_after_crash(
    tmp_path: Path,
) -> None:
    settings = _webhook_settings()
    launched: list[Path] = []
    store = BackgroundStore(tmp_path)
    store.configure_notifications(settings, dispatcher_launcher=launched.append)
    store.create(_session(status=SessionStatus.RUNNING))
    coordinator = store._notification_coordinator
    assert isinstance(coordinator, BackgroundNotificationCoordinator)

    def fail_enqueue(_descriptor: object) -> None:
        raise OSError("simulated crash after event journal fsync")

    coordinator.enqueue_and_dispatch = fail_enqueue  # type: ignore[method-assign]
    completed = store.transition("session-a", SessionStatus.COMPLETED)
    assert completed.status is SessionStatus.COMPLETED
    journal = store._read_events()
    transition = next(
        event
        for event in reversed(journal)
        if event.get("event_type") == "updated"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("current_status") == "completed"
    )
    payload = transition["payload"]
    assert isinstance(payload, dict)
    descriptor = payload["notification"]
    assert isinstance(descriptor, dict)
    assert "secret prompt" not in json.dumps(descriptor)

    recovered_store = BackgroundStore(tmp_path)
    recovered = recovered_store.configure_notifications(
        settings,
        dispatcher_launcher=lambda root: launched.append(root),
    )
    assert isinstance(recovered, BackgroundNotificationCoordinator)
    rows = recovered.list_deliveries()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert launched
    assert "target_url" not in rows[0]


def test_event_filters_prevent_unsubscribed_outbox_records(tmp_path: Path) -> None:
    settings = BackgroundNotificationSettings.from_mapping(
        {
            "webhooks": [
                {
                    "name": "only-failures",
                    "url": "https://hooks.example.test/agenthicc",
                    "secret_env": "AGENTHICC_WEBHOOK_FAILURES",
                    "events": ["failed"],
                }
            ]
        }
    )
    coordinator = BackgroundNotificationCoordinator(
        tmp_path,
        settings,
        dispatcher_launcher=lambda _root: None,
    )
    running = _session()
    completed = running.evolve(status=SessionStatus.COMPLETED, state_changed_at=50.0)
    assert coordinator.describe_transition(running, completed) is None


def test_notification_settings_in_generated_config_template() -> None:
    from agenthicc.config_template import build_commented_config_template

    template = build_commented_config_template()
    assert "[background.notifications]" in template
    assert "local_enabled = false" in template
    assert "allow_private_network = false" in template
    assert 'secret_env = "AGENTHICC_WEBHOOK_SECRET"' in template
