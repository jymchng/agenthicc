"""Mode system for agenthicc — operating modes that shape agent behaviour.

Public surface
--------------
Mode                   — dataclass for a single mode (badge, tool_filter, system_patch …)
ToolFilter             — type alias for mode tool-filter callables
ModeHook               — type alias for pre/post hook callables
ModeRegistry           — ordered registry with cycling and source-based removal
ModeManager            — stateful wrapper that tracks the current mode and applies it
BUILTIN_MODES          — list of the 6 built-in Mode instances
build_default_registry — factory returning a ModeRegistry with all built-in modes
ModeLoadResult         — result of loading a single mode plugin file
ModePluginSet          — aggregated results from a full plugin discovery scan
discover_mode_plugins  — discover and load mode plugins from filesystem directories
"""
from __future__ import annotations

from .mode import Mode, ModeHook, ToolFilter
from .registry import ModeRegistry
from .manager import ModeManager
from .builtin import BUILTIN_MODES, build_default_registry
from .plugin_loader import ModeLoadResult, ModePluginSet, discover_mode_plugins

__all__ = [
    "Mode",
    "ModeHook",
    "ModeManager",
    "ModeRegistry",
    "ToolFilter",
    "BUILTIN_MODES",
    "build_default_registry",
    "ModeLoadResult",
    "ModePluginSet",
    "discover_mode_plugins",
]
