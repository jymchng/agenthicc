"""Durable, privacy-filtered notifications for background-session transitions.

The session event journal records a sanitized notification descriptor in the
same durable update as a meaningful lifecycle transition.  This module
projects those descriptors into a SQLite outbox and delivers local notices or
signed HTTPS webhooks independently of worker completion.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import ResolveResult

from .model import BackgroundSession, SessionStatus
from .settings import (
    BackgroundNotificationSettings,
    NOTIFICATION_EVENT_TYPES,
)

if TYPE_CHECKING:
    from .store import BackgroundStore

__all__ = [
    "BackgroundNotificationCoordinator",
    "BackgroundNotificationOutbox",
    "NotificationDelivery",
    "build_notification_descriptor",
    "classify_webhook_status",
    "dispatch_outbox",
]

logger = logging.getLogger(__name__)
_SECRET_ENV = re.compile(r"^AGENTHICC_WEBHOOK_[A-Za-z0-9_]{1,240}$")
_EVENT_FOR_STATUS: dict[SessionStatus, str] = {
    SessionStatus.COMPLETED: "agent_finished",
    SessionStatus.FAILED: "agent_failed",
    SessionStatus.WAITING_APPROVAL: "agent_needs_input",
    SessionStatus.WAITING_INPUT: "agent_needs_input",
    SessionStatus.ORPHANED: "agent_failed",
}
_EVENT_SUMMARIES = {
    "agent_finished": "Background agent finished.",
    "agent_failed": "Background agent failed or needs recovery.",
    "agent_needs_input": "Background agent is waiting for your input.",
}


class DestinationPolicyError(ValueError):
    """A webhook destination violates the configured outbound-network policy."""


@dataclass(frozen=True)
class NotificationDelivery:
    """One leased outbox delivery, with no resolved secret material."""

    delivery_id: str
    event_id: str
    payload: Mapping[str, object]
    destination_name: str
    destination_kind: str
    target_url: str
    secret_env: str
    local_backend: str
    allow_private_network: bool
    request_timeout_s: float
    max_attempts: int
    retry_base_delay_s: float
    retry_max_delay_s: float
    retry_rate_limits: bool
    attempts: int
    lease_token: str


def _workspace_name(cwd: str) -> str:
    name = Path(cwd.rstrip("/\\")).name
    return (name or ("root" if cwd.startswith(("/", "\\")) else "workspace"))[:128]


def build_notification_descriptor(
    previous: BackgroundSession,
    current: BackgroundSession,
    settings: BackgroundNotificationSettings,
    *,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Build a minimal, stable descriptor only for meaningful state edges.

    User prompts, tool activity, provider data, exception text, and absolute
    workspace paths are intentionally not read into this payload.
    """

    if previous.status is current.status:
        return None
    event_type = _EVENT_FOR_STATUS.get(current.status)
    if event_type is None:
        return None
    destinations: list[dict[str, object]] = []
    quiet_hours_active = settings.quiet_hours is not None and settings.quiet_hours.active_at(
        now or datetime.now().astimezone()
    )
    if settings.local_enabled and not quiet_hours_active:
        destinations.append({"name": "local", "kind": "local", "backend": settings.local_backend})
    for webhook in settings.webhooks:
        if event_type in webhook.events:
            destinations.append(
                {
                    "name": webhook.name,
                    "kind": "webhook",
                    "url": webhook.url,
                    "secret_env": webhook.secret_env,
                }
            )
    if not destinations:
        return None
    transition_at = current.state_changed_at or current.last_active or time.time()
    identity = "\0".join(
        (
            current.session_id,
            str(current.attempt),
            event_type,
            f"{transition_at:.9f}",
        )
    )
    event_id = "evt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    payload: dict[str, object] = {
        "schema_version": 1,
        "event_id": event_id,
        "type": event_type,
        "session_id": current.session_id[:128],
        "run_id": current.run_id[:128],
        "workspace": _workspace_name(current.cwd),
        "state": current.status.value,
        "event_time": transition_at,
        "summary": _EVENT_SUMMARIES[event_type],
    }
    return {
        "schema_version": 1,
        "event": payload,
        "destinations": destinations,
        "policy": {
            "allow_private_network": settings.allow_private_network,
            "request_timeout_s": settings.request_timeout_s,
            "max_attempts": settings.max_attempts,
            "retry_base_delay_s": settings.retry_base_delay_s,
            "retry_max_delay_s": settings.retry_max_delay_s,
            "retry_rate_limits": settings.retry_rate_limits,
        },
    }


