"""Configuration for the local background-session supervisor.

Background settings are read at the supervisor boundary so older config files
remain valid.  The section is intentionally small, local-only, and validated
before it can affect worker creation.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


NOTIFICATION_EVENT_TYPES = frozenset(
    {
        "completed",
        "failed",
        "waiting_approval",
        "waiting_input",
        "recovery_required",
    }
)
_SECRET_ENV_NAME = re.compile(r"^AGENTHICC_WEBHOOK_[A-Za-z0-9_]{1,240}$")


@dataclass(frozen=True)
class WebhookDestinationSettings:
    """One explicitly configured HTTPS delivery target.

    Secrets are referenced by environment-variable name only; secret values
    are resolved by the dispatcher immediately before signing a request.
    """

    name: str
    url: str
    secret_env: str
    events: tuple[str, ...] = tuple(sorted(NOTIFICATION_EVENT_TYPES))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], *, index: int) -> WebhookDestinationSettings:
        allowed = {"name", "url", "secret_env", "events"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"background.notifications.webhooks[{index}] has unknown fields: "
                f"{', '.join(sorted(str(item) for item in unknown))}"
            )
        name = raw.get("name")
        url = raw.get("url")
        secret_env = raw.get("secret_env")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError(
                f"background.notifications.webhooks[{index}].name must be a non-empty string up to 128 characters"
            )
        if name.strip().casefold() == "local":
            raise ValueError(f"background.notifications.webhooks[{index}].name 'local' is reserved")
        if not isinstance(url, str) or len(url) > 2_048:
            raise ValueError(
                f"background.notifications.webhooks[{index}].url must be a string up to 2048 characters"
            )
        if not isinstance(secret_env, str) or not _SECRET_ENV_NAME.fullmatch(secret_env):
            raise ValueError(
                f"background.notifications.webhooks[{index}].secret_env must be an environment-variable name"
            )
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                f"background.notifications.webhooks[{index}].url has an invalid host or port"
            ) from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65_535)
        ):
            raise ValueError(
                f"background.notifications.webhooks[{index}].url must be HTTPS without credentials, query, or fragment"
            )
        raw_events = raw.get("events", tuple(sorted(NOTIFICATION_EVENT_TYPES)))
        if not isinstance(raw_events, (list, tuple)) or not raw_events:
            raise ValueError(
                f"background.notifications.webhooks[{index}].events must be a non-empty list"
            )
        if not all(isinstance(item, str) for item in raw_events):
            raise ValueError(
                f"background.notifications.webhooks[{index}].events must contain strings"
            )
        events = tuple(dict.fromkeys(item.strip() for item in raw_events))
        unknown_events = set(events) - NOTIFICATION_EVENT_TYPES
        if unknown_events:
            raise ValueError(
                f"background.notifications.webhooks[{index}].events contains unsupported events: "
                f"{', '.join(sorted(unknown_events))}"
            )
        return cls(name=name.strip(), url=url, secret_env=secret_env, events=events)


@dataclass(frozen=True)
class BackgroundNotificationSettings:
    """Opt-in local and webhook notification policy."""

    local_enabled: bool = False
    local_backend: str = "auto"
    webhooks: tuple[WebhookDestinationSettings, ...] = ()
    allow_private_network: bool = False
    request_timeout_s: float = 5.0
    max_attempts: int = 5
    retry_base_delay_s: float = 1.0
    retry_max_delay_s: float = 300.0
    retry_rate_limits: bool = False

    @property
    def enabled(self) -> bool:
        """Whether at least one local or remote notification is requested."""

        return self.local_enabled or bool(self.webhooks)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> BackgroundNotificationSettings:
        allowed = {
            "local_enabled",
            "local_backend",
            "webhooks",
            "allow_private_network",
            "request_timeout_s",
            "max_attempts",
            "retry_base_delay_s",
            "retry_max_delay_s",
            "retry_rate_limits",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                "background.notifications has unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        local_enabled = raw.get("local_enabled", False)
        allow_private = raw.get("allow_private_network", False)
        retry_rate_limits = raw.get("retry_rate_limits", False)
        if not isinstance(local_enabled, bool):
            raise ValueError("background.notifications.local_enabled must be a boolean")
        if not isinstance(allow_private, bool):
            raise ValueError("background.notifications.allow_private_network must be a boolean")
        if not isinstance(retry_rate_limits, bool):
            raise ValueError("background.notifications.retry_rate_limits must be a boolean")
        backend = raw.get("local_backend", "auto")
        if not isinstance(backend, str) or backend not in {"auto", "desktop", "terminal"}:
            raise ValueError(
                "background.notifications.local_backend must be auto, desktop, or terminal"
            )

        def finite_number(name: str, default: float, minimum: float, maximum: float) -> float:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"background.notifications.{name} must be a number")
            result = float(value)
            if not math.isfinite(result) or not minimum <= result <= maximum:
                raise ValueError(
                    f"background.notifications.{name} must be finite and between {minimum} and {maximum}"
                )
            return result

        max_attempts = raw.get("max_attempts", 5)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("background.notifications.max_attempts must be between 1 and 20")
        raw_webhooks = raw.get("webhooks", ())
        if not isinstance(raw_webhooks, (list, tuple)):
            raise ValueError("background.notifications.webhooks must be an array of tables")
        webhooks: list[WebhookDestinationSettings] = []
        names: set[str] = set()
        for index, destination in enumerate(raw_webhooks):
            if not isinstance(destination, Mapping):
                raise ValueError(f"background.notifications.webhooks[{index}] must be a table")
            item = WebhookDestinationSettings.from_mapping(destination, index=index)
            folded_name = item.name.casefold()
            if folded_name in names:
                raise ValueError("background.notifications.webhook names must be unique")
            names.add(folded_name)
            webhooks.append(item)
            if len(webhooks) > 64:
                raise ValueError("background.notifications.webhooks supports at most 64 targets")
        retry_base_delay_s = finite_number("retry_base_delay_s", 1.0, 0.1, 3_600.0)
        retry_max_delay_s = finite_number("retry_max_delay_s", 300.0, 0.1, 86_400.0)
        if retry_max_delay_s < retry_base_delay_s:
            raise ValueError(
                "background.notifications.retry_max_delay_s must be at least retry_base_delay_s"
            )
        return cls(
            local_enabled=local_enabled,
            local_backend=str(backend),
            webhooks=tuple(webhooks),
            allow_private_network=allow_private,
            request_timeout_s=finite_number("request_timeout_s", 5.0, 0.1, 60.0),
            max_attempts=max_attempts,
            retry_base_delay_s=retry_base_delay_s,
            retry_max_delay_s=retry_max_delay_s,
            retry_rate_limits=retry_rate_limits,
        )


@dataclass(frozen=True)
class AgentsHubSettings:
    """Latency and resource limits for the agents hub (``[background.manager]``).

    The hub is event-driven (AGX-04). ``refresh_interval_s`` is how often
    it stats the files it watches when native change notification is
    unavailable; ``maintenance_interval_s`` is how often it probes a worker
    whose exit it cannot watch and, without inotify, rescans the input inbox.
    """

    refresh_interval_s: float = 0.25
    maintenance_interval_s: float = 5.0
    activity_tail_bytes: int = 64_000
    frame_debounce_ms: int = 16
    max_in_flight_operations: int = 4
    metrics: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "AgentsHubSettings":
        def number(name: str, default: float, minimum: float, maximum: float) -> float:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"background.manager.{name} must be a number")
            result = float(value)
            if not math.isfinite(result) or not minimum <= result <= maximum:
                raise ValueError(
                    f"background.manager.{name} must be finite and between {minimum} and {maximum}"
                )
            return result

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            value = raw.get(name, default)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(
                    f"background.manager.{name} must be an integer between {minimum} and {maximum}"
                )
            return value

        metrics = raw.get("metrics", False)
        if not isinstance(metrics, bool):
            raise ValueError("background.manager.metrics must be a boolean")
        return cls(
            refresh_interval_s=number("refresh_interval_s", 0.25, 0.05, 60.0),
            maintenance_interval_s=number("maintenance_interval_s", 5.0, 0.5, 3_600.0),
            activity_tail_bytes=integer("activity_tail_bytes", 64_000, 1_024, 4_000_000),
            frame_debounce_ms=integer("frame_debounce_ms", 16, 0, 1_000),
            max_in_flight_operations=integer("max_in_flight_operations", 4, 1, 64),
            metrics=metrics,
        )


@dataclass(frozen=True)
class BackgroundSettings:
    """Validated resource and retention limits for background sessions."""

    enabled: bool = True
    store_path: str = ""
    max_workers: int = 4
    max_workers_per_project: int = 4
    cancel_grace_s: float = 5.0
    stale_after_s: float = 30.0
    wall_timeout_s: float = 0.0
    max_activity_bytes: int = 64_000
    trash_retention_days: int = 30
    terminals_enabled: bool = True
    max_terminals: int = 4
    max_terminals_per_project: int = 8
    terminal_max_output_bytes: int = 64_000
    terminal_wall_timeout_s: float = 0.0
    terminal_cancel_grace_s: float = 5.0
    terminal_retention_days: int = 30
    manager: AgentsHubSettings = AgentsHubSettings()
    # PRD-225 is opt-in while the durable scheduler receives broader rollout.
    queue_enabled: bool = False
    max_queue_length: int = 100
    max_workers_per_user: int | None = None
    priority_aging_interval_s: float = 900.0
    dispatch_lease_s: float = 30.0
    notifications: BackgroundNotificationSettings = BackgroundNotificationSettings()

    @property
    def store_root(self) -> Path | None:
        """The configured store directory, or ``None`` for the default location."""
        return Path(self.store_path).expanduser() if self.store_path else None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "BackgroundSettings":
        """Build settings and reject unsafe or ambiguous values."""

        def positive_int(
            name: str,
            default: int,
            *,
            allow_zero: bool = False,
            maximum: int = 100_000_000,
        ) -> int:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"background.{name} must be an integer")
            if value < 0 or (value == 0 and not allow_zero) or value > maximum:
                raise ValueError(
                    f"background.{name} must be {'non-negative' if allow_zero else 'positive'} "
                    f"and no greater than {maximum}"
                )
            return value

        def nonnegative_float(name: str, default: float) -> float:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"background.{name} must be a number")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"background.{name} must be finite and non-negative")
            return number

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("background.enabled must be a boolean")
        store_path = raw.get("store_path", "")
        if not isinstance(store_path, str):
            raise ValueError("background.store_path must be a string")
        terminals_enabled = raw.get("terminals_enabled", True)
        if not isinstance(terminals_enabled, bool):
            raise ValueError("background.terminals_enabled must be a boolean")
        queue_enabled = raw.get("queue_enabled", False)
        if not isinstance(queue_enabled, bool):
            raise ValueError("background.queue_enabled must be a boolean")
        max_workers_per_user_raw = raw.get("max_workers_per_user")
        if max_workers_per_user_raw is None:
            max_workers_per_user = None
        elif (
            isinstance(max_workers_per_user_raw, bool)
            or not isinstance(max_workers_per_user_raw, int)
            or not 1 <= max_workers_per_user_raw <= 1_024
        ):
            raise ValueError(
                "background.max_workers_per_user must be an integer between 1 and 1024"
            )
        else:
            max_workers_per_user = max_workers_per_user_raw
        manager_raw = raw.get("manager", {})
        if not isinstance(manager_raw, Mapping):
            raise ValueError("background.manager must be a table")
        notifications_raw = raw.get("notifications", {})
        if not isinstance(notifications_raw, Mapping):
            raise ValueError("background.notifications must be a table")
        priority_aging_interval_s = nonnegative_float("priority_aging_interval_s", 900.0)
        if not 1.0 <= priority_aging_interval_s <= 2_678_400.0:
            raise ValueError(
                "background.priority_aging_interval_s must be between 1 and 2678400 seconds"
            )
        dispatch_lease_s = nonnegative_float("dispatch_lease_s", 30.0)
        if not 0.1 <= dispatch_lease_s <= 3_600.0:
            raise ValueError("background.dispatch_lease_s must be between 0.1 and 3600 seconds")
        return cls(
            enabled=enabled,
            store_path=store_path,
            max_workers=positive_int("max_workers", 4, maximum=1_024),
            max_workers_per_project=positive_int("max_workers_per_project", 4, maximum=1_024),
            cancel_grace_s=nonnegative_float("cancel_grace_s", 5.0),
            stale_after_s=nonnegative_float("stale_after_s", 30.0),
            wall_timeout_s=nonnegative_float("wall_timeout_s", 0.0),
            max_activity_bytes=positive_int("max_activity_bytes", 64_000),
            trash_retention_days=positive_int("trash_retention_days", 30, allow_zero=True),
            terminals_enabled=terminals_enabled,
            max_terminals=positive_int("max_terminals", 4),
            max_terminals_per_project=positive_int("max_terminals_per_project", 8),
            terminal_max_output_bytes=positive_int("terminal_max_output_bytes", 64_000),
            terminal_wall_timeout_s=nonnegative_float("terminal_wall_timeout_s", 0.0),
            terminal_cancel_grace_s=nonnegative_float("terminal_cancel_grace_s", 5.0),
            terminal_retention_days=positive_int("terminal_retention_days", 30, allow_zero=True),
            manager=AgentsHubSettings.from_mapping(manager_raw),
            queue_enabled=queue_enabled,
            max_queue_length=positive_int("max_queue_length", 100, maximum=100_000),
            max_workers_per_user=max_workers_per_user,
            priority_aging_interval_s=priority_aging_interval_s,
            dispatch_lease_s=dispatch_lease_s,
            notifications=BackgroundNotificationSettings.from_mapping(notifications_raw),
        )


def _config_candidates(cwd: Path) -> tuple[Path, ...]:
    return (
        Path.home() / ".agenthicc" / "agenthicc.toml",
        Path.home() / ".agenthicc" / ".agenthicc.toml",
        cwd / ".agenthicc" / "agenthicc.toml",
        cwd / ".agenthicc" / ".agenthicc.toml",
        cwd / "agenthicc.toml",
        cwd / ".agenthicc.toml",
    )


def _read_section(path: Path) -> dict[str, object]:
    try:
        loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = loaded.get("background", {})
    return dict(section) if isinstance(section, dict) else {}


def _override_value(raw: str) -> object:
    try:
        parsed = tomllib.loads(f"value = {raw}")
    except tomllib.TOMLDecodeError:
        return raw
    return parsed.get("value", raw)


def load_background_settings(
    *,
    config_path: str | None = None,
    overrides: tuple[str, ...] = (),
    cwd: Path | None = None,
    config: object | None = None,
) -> BackgroundSettings:
    """Load local/global ``[background]`` settings with CLI overrides.

    ``config`` is accepted for forward compatibility with a future typed
    ``AgenthiccConfig.background`` section. A typed ``BackgroundSettings``
    instance, when supplied by that section, takes precedence.
    """

    working_dir = (cwd or Path.cwd()).resolve()
    raw: dict[str, object] = {}
    paths = (Path(config_path).expanduser(),) if config_path else _config_candidates(working_dir)
    global_paths = {
        Path.home() / ".agenthicc" / "agenthicc.toml",
        Path.home() / ".agenthicc" / ".agenthicc.toml",
    }
    trusted_notification_settings: Mapping[str, object] | None = None
    for path in paths:
        if path.exists():
            section = _read_section(path)
            if path in global_paths or config_path is not None:
                notification_section = section.get("notifications")
                if isinstance(notification_section, Mapping):
                    trusted_notification_settings = notification_section
            raw.update(section)
    for item in overrides:
        if not item.startswith("background.") or "=" not in item:
            continue
        name, value = item[len("background.") :].split("=", 1)
        components = name.strip().split(".")
        if len(components) == 2 and components[0] == "manager":
            manager_raw = raw.get("manager", {})
            nested = dict(manager_raw) if isinstance(manager_raw, Mapping) else {}
            nested[components[1]] = _override_value(value.strip())
            raw["manager"] = nested
        elif len(components) == 2 and components[0] == "notifications":
            notifications_raw = raw.get("notifications", {})
            nested = dict(notifications_raw) if isinstance(notifications_raw, Mapping) else {}
            nested[components[1]] = _override_value(value.strip())
            raw["notifications"] = nested
        elif len(components) == 1:
            raw[components[0]] = _override_value(value.strip())
    notifications_raw = raw.get("notifications", {})
    if isinstance(notifications_raw, Mapping):
        notification_settings = dict(notifications_raw)
        # Webhook endpoints can cause environment-backed secrets to leave the
        # machine. Load them only from operator-owned global config, or from
        # an explicitly selected config file; repository config is untrusted.
        notification_settings["webhooks"] = (
            trusted_notification_settings.get("webhooks", ())
            if trusted_notification_settings is not None
            else ()
        )
        # Repository-local settings cannot grant themselves access to private
        # or loopback services. The user-global file is the administrator
        # policy; an explicitly selected config file is also an operator opt-in.
        notification_settings["allow_private_network"] = (
            trusted_notification_settings is not None
            and trusted_notification_settings.get("allow_private_network") is True
        )
        raw["notifications"] = notification_settings
    settings = BackgroundSettings.from_mapping(raw)
    return config if isinstance(config, BackgroundSettings) else settings


def background_enabled(settings: BackgroundSettings) -> bool:
    """Return whether background execution is enabled for this invocation."""

    return settings.enabled and os.environ.get("AGENTHICC_DISABLE_BACKGROUND", "") != "1"
