"""OverlayHost and Overlay base class (PRD-62 §3)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.tui.conversation_store import AppState


class Overlay(ABC):
    """Base class for all transient overlays."""

    name: str = "overlay"

    def on_mount(self) -> None:
        """Called when overlay becomes active."""

    def on_unmount(self) -> None:
        """Called when overlay is dismissed."""

    @abstractmethod
    def render(self) -> RenderableType:
        """Return a Rich renderable for the Live region."""

    @abstractmethod
    def handle_key(self, key: Key, ch: str) -> bool:
        """Handle a keystroke. Return True if consumed."""


class OverlayHost:
    """Manages the single active overlay. Part of the Live region."""

    def __init__(self, app_state: AppState) -> None:
        self._state:   AppState           = app_state
        self._overlay: Overlay | None     = None
        self._redraw:  Callable[[], None] | None = None

    def set_redraw_callback(self, fn: Callable[[], None]) -> None:
        self._redraw = fn

    @property
    def active(self) -> bool:
        return self._overlay is not None

    @property
    def widget(self) -> Overlay | None:
        return self._overlay

    def show(self, overlay: Overlay) -> None:
        if self._overlay:
            self._overlay.on_unmount()
        self._overlay = overlay
        overlay.on_mount()
        self._state.overlay.set(overlay.name)
        self._state.modal_open.set(True)
        if self._redraw:
            self._redraw()

    def hide(self) -> None:
        if self._overlay:
            self._overlay.on_unmount()
            self._overlay = None
        self._state.overlay.set("")
        self._state.modal_open.set(False)
        if self._redraw:
            self._redraw()

    def render(self) -> RenderableType | None:
        if self._overlay:
            return self._overlay.render()
        return None

    def handle_key(self, key: Key, ch: str) -> bool:
        """Return True if the overlay consumed the key.

        Always redraws after handling so that typing updates (fragment changes,
        match list scrolling, etc.) are visible immediately.
        """
        if self._overlay:
            consumed = self._overlay.handle_key(key, ch)
            # Redraw after every keystroke — overlay internal state may have
            # changed (fragment, selected index, hint text) even if the overlay
            # did not close.  Without this redraw typing appears to do nothing.
            if self._redraw:
                self._redraw()
            return consumed
        return False
