"""Loading, hot-reloading and reporting the user's key bindings (AGX-13)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.keymap import KeymapLoad, load_keybindings, template

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState

__all__ = ["KeymapService"]


class KeymapService:
    """Keep ``AppState.keymap`` in step with ``~/.agenthicc/keybindings.toml``.

    A valid edit applies at once; an invalid one keeps the defaults, is noted
    in the footer and is listed by ``/keybindings``.
    """

    def __init__(self, app_state: AppState, path: Path | None = None) -> None:
        self._state = app_state
        self.current: KeymapLoad = load_keybindings(DEFAULT_ACTIONS, path)
        app_state.keymap.set(self.current.keymap)

    @property
    def path(self) -> Path:
        return self.current.path

    def reload(self) -> KeymapLoad:
        """Re-read the file if it changed and publish the result."""
        loaded = load_keybindings(DEFAULT_ACTIONS, self.current.path)
        if loaded.fingerprint == self.current.fingerprint and loaded.errors == self.current.errors:
            return self.current
        self.current = loaded
        self._state.keymap.set(loaded.keymap)
        if loaded.errors:
            self._state.conversation.notify_transient(
                f"Key bindings not applied: {loaded.errors[0]} — see /keybindings", duration=8.0
            )
        else:
            self._state.conversation.notify_transient("Key bindings reloaded", duration=3.0)
        return loaded

    def ensure_file(self) -> bool:
        """Create the bindings file with every action commented out; ``True`` if created."""
        if self.current.path.exists():
            return False
        self.current.path.parent.mkdir(parents=True, exist_ok=True)
        self.current.path.write_text(template(DEFAULT_ACTIONS), encoding="utf-8")
        return True

    async def watch(self) -> None:
        """Reload whenever the bindings file changes, until cancelled."""
        from agenthicc.background.watch import FileWatcher

        watcher = FileWatcher(fallback_interval_s=1.0)
        watcher.watch("keybindings", self.current.path)
        try:
            while True:
                if await watcher.wait(None):
                    self.reload()
        except asyncio.CancelledError:
            raise
        finally:
            watcher.close()
