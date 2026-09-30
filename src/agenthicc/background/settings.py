"""Configuration for the local background-session supervisor.

Background settings are read at the supervisor boundary so older config files
remain valid.  The section is intentionally small, local-only, and validated
before it can affect worker creation.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class BackgroundManagerSettings:
    """Latency and resource limits for the interactive agents manager."""

    refresh_interval_s: float = 0.25
    maintenance_interval_s: float = 5.0
    projection_batch_size: int = 256
    activity_tail_bytes: int = 64_000
    frame_debounce_ms: int = 16
    max_in_flight_operations: int = 4
    metrics: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "BackgroundManagerSettings":
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
            projection_batch_size=integer("projection_batch_size", 256, 1, 10_000),
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
    max_workers: int = 2
    max_workers_per_project: int = 2
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
    manager: BackgroundManagerSettings = BackgroundManagerSettings()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "BackgroundSettings":
        """Build settings and reject unsafe or ambiguous values."""

        def positive_int(name: str, default: int, *, allow_zero: bool = False) -> int:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"background.{name} must be an integer")
            if value < 0 or (value == 0 and not allow_zero):
                raise ValueError(
                    f"background.{name} must be {'non-negative' if allow_zero else 'positive'}"
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
        manager_raw = raw.get("manager", {})
        if not isinstance(manager_raw, Mapping):
            raise ValueError("background.manager must be a table")
        return cls(
            enabled=enabled,
            store_path=store_path,
            max_workers=positive_int("max_workers", 2),
            max_workers_per_project=positive_int("max_workers_per_project", 2),
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
            manager=BackgroundManagerSettings.from_mapping(manager_raw),
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
    for path in paths:
        if path.exists():
            raw.update(_read_section(path))
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
        elif len(components) == 1:
            raw[components[0]] = _override_value(value.strip())
    settings = BackgroundSettings.from_mapping(raw)
    return config if isinstance(config, BackgroundSettings) else settings


def background_enabled(settings: BackgroundSettings) -> bool:
    """Return whether background execution is enabled for this invocation."""

    return settings.enabled and os.environ.get("AGENTHICC_DISABLE_BACKGROUND", "") != "1"
