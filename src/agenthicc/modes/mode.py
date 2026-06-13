"""Core Mode dataclass and associated type aliases for the agenthicc modes system."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["Mode", "ToolFilter", "ModeHook"]

# A callable that receives (tool_name, tool_kwargs) and returns True if the tool
# should be allowed for this mode.
ToolFilter = Callable[[str, dict[str, Any]], bool]

# A callable that receives (prompt_text, renderer_or_context) and returns a
# (possibly modified) string. Used for pre/post hooks around agent calls.
ModeHook = Callable[[str, Any], str]

_COLOUR_MAP: dict[str, str] = {
    "white": "\x1b[37m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "cyan": "\x1b[36m",
    "blue": "\x1b[34m",
    "red": "\x1b[31m",
    "magenta": "\x1b[35m",
}


@dataclass
class Mode:
    """A named operating mode that shapes how the agent behaves.

    Attributes
    ----------
    name:
        Unique machine-readable identifier (e.g. ``"Auto"``).
    label:
        Short human-readable label shown in the TUI badge (e.g. ``"AUTO"``).
    description:
        One-sentence explanation shown in help / mode-picker UI.
    colour:
        ANSI colour name for the badge; one of white, green, yellow, cyan,
        blue, red, magenta.
    system_patch:
        Text prepended to the base system prompt when this mode is active.
        An empty string means no patch is applied.
    tool_filter:
        Optional callable ``(tool_name, tool_kwargs) -> bool``.  When set,
        only tools for which the filter returns ``True`` are exposed to the
        agent.  ``None`` means all tools are allowed.
    pre_hook:
        Optional callable ``(prompt_text, context) -> str`` called before each
        agent invocation.  May transform the prompt.
    post_hook:
        Optional callable ``(response_text, context) -> str`` called after each
        agent invocation.  May transform or annotate the response.
    source_id:
        Identifies where this mode came from; ``"builtin"`` for built-ins,
        ``"mode-plugin:<stem>"`` for file-loaded plugins.
    shortcut_hint:
        Optional keyboard shortcut hint displayed in the TUI (e.g. ``"Ctrl-P"``).
    """

    name: str
    label: str
    description: str
    colour: str = "white"
    system_patch: str = ""
    tool_filter: ToolFilter | None = None
    pre_hook: ModeHook | None = None
    post_hook: ModeHook | None = None
    source_id: str = "builtin"
    shortcut_hint: str = ""

    @property
    def badge(self) -> str:
        """Return an ANSI-coloured badge string, e.g. ``\\x1b[32m[PLAN]\\x1b[0m``."""
        ansi = _COLOUR_MAP.get(self.colour, "\x1b[37m")
        return f"{ansi}[{self.label}]\x1b[0m"
