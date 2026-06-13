---
title: "PRD-47: Mode System Architecture — Agent Operational Modes"
status: draft
version: 0.1.0
created: 2026-06-13
---

# PRD-47: Mode System Architecture

## Executive Summary

Modes are named operational contexts that change how the AI agent behaves:
which tools it can call, what system-prompt instructions it receives, and how
it presents its output.  The user cycles through modes with **Shift+Tab** and
the current mode is shown as a badge in the input bar and status line.

The default mode is **Auto** (full permissions, standard behaviour).  Built-in
modes (PRD-48) cover the most common workflows.  User-defined mode plugins
(PRD-50) extend the set for project-specific needs.

---

## Goals

| ID | Goal |
|----|------|
| G1 | A `Mode` object encapsulates everything that changes between modes: system prompt patch, tool filter, display label, colour |
| G2 | `ModeRegistry` is the single source of truth; modes are registered at startup |
| G3 | `ModeManager` tracks the active mode and cycles through registered modes |
| G4 | Shift+Tab cycles forward through all modes; wraps back to the first |
| G5 | The active mode is visible in the input bar prompt and status line at all times |
| G6 | `_run_agent_turn()` reads the active mode and applies its system-prompt patch and tool filter |
| G7 | Any mode can opt-in to a **pre-flight hook** that runs before the first tool call |
| G8 | Any mode can opt-in to a **post-flight hook** that runs after the agent responds |
| G9 | Modes cannot conflict — activating one deactivates all others |
| G10 | `renderer._active_mode` is the live reference read by all subsystems |

## Non-Goals
- Multiple simultaneous modes
- Per-agent-turn mode (mode is a session-level setting)
- Mode-specific memory or session isolation

---

## `Mode` Dataclass

```python
# src/agenthicc/modes/mode.py

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["Mode", "ToolFilter", "ModeHook"]

# (tool_name: str, ctx: dict) -> bool — True allows the tool call
ToolFilter = Callable[[str, dict], bool]

# (text: str, renderer: Any) -> str — may transform text; return unchanged to pass through
ModeHook = Callable[[str, Any], str]


@dataclass
class Mode:
    """Complete specification of an operational mode."""

    name: str           # e.g. "Auto", "Plan", "Review"
    label: str          # short display label, e.g. "AUTO", "PLAN"
    description: str    # shown in the mode picker / /help
    colour: str = "white"       # ANSI colour name or #rrggbb for the badge

    # System-prompt patch applied ON TOP of the base system prompt.
    # Prepended when non-empty.
    system_patch: str = ""

    # Optional tool filter.  None = allow everything.
    tool_filter: ToolFilter | None = None

    # Optional pre-flight hook: called with (user_text, renderer) before
    # the agent runs.  May modify or annotate the text.
    pre_hook: ModeHook | None = None

    # Optional post-flight hook: called with (agent_response, renderer)
    # after the agent responds.  May add summaries, warnings, etc.
    post_hook: ModeHook | None = None

    # Source identifier for plugin modes (mirrors PRD-45 source_id pattern).
    source_id: str = "builtin"

    # User-facing keyboard hint shown in the status bar.
    # Leave empty to use the default "Shift+Tab to switch".
    shortcut_hint: str = ""

    @property
    def badge(self) -> str:
        """ANSI-coloured badge for display in the prompt / status bar."""
        colour_map = {
            "white":  "\x1b[37m",
            "green":  "\x1b[32m",
            "yellow": "\x1b[33m",
            "cyan":   "\x1b[36m",
            "blue":   "\x1b[34m",
            "red":    "\x1b[31m",
            "magenta":"\x1b[35m",
        }
        ansi = colour_map.get(self.colour, "\x1b[37m")
        return f"{ansi}[{self.label}]\x1b[0m"
```

---

## `ModeRegistry`

