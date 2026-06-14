from __future__ import annotations

# Spinner frames for thinking indicator (8 braille frames)
SPINNER_FRAMES: list[str] = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]

# Tool call state symbols
TOOL_PENDING  = "○"
TOOL_RUNNING  = "◉"
TOOL_SUCCESS  = "✓"
TOOL_ERROR    = "✗"
TOOL_APPROVAL = "⚑"

# Agent role indicators
AGENT_BULLET  = "●"
USER_BULLET   = "❯"

# Box-drawing for dividers
DIVIDER_CHAR   = "─"
DOUBLE_DIVIDER = "═"

# Mode symbols dict: mode_name -> symbol
MODE_SYMBOLS: dict[str, str] = {
    "Auto":   "◆",
    "Plan":   "📋",
    "Ask":    "❓",
    "Review": "👁",
    "Safe":   "🔒",
    "Debug":  "🐛",
}

MODE_COLORS: dict[str, str] = {
    "Auto":   "bright_blue",
    "Plan":   "bright_yellow",
    "Ask":    "bright_cyan",
    "Review": "bright_magenta",
    "Safe":   "bright_red",
    "Debug":  "bright_green",
}

# Agent colors (cycle through these for multi-agent sessions)
AGENT_COLORS: list[str] = [
    "bright_blue",
    "bright_green",
    "bright_yellow",
    "bright_magenta",
    "bright_cyan",
    "bright_red",
]


def _unicode_safe(preferred: str, ascii_fallback: str) -> str:
    """Return preferred if terminal supports unicode, else ascii_fallback."""
    import os as _os
    lang = _os.environ.get("LANG", "") + _os.environ.get("LC_ALL", "")
    if "utf" in lang.lower() or "UTF" in lang:
        return preferred
    return ascii_fallback
