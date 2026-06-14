"""Backward-compatibility re-export — canonical location is input/completions.py."""
from __future__ import annotations

from agenthicc.tui.input.completions import *  # noqa: F401, F403
from agenthicc.tui.input.completions import (  # noqa: F401  (explicit for type checkers)
    AtMentionCompleter,
    BUILTIN_COMMANDS,
    CommandRegistry,
    CommandSpec,
    SlashCommandCompleter,
    _entry_meta,
    build_default_registry,
)