```python
# src/agenthicc/modes/registry.py

from __future__ import annotations

from .mode import Mode

__all__ = ["ModeRegistry"]


class ModeRegistry:
    """Ordered registry of all available modes.

    Modes are stored in registration order; cycling with Shift+Tab
    moves to the next mode in that order, wrapping around.
    """

    def __init__(self) -> None:
        self._modes: list[Mode] = []
        self._by_name: dict[str, Mode] = {}

    def register(self, mode: Mode) -> None:
        if mode.name in self._by_name:
            # Replace existing (allows plugin override of builtins).
            idx = next(i for i, m in enumerate(self._modes) if m.name == mode.name)
            self._modes[idx] = mode
        else:
            self._modes.append(mode)
        self._by_name[mode.name] = mode

    def register_many(self, modes: list[Mode]) -> None:
        for mode in modes:
            self.register(mode)

    def unregister_source(self, source_id: str) -> int:
        before = len(self._modes)
        self._modes = [m for m in self._modes if m.source_id != source_id]
        self._by_name = {m.name: m for m in self._modes}
        return before - len(self._modes)

    def get(self, name: str) -> Mode | None:
        return self._by_name.get(name)

    def all_modes(self) -> list[Mode]:
        return list(self._modes)

    def next_after(self, current_name: str) -> Mode:
        """Return the mode after *current_name*, wrapping around."""
        if not self._modes:
            raise ValueError("ModeRegistry is empty")
        names = [m.name for m in self._modes]
        try:
            idx = names.index(current_name)
            return self._modes[(idx + 1) % len(self._modes)]
        except ValueError:
            return self._modes[0]

    def __len__(self) -> int:
        return len(self._modes)

    def __iter__(self):
        return iter(self._modes)
```

---

## `ModeManager`

```python
# src/agenthicc/modes/manager.py

from __future__ import annotations

from .mode import Mode
from .registry import ModeRegistry

__all__ = ["ModeManager"]


class ModeManager:
    """Tracks the active mode and handles Shift+Tab cycling."""

    def __init__(self, registry: ModeRegistry, default_name: str = "Auto") -> None:
        self._registry = registry
        default = registry.get(default_name) or (registry.all_modes()[0] if registry.all_modes() else None)
        self._active: Mode = default  # type: ignore[assignment]

    @property
    def active(self) -> Mode:
        return self._active

    @property
    def active_name(self) -> str:
        return self._active.name if self._active else "Auto"

    def cycle(self) -> Mode:
        """Advance to the next mode (called on Shift+Tab). Returns the new mode."""
        self._active = self._registry.next_after(self.active_name)
        return self._active

    def set(self, name: str) -> Mode | None:
        """Switch directly to a named mode. Returns None if not found."""
        mode = self._registry.get(name)
        if mode:
            self._active = mode
        return mode

    def apply_to_agent(
        self,
        base_system: str,
        registry_tools: list,
    ) -> tuple[str, list]:
        """Return (effective_system_prompt, filtered_tools) for the active mode."""
        mode = self._active
        if not mode:
            return base_system, registry_tools

        # Prepend system-prompt patch.
        system = base_system
        if mode.system_patch:
            system = mode.system_patch.rstrip() + "\n\n" + system

        # Apply tool filter.
        tools = registry_tools
        if mode.tool_filter:
            tools = [t for t in registry_tools if mode.tool_filter(getattr(t, "__name__", ""), {})]

        return system, tools
```

---

## Integration in `_run_agent_turn()`

Two hooks are added to `__main__.py`:

```python
# At the top of _run_agent_turn(), after building the registry:

mode_manager: ModeManager = getattr(renderer, "_mode_manager", None)
active_mode = mode_manager.active if mode_manager else None

# 1. Pre-flight hook — may annotate or modify the user text.
if active_mode and active_mode.pre_hook:
    text = active_mode.pre_hook(text, renderer)

# 2. Apply system prompt patch + tool filter.
BASE_SYSTEM = (
    "You are a capable AI assistant with access to filesystem, shell, "
    "and git tools. Use them directly to complete tasks. "
    "Give concise responses. Show command output when relevant. "
    "Never invent file contents — always read them first."
    + (_skill_suffix if _skill_suffix else "")
    + (f"\n\n{_tool_description}" if _tool_description else "")
)

if mode_manager:
    effective_system, effective_tools = mode_manager.apply_to_agent(
        BASE_SYSTEM, _registry.tools
    )
else:
    effective_system, effective_tools = BASE_SYSTEM, _registry.tools

@agent_decorator(model=model_id, system=effective_system)
@use_tools(*effective_tools)
class _AgenthiccAgent: ...

# ... (build runner, run agent) ...

# 3. Post-flight hook — may summarise or warn about the response.
if active_mode and active_mode.post_hook:
    content = active_mode.post_hook(content, renderer)
```