def _object(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


class BackgroundNotificationOutbox:
    """Cross-process SQLite outbox with idempotent enqueue and fenced claims."""

    def __init__(self, background_root: Path | str) -> None:
        self.root = Path(background_root).expanduser() / "notifications"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.path = self.root / "outbox.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL REFERENCES events(event_id),
                    destination_name TEXT NOT NULL,
                    destination_kind TEXT NOT NULL,
                    target_url TEXT NOT NULL DEFAULT '',
                    secret_env TEXT NOT NULL DEFAULT '',
                    local_backend TEXT NOT NULL DEFAULT 'auto',
                    allow_private_network INTEGER NOT NULL DEFAULT 0,
                    request_timeout_s REAL NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    retry_base_delay_s REAL NOT NULL,
                    retry_max_delay_s REAL NOT NULL,
                    retry_rate_limits INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL,
                    last_result_category TEXT NOT NULL DEFAULT '',
                    last_http_status INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(event_id, destination_name)
                );
                CREATE INDEX IF NOT EXISTS deliveries_due
                    ON deliveries(status, next_attempt_at, created_at);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def enqueue(self, descriptor: Mapping[str, object]) -> bool:
        """Persist an event and its destination deliveries exactly once."""

        event = _object(descriptor.get("event"))
        event_id = event.get("event_id")
        event_type = event.get("type")
        session_id = event.get("session_id")
        destinations = descriptor.get("destinations")
        policy = _object(descriptor.get("policy"))
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(event_type, str)
            or event_type not in NOTIFICATION_EVENT_TYPES
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(destinations, (tuple, list))
            or not destinations
            or len(destinations) > 64
        ):
            raise ValueError("notification descriptor is invalid")
        payload_json = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > 16_384:
            raise ValueError("notification payload exceeds 16 KiB")
        now = time.time()
        inserted = False
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO events(event_id, event_type, session_id, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (event_id, event_type, session_id, now, payload_json),
                )
                for raw_destination in destinations:
                    destination = _object(raw_destination)
                    name = destination.get("name")
                    kind = destination.get("kind")
                    if (
                        not isinstance(name, str)
                        or not name
                        or len(name) > 128
                        or kind not in {"local", "webhook"}
                    ):
                        raise ValueError("notification destination is invalid")
                    url = destination.get("url", "")
                    secret_env = destination.get("secret_env", "")
                    backend = destination.get("backend", "auto")
                    if kind == "webhook":
                        if not isinstance(url, str) or not isinstance(secret_env, str):
                            raise ValueError("webhook destination is invalid")
                        _validate_https_url(url)
                        if not _SECRET_ENV.fullmatch(secret_env):
                            raise ValueError(
                                "webhook secret must use the AGENTHICC_WEBHOOK_ namespace"
                            )
                    if not isinstance(backend, str) or backend not in {
                        "auto",
                        "desktop",
                        "terminal",
                    }:
                        raise ValueError("local notification backend is invalid")
                    connection.execute(
                        """INSERT INTO deliveries(
                            delivery_id, event_id, destination_name, destination_kind,
                            target_url, secret_env, local_backend, allow_private_network,
                            request_timeout_s, max_attempts, retry_base_delay_s,
                            retry_max_delay_s, retry_rate_limits, status, attempts,
                            next_attempt_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
                        (
                            f"{event_id}:{name}",
                            event_id,
                            name,
                            str(kind),
                            url if isinstance(url, str) else "",
                            secret_env if isinstance(secret_env, str) else "",
                            backend,
                            int(policy.get("allow_private_network") is True),
                            _finite_float(policy.get("request_timeout_s"), 5.0, 0.1, 60.0),
                            _bounded_int(policy.get("max_attempts"), 5, 1, 20),
                            _finite_float(policy.get("retry_base_delay_s"), 1.0, 0.1, 3_600.0),
                            _finite_float(policy.get("retry_max_delay_s"), 300.0, 0.1, 86_400.0),
                            int(policy.get("retry_rate_limits") is True),
                            now,
                            now,
                            now,
                        ),
                    )
                inserted = True
            connection.execute("COMMIT")
        return inserted

    def has_pending(self) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM deliveries WHERE status IN ('pending', 'sending') LIMIT 1"
            ).fetchone()
        return row is not None

    def claim_due(self, *, now: float | None = None) -> NotificationDelivery | None:
        """Claim one due item; an expired lease is safe to redeliver by event ID."""

        current_time = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE deliveries SET status='failed', next_attempt_at=0,
                    lease_token='', lease_until=NULL, last_result_category='attempts_exhausted',
                    updated_at=?
                   WHERE attempts >= max_attempts AND status='sending' AND lease_until <= ?""",
                (current_time, current_time),
            )
            row = connection.execute(
                """SELECT d.*, e.payload_json FROM deliveries AS d
                   JOIN events AS e USING(event_id)
                   WHERE (d.status='pending' AND d.next_attempt_at <= ?)
                      OR (d.status='sending' AND d.lease_until <= ?)
                   ORDER BY d.next_attempt_at, d.created_at, d.delivery_id LIMIT 1""",
                (current_time, current_time),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            if row["attempts"] >= row["max_attempts"]:
                connection.execute(
                    "UPDATE deliveries SET status='failed', last_result_category='attempts_exhausted', "
                    "lease_token='', lease_until=NULL, updated_at=? WHERE delivery_id=?",
                    (current_time, row["delivery_id"]),
                )
                connection.execute("COMMIT")
                return None
            token = uuid.uuid4().hex
            attempts = int(row["attempts"]) + 1
            lease_for = max(30.0, float(row["request_timeout_s"]) + 5.0)
            connection.execute(
                """UPDATE deliveries SET status='sending', attempts=?, lease_token=?,
                    lease_until=?, updated_at=? WHERE delivery_id=?""",
                (attempts, token, current_time + lease_for, current_time, row["delivery_id"]),
            )
            connection.execute("COMMIT")
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            self.finish(
                str(row["delivery_id"]),
                token,
                category="invalid_outbox_payload",
                retry_at=None,
                terminal=True,
                now=current_time,
            )
            return None
        if not isinstance(payload, dict):
            self.finish(
                str(row["delivery_id"]),
                token,
                category="invalid_outbox_payload",
                retry_at=None,
                terminal=True,
                now=current_time,
            )
            return None
        return NotificationDelivery(
            delivery_id=str(row["delivery_id"]),
            event_id=str(row["event_id"]),
            payload=payload,
            destination_name=str(row["destination_name"]),
            destination_kind=str(row["destination_kind"]),
            target_url=str(row["target_url"]),
            secret_env=str(row["secret_env"]),
            local_backend=str(row["local_backend"]),
            allow_private_network=bool(row["allow_private_network"]),
            request_timeout_s=float(row["request_timeout_s"]),
            max_attempts=int(row["max_attempts"]),
            retry_base_delay_s=float(row["retry_base_delay_s"]),
            retry_max_delay_s=float(row["retry_max_delay_s"]),
            retry_rate_limits=bool(row["retry_rate_limits"]),
            attempts=attempts,
            lease_token=token,
        )

    def finish(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        category: str,
        retry_at: float | None,
        terminal: bool,
        http_status: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Commit a result only for the current lease holder."""

        current_time = time.time() if now is None else now
        status = "delivered" if category == "delivered" else "failed" if terminal else "pending"
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE deliveries SET status=?, last_result_category=?, last_http_status=?,
                    next_attempt_at=?, lease_token='', lease_until=NULL, updated_at=?
                   WHERE delivery_id=? AND status='sending' AND lease_token=?""",
                (
                    status,
                    category[:64],
                    http_status,
                    0.0 if retry_at is None else retry_at,
                    current_time,
                    delivery_id,
                    lease_token,
                ),
            )
        return cursor.rowcount == 1

    def next_due_time(self) -> float | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT MIN(next_time) FROM (
                    SELECT next_attempt_at AS next_time FROM deliveries WHERE status='pending'
                    UNION ALL
                    SELECT lease_until AS next_time FROM deliveries WHERE status='sending'
                )"""
            ).fetchone()
        value = row[0] if row is not None else None
        return float(value) if isinstance(value, (int, float)) else None

    def list_deliveries(
        self,
        *,
        limit: int = 100,
        status: str = "",
        session_id: str = "",
    ) -> list[dict[str, object]]:
        """Return a safe operator projection; no payload, URL, or secret reference."""

        bounded_limit = max(1, min(1_000, limit))
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("d.status=?")
            params.append(status)
        if session_id:
            clauses.append("e.session_id=?")
            params.append(session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(bounded_limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT d.delivery_id, d.event_id, d.destination_name,
                       d.destination_kind, d.status, d.attempts,
                       d.max_attempts, d.last_result_category, d.last_http_status,
                       d.next_attempt_at, d.created_at, d.updated_at,
                       e.event_type, e.session_id
                   FROM deliveries d JOIN events e USING(event_id)"""
                + where
                + " ORDER BY d.created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]


def _finite_float(value: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    parsed = float(value)
    return parsed if minimum <= parsed <= maximum else default


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if minimum <= value <= maximum else default


def _validate_https_url(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise DestinationPolicyError("invalid webhook host or port") from exc
    host = parsed.hostname
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65_535
    ):
        raise DestinationPolicyError("webhook must be configured as a safe HTTPS URL")
    try:
        normalized_host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise DestinationPolicyError("invalid webhook hostname") from exc
    return normalized_host, port


class _PolicyResolver(aiohttp.abc.AbstractResolver):
    """Resolve only the configured host and reject unsafe answers at connect time."""

    def __init__(self, expected_host: str, *, allow_private: bool) -> None:
        self.expected_host = expected_host
        self.allow_private = allow_private
        self.delegate = aiohttp.resolver.DefaultResolver()
        self.policy_rejected = False

    def _reject(self, message: str) -> NoReturn:
        self.policy_rejected = True
        raise DestinationPolicyError(message)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        try:
            normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            self._reject("invalid resolved hostname")
        if normalized != self.expected_host:
            self._reject("webhook resolver was asked for an unconfigured host")
        try:
            results = await self.delegate.resolve(host, port, family)
        except OSError:
            raise
        if not results:
            self._reject("webhook hostname resolved to no addresses")
        for result in results:
            address = result.get("host")
            if not isinstance(address, str):
                self._reject("webhook resolver returned an invalid address")
            try:
                ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError:
                self._reject("webhook resolver returned an invalid address")
            if not self.allow_private and not ip.is_global:
                self._reject("webhook resolved to a non-public address")
        return results

    async def close(self) -> None:
        await self.delegate.close()


def classify_webhook_status(status: int, *, retry_rate_limits: bool) -> tuple[str, bool]:
    """Return ``(result_category, retryable)`` for one HTTP response."""

    if 200 <= status < 300:
        return "delivered", False
    if status == 429:
        return "rate_limited", retry_rate_limits
    if status in {408, 425} or 500 <= status <= 599:
        return "transient_http_error", True
    if 300 <= status < 400:
        return "redirect_blocked", False
    if 400 <= status < 500:
        return "permanent_http_error", False
    return "invalid_http_status", False


def _retry_after(headers: Mapping[str, str], *, now: float, maximum: float) -> float | None:
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        try:
            moment = parsedate_to_datetime(raw)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = moment.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return now + min(max(0.0, seconds), maximum)


async def _post_webhook(
    delivery: NotificationDelivery,
    *,
    now: float,
) -> tuple[str, int | None, Mapping[str, str]]:
    host, _port = _validate_https_url(delivery.target_url)
    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None
    if ip_literal is not None and not delivery.allow_private_network and not ip_literal.is_global:
        raise DestinationPolicyError("webhook points to a non-public address")
    secret = os.environ.get(delivery.secret_env)
    if not secret:
        return "missing_secret", None, {}
    body = json.dumps(
        dict(delivery.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    timestamp = str(int(now))
    signature = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "agenthicc-notifier/1.0",
        "X-Agenthicc-Event-ID": delivery.event_id,
        "X-Agenthicc-Timestamp": timestamp,
        "X-Agenthicc-Signature": f"sha256={signature}",
    }
    resolver = _PolicyResolver(host, allow_private=delivery.allow_private_network)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        force_close=True,
        limit=1,
        family=socket.AF_UNSPEC,
    )
    timeout = aiohttp.ClientTimeout(total=delivery.request_timeout_s)
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=False,
            raise_for_status=False,
        ) as client:
            async with client.post(
                delivery.target_url,
                data=body,
                headers=headers,
                allow_redirects=False,
            ) as response:
                # Never read/retain response bodies; webhook responses can
                # contain credentials or arbitrary sensitive service data.
                response.release()
                category, _retryable = classify_webhook_status(
                    response.status, retry_rate_limits=delivery.retry_rate_limits
                )
                return category, response.status, dict(response.headers)
    except Exception:
        if resolver.policy_rejected:
            raise DestinationPolicyError(
                "webhook destination was rejected by network policy"
            ) from None
        raise


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _send_local_notification(
    payload: Mapping[str, object],
    backend: str,
    *,
    background_root: Path | None = None,
) -> str:
    if background_root is not None:
        from .attention import tui_is_active

        if tui_is_active(background_root):
            # The active TUI observes the same durable registry transition and
            # routes it through its renderer; never write into its terminal.
            return "delivered"
    workspace = str(payload.get("workspace", "workspace"))[:128]
    summary = str(payload.get("summary", "Background run needs attention."))[:240]
    title = f"Agenthicc · {workspace}"
    if backend in {"auto", "desktop"}:
        if sys.platform == "darwin":
            executable = shutil.which("osascript")
            if executable:
                script = (
                    f'display notification "{_escape_applescript(summary)}" '
                    f'with title "{_escape_applescript(title)}"'
                )
                subprocess.run(
                    [executable, "-e", script],
                    check=True,
                    capture_output=True,
                    timeout=3.0,
                )
                return "delivered"
        elif os.name == "nt":
            # Windows does not provide a stable built-in toast command across
            # supported shells; quietly fall through when no terminal exists.
            pass
        else:
            executable = shutil.which("notify-send")
            if executable:
                subprocess.run(
                    [executable, "--", title, summary],
                    check=True,
                    capture_output=True,
                    timeout=3.0,
                )
                return "delivered"
        if backend == "desktop":
            return "local_notifier_unavailable"
    if backend in {"auto", "terminal"}:
        try:
            if sys.stderr.isatty():
                sys.stderr.write(f"\n[agenthicc] {title}: {summary}\n")
                sys.stderr.flush()
                return "delivered"
        except (OSError, ValueError):
            pass
    return "local_notifier_unavailable"


def _backoff(delivery: NotificationDelivery, *, now: float) -> float:
    exponent = min(30, max(0, delivery.attempts - 1))
    ceiling = min(delivery.retry_max_delay_s, delivery.retry_base_delay_s * (2**exponent))
    jitter_millis: int = 800 + secrets.randbelow(401)
    delay: float = min(delivery.retry_max_delay_s, ceiling * jitter_millis / 1_000.0)
    return now + delay


async def deliver_one(
    delivery: NotificationDelivery,
    *,
    now: float | None = None,
    background_root: Path | None = None,
) -> tuple[str, int | None, float | None, bool]:
    """Deliver one claim and return category/status/retry time/terminal."""

    current_time = time.time() if now is None else now
    if delivery.destination_kind == "local":
        try:
            category = _send_local_notification(
                delivery.payload,
                delivery.local_backend,
                background_root=background_root,
            )
        except (OSError, subprocess.SubprocessError):
            category = "local_notifier_failed"
        return category, None, None, True
    if delivery.destination_kind != "webhook":
        return "invalid_destination_kind", None, None, True
    try:
        category, status, headers = await _post_webhook(delivery, now=current_time)
    except DestinationPolicyError:
        return "destination_blocked", None, None, True
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        category, status, headers = "transient_network_error", None, {}
    if category == "delivered":
        return category, status, None, True
    retryable = category == "transient_network_error"
    retry_after_at: float | None = None
    if status is not None:
        _category, retryable = classify_webhook_status(
            status, retry_rate_limits=delivery.retry_rate_limits
        )
        retry_after_at = _retry_after(headers, now=current_time, maximum=delivery.retry_max_delay_s)
    if not retryable:
        return category, status, None, True
    if delivery.attempts >= delivery.max_attempts:
        return "attempts_exhausted", status, None, True
    retry_at = (
        retry_after_at if retry_after_at is not None else _backoff(delivery, now=current_time)
    )
    return category, status, retry_at, False


async def dispatch_outbox(
    outbox: BackgroundNotificationOutbox,
    *,
    max_events: int = 0,
) -> int:
    """Drain due notifications; ``max_events`` bounds tests/manual invocations."""

    processed = 0
    while max_events <= 0 or processed < max_events:
        delivery = outbox.claim_due()
        if delivery is None:
            if max_events > 0 or not outbox.has_pending():
                return processed
            next_due = outbox.next_due_time()
            delay = 0.5 if next_due is None else min(0.5, max(0.05, next_due - time.time()))
            await asyncio.sleep(delay)
            continue
        category, status, retry_at, terminal = await deliver_one(
            delivery,
            background_root=outbox.root.parent,
        )
        outbox.finish(
            delivery.delivery_id,
            delivery.lease_token,
            category=category,
            retry_at=retry_at,
            terminal=terminal,
            http_status=status,
        )
        processed += 1
    return processed


class BackgroundNotificationCoordinator:
    """Connect durable lifecycle transitions to the outbox and detached sender."""

    def __init__(
        self,
        background_root: Path | str,
        settings: BackgroundNotificationSettings,
        *,
        dispatcher_launcher: Callable[[Path], object] | None = None,
    ) -> None:
        self.background_root = Path(background_root).expanduser()
        self.settings = settings
        self.outbox = BackgroundNotificationOutbox(self.background_root)
        self._dispatcher_launcher = dispatcher_launcher or _launch_dispatcher
        self._dispatcher_process: subprocess.Popen[bytes] | None = None
        self._dispatcher_assumed_active = False

    def describe_transition(
        self, previous: BackgroundSession, current: BackgroundSession
    ) -> dict[str, object] | None:
        return build_notification_descriptor(previous, current, self.settings)

    def enqueue_and_dispatch(self, descriptor: Mapping[str, object]) -> None:
        try:
            self.outbox.enqueue(descriptor)
            self._kick_if_pending()
        except Exception as exc:  # noqa: BLE001
            # The descriptor is already in the authoritative background event
            # journal. Startup reconciliation will retry this bounded enqueue.
            logger.warning("notification enqueue deferred (%s)", type(exc).__name__)

    def reconcile(self, store: BackgroundStore) -> int:
        """Repair journal-to-outbox gaps after a crash and kick pending work."""

        repaired = 0
        for record in store.iter_events():
            payload = record.get("payload")
            descriptor = payload.get("notification") if isinstance(payload, Mapping) else None
            if isinstance(descriptor, Mapping) and self.outbox.enqueue(descriptor):
                repaired += 1
        self._kick_if_pending()
        return repaired

    def list_deliveries(
        self, *, limit: int = 100, status: str = "", session_id: str = ""
    ) -> list[dict[str, object]]:
        return self.outbox.list_deliveries(limit=limit, status=status, session_id=session_id)

    def _kick_if_pending(self) -> None:
        if not self.outbox.has_pending():
            self._dispatcher_process = None
            self._dispatcher_assumed_active = False
            return
        if self._dispatcher_process is not None:
            if self._dispatcher_process.poll() is None:
                return
            self._dispatcher_process = None
        elif self._dispatcher_assumed_active:
            return
        try:
            process = self._dispatcher_launcher(self.outbox.root)
            if isinstance(process, subprocess.Popen):
                self._dispatcher_process = process
            else:
                # A custom launcher may intentionally provide no process
                # handle. Avoid spawning repeatedly in that coordinator.
                self._dispatcher_assumed_active = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("notification dispatcher launch deferred (%s)", type(exc).__name__)


def _launch_dispatcher(root: Path) -> subprocess.Popen[bytes]:
    """Launch an independent sender; never wait on network in the job worker."""

    command = [sys.executable, "-m", "agenthicc.background.notifications", "--root", str(root)]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


async def _run_sender(root: Path) -> int:
    return await dispatch_outbox(BackgroundNotificationOutbox(root.parent))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agenthicc notification outbox dispatcher")
    parser.add_argument("--root", required=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_sender(Path(args.root)))
    except Exception as exc:  # noqa: BLE001
        # Do not print exception text: network libraries may include a URL.
        logger.error("notification dispatcher stopped (%s)", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
