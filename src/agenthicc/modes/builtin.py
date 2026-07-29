"""Legacy-mode compatibility definitions.

The TUI runtime owns the canonical permission policy. These ``Mode`` objects
remain available to downstream mode plugins and legacy imports, but expose the
same three product identities: Safe, Plan, and Yolo.
"""

from __future__ import annotations

from .mode import Mode
from .registry import ModeRegistry

__all__ = ["build_default_registry", "BUILTIN_MODES"]

_PLAN_ALLOWED: frozenset[str] = frozenset(
    {
        "read_file",
        "read_lines",
        "list_directory",
        "list_files",
        "search_files",
        "grep_files",
        "get_file_info",
        "file_exists",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_blame",
        "git_grep",
        "git_branch",
    }
)


def _plan_filter(tool_name: str, kwargs: dict[str, object]) -> bool:
    """Retain the legacy Plan allowlist for callers outside the runtime gate."""
    return tool_name in _PLAN_ALLOWED


_PLAN_PATCH = (
    "## PLAN MODE\n"
    "You are operating in PLAN MODE. In this mode you MUST NOT write any files,\n"
    "execute any commands, or make any changes to the filesystem or repository.\n"
    "Analyse the request and produce a structured, step-by-step action plan only."
)

_SAFE_PATCH = (
    "## SAFE MODE\n"
    "Read, search, and git-read tools are available directly. Any tool that can "
    "write, execute, modify git, or access the network requires explicit user approval."
)


def build_default_registry() -> ModeRegistry:
    """Return the legacy adapter registry in Safe → Plan → Yolo order."""
    registry = ModeRegistry(
        aliases={"Auto": "Yolo", "Guard": "Safe", "Ask": "Safe", "Review": "Plan"}
    )
    registry.register(
        Mode(
            name="Safe",
            label="⊘",
            description="Side-effecting actions require explicit approval.",
            colour="red",
            system_patch=_SAFE_PATCH,
            tool_filter=None,
            source_id="builtin",
        )
    )
    registry.register(
        Mode(
            name="Plan",
            label="◈",
            description="Read-only planning; side effects are hard-blocked.",
            colour="yellow",
            system_patch=_PLAN_PATCH,
            tool_filter=_plan_filter,
            source_id="builtin",
        )
    )
    registry.register(
        Mode(
            name="Yolo",
            label="⏵⏵",
            description="Full automatic mode — all tools allowed, no prompt patch.",
            colour="green",
            source_id="builtin",
        )
    )
    return registry


BUILTIN_MODES: list[Mode] = list(build_default_registry())