---

## Session Startup Integration

```python
# In InlineRenderer.run() — after command registry setup

from agenthicc.modes import build_default_registry as _build_mode_registry
from agenthicc.modes import ModeManager

_mode_registry = _build_mode_registry()
_mode_manager  = ModeManager(_mode_registry, default_name="Auto")
self._mode_manager  = _mode_manager
self._mode_registry = _mode_registry
```

---

## Package Layout

```
src/agenthicc/modes/
  __init__.py        ← re-exports Mode, ModeRegistry, ModeManager,
                        build_default_registry, BUILTIN_MODES
  mode.py            ← Mode, ToolFilter, ModeHook
  registry.py        ← ModeRegistry
  manager.py         ← ModeManager
  builtins.py        ← all built-in modes + build_default_registry()
  plugin_loader.py   ← discover_mode_plugins() (PRD-50)
```

---

## Tests

```python
# tests/unit/test_mode_system.py  (pytestmark = pytest.mark.unit)

def test_mode_dataclass_badge():
    from agenthicc.modes import Mode
    m = Mode(name="Plan", label="PLAN", description="Planning mode", colour="yellow")
    assert "PLAN" in m.badge

def test_registry_register_and_get():
    from agenthicc.modes import ModeRegistry, Mode
    reg = ModeRegistry()
    reg.register(Mode("Auto", "AUTO", "Default", colour="green"))
    assert reg.get("Auto") is not None

def test_registry_cycle_order():
    from agenthicc.modes import ModeRegistry, Mode
    reg = ModeRegistry()
    for n in ("Auto", "Plan", "Safe"):
        reg.register(Mode(n, n.upper(), ""))
    assert reg.next_after("Auto").name == "Plan"
    assert reg.next_after("Plan").name == "Safe"
    assert reg.next_after("Safe").name == "Auto"   # wraps

def test_manager_cycle():
    from agenthicc.modes import ModeRegistry, ModeManager, Mode
    reg = ModeRegistry()
    for n in ("Auto", "Plan"):
        reg.register(Mode(n, n.upper(), ""))
    mgr = ModeManager(reg)
    assert mgr.active_name == "Auto"
    mgr.cycle()
    assert mgr.active_name == "Plan"
    mgr.cycle()
    assert mgr.active_name == "Auto"

def test_manager_apply_system_patch():
    from agenthicc.modes import ModeRegistry, ModeManager, Mode
    reg = ModeRegistry()
    reg.register(Mode("Plan", "PLAN", "", system_patch="PLAN MODE: do not write files."))
    mgr = ModeManager(reg, default_name="Plan")
    sys, tools = mgr.apply_to_agent("Base system.", ["read_file", "write_file"])
    assert sys.startswith("PLAN MODE")
    assert "Base system." in sys

def test_manager_tool_filter():
    from agenthicc.modes import ModeRegistry, ModeManager, Mode
    reg = ModeRegistry()
    reg.register(Mode("Safe", "SAFE", "",
        tool_filter=lambda name, _: not name.startswith("write")))
    mgr = ModeManager(reg, default_name="Safe")
    _, tools = mgr.apply_to_agent("base", ["read_file", "write_file", "git_status"])
    assert "write_file" not in tools
    assert "read_file" in tools

def test_unregister_source():
    from agenthicc.modes import ModeRegistry, Mode
    reg = ModeRegistry()
    reg.register(Mode("Custom", "CUSTOM", "", source_id="plugin:custom"))
    reg.register(Mode("Auto", "AUTO", "", source_id="builtin"))
    removed = reg.unregister_source("plugin:custom")
    assert removed == 1
    assert reg.get("Custom") is None
    assert reg.get("Auto") is not None
```
