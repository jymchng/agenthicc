"""CLIContext and CLIFlags — typed CLI state injected into command handlers (PRD-79)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CLIFlags:
    """Ephemeral security and behaviour flags — intentionally NOT settable in TOML.

    These must be typed explicitly on every invocation; there is no path for
    silent persistence.  Runtime components read them via AppState.cli_flags.
    """

    dangerously_skip_permissions: bool = False
    # future: dry_run, offline, no_telemetry …


@dataclass(frozen=True)
class CLIContext:
    """All context needed for a single CLI invocation.

    Injected into command handlers by annotation type (not by parameter name).
    """

    resume_id: str | None = None
    headless: bool = False
    config_path: str | None = None
    set_overrides: tuple[str, ...] = ()
    flags: CLIFlags = field(default_factory=CLIFlags)
    record_cassette: str | None = None
    continue_session: bool = False
