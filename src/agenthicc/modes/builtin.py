"""Built-in mode definitions for agenthicc (6 default modes).

The six modes are: Auto, Plan, Ask, Review, Safe, Debug.

``build_default_registry()`` returns a :class:`~agenthicc.modes.ModeRegistry`
pre-populated with all six modes in canonical cycle order.
"""
from __future__ import annotations

from typing import Any

from .mode import Mode
from .registry import ModeRegistry

__all__ = ["build_default_registry", "BUILTIN_MODES"]

# ---------------------------------------------------------------------------
# Tool filters — allowlist-based for deterministic, predictable restriction
# ---------------------------------------------------------------------------

# Tools permitted in Plan and Review modes: read/inspect tools + non-destructive git.
_PLAN_REVIEW_ALLOWED: frozenset[str] = frozenset({
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
})

# Tools permitted in Safe mode: narrower read-only set (no git blame/grep).
_SAFE_ALLOWED: frozenset[str] = frozenset({
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
})


def _plan_filter(tool_name: str, kwargs: dict[str, Any]) -> bool:
    """Allow only read/inspect tools and non-destructive git commands."""
    return tool_name in _PLAN_REVIEW_ALLOWED


def _review_filter(tool_name: str, kwargs: dict[str, Any]) -> bool:
    """Allow only read/inspect and diff tools (same set as Plan)."""
    return tool_name in _PLAN_REVIEW_ALLOWED


def _safe_filter(tool_name: str, kwargs: dict[str, Any]) -> bool:
    """Allow only a conservative read-only set (no git blame/grep)."""
    return tool_name in _SAFE_ALLOWED


# ---------------------------------------------------------------------------
# Post-hook for Debug mode
# ---------------------------------------------------------------------------

def _debug_post_hook(response: str, context: Any) -> str:
    """Append a DEBUG footer with timing and token metadata to every agent response.

    Reads ``elapsed``, ``tokens_in``, ``tokens_out``, and ``cost`` from
    ``context._status`` (or *context* itself) via :func:`getattr` — never
    raises even when the context is a stub or ``None``.
    """
    status = getattr(context, "_status", context)
    elapsed = getattr(status, "elapsed", None)
    tokens_in = getattr(status, "tokens_in", None)
    tokens_out = getattr(status, "tokens_out", None)
    cost = getattr(status, "cost", None)

    elapsed_str = f"{elapsed:.1f}" if isinstance(elapsed, (int, float)) else "?"
    in_str = str(tokens_in) if tokens_in is not None else "?"
    out_str = str(tokens_out) if tokens_out is not None else "?"

    if isinstance(cost, (int, float)):
        cost_str = f"0.{int(cost * 10000):04d}"
    else:
        cost_str = "0.XXXX"

    debug_block = (
        f"\n\n```\n"
        f"[DEBUG] elapsed={elapsed_str}s  in={in_str} out={out_str}  cost={cost_str}\n"
        f"```"
    )
    return response + debug_block


# ---------------------------------------------------------------------------
# System patches
# ---------------------------------------------------------------------------

_PLAN_PATCH = (
    "## PLAN MODE\n"
    "You are operating in PLAN MODE. In this mode you MUST NOT write any files,\n"
    "execute any commands, or make any changes to the filesystem or repository.\n"
    "Your task is to analyse the request and produce a structured, step-by-step\n"
    "action plan only."
)

_ASK_PATCH = (
    "## ASK MODE\n"
    "You are operating in ASK MODE. Ask clarifying questions and gather all\n"
    "necessary information before attempting any work. Do not take actions;\n"
    "only ask questions and explain what you would need."
)

_REVIEW_PATCH = (
    "## REVIEW MODE\n"
    "You are operating in REVIEW MODE. Inspect code and diffs and provide\n"
    "feedback. Do not make changes to files or run commands."
)

_SAFE_PATCH = (
    "## SAFE MODE\n"
    "You are operating in SAFE MODE. You may only read files and browse the\n"
    "repository. No writes, no command execution."
)

_DEBUG_PATCH = (
    "## DEBUG MODE\n"
    "You are operating in DEBUG MODE. Full tool access is available. All\n"
    "responses will include a debug footer with diagnostic information."
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_default_registry() -> ModeRegistry:
    """Return a :class:`ModeRegistry` with the 6 built-in modes.

    Cycle order: Auto → Plan → Ask → Review → Safe → Debug → Auto
    """
    registry = ModeRegistry()

    registry.register(Mode(
        name="Auto",
        label="⏵⏵",
        description="Full automatic mode — all tools allowed, no prompt patch.",
        colour="green",
        system_patch="",
        tool_filter=None,
        source_id="builtin",
    ))

    registry.register(Mode(
        name="Plan",
        label="◈",
        description="Planning only — write and exec tools blocked; produce action plans.",
        colour="yellow",
        system_patch=_PLAN_PATCH,
        tool_filter=_plan_filter,
        source_id="builtin",
    ))

    registry.register(Mode(
        name="Safe",
        label="⊘",
        description="Safe mode — read-only; all writes and exec tools blocked.",
        colour="red",
        system_patch=_SAFE_PATCH,
        tool_filter=_safe_filter,
        source_id="builtin",
    ))

    return registry


# ---------------------------------------------------------------------------
# Flat list of built-in Mode instances (for direct import)
# ---------------------------------------------------------------------------

BUILTIN_MODES: list[Mode] = list(build_default_registry())
