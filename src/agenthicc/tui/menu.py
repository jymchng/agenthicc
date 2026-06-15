"""Menu Widget System — abstract interactive panels below the input bar (PRD-41, PRD-42)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "MenuResultKind",
    "MenuResult",
    "MenuWidget",
    "MenuDriver",
    "RendererContext",
    "CommandMenuRegistry",
]


# ---------------------------------------------------------------------------
# MenuResultKind
# ---------------------------------------------------------------------------


class MenuResultKind(str, Enum):
    """Signals what happened after a single keypress inside a MenuWidget."""

    CONTINUE = "CONTINUE"  # keep the menu open, no return value
    DONE = "DONE"          # menu completed normally, value in .data
    CANCEL = "CANCEL"      # user pressed Esc; no value


# ---------------------------------------------------------------------------
# MenuResult
# ---------------------------------------------------------------------------


@dataclass
class MenuResult:
    """Value returned from MenuWidget.handle_key()."""

    kind: MenuResultKind
    data: Any = None  # set when kind == DONE

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def continue_(cls) -> MenuResult:
        """The menu should stay open; no selection yet."""
        return cls(kind=MenuResultKind.CONTINUE)

    @classmethod
    def done(cls, value: Any = None) -> MenuResult:
        """The menu completed normally; *value* is the result payload."""
        return cls(kind=MenuResultKind.DONE, data=value)

    @classmethod
    def cancel(cls) -> MenuResult:
        """The user dismissed the menu (Esc); no value is produced."""
        return cls(kind=MenuResultKind.CANCEL)


# ---------------------------------------------------------------------------
# MenuWidget Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MenuWidget(Protocol):
    """Abstract interactive panel rendered below the input bar.

    Implementing classes must provide ``render``, ``handle_key``, and the
    ``edit_field_value`` property.  The protocol is ``runtime_checkable`` so
    ``isinstance(w, MenuWidget)`` works for duck-type validation.
    """

    def render(
        self,
        prompt_str: str,
        buf: list[str],
        prev_n_lines: int,
    ) -> int:
        """Erase *prev_n_lines* old rows, redraw the prompt + buf + widget.

        Returns the number of lines now visible below the input row.  The
        caller stores this as *prev_n_lines* for the next call so the widget
        can erase exactly the right number of rows on the next redraw.
        """
        ...  # pragma: no cover

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        """Process one keystroke.

        Parameters
        ----------
        key:
            A platform-specific key constant (e.g. ``Key.ENTER``).
        ch:
            The decoded character string for printable characters (may be
            empty for special keys).

        Returns a :class:`MenuResult` indicating whether the menu should
        stay open, return a value, or be dismissed.
        """
        ...  # pragma: no cover

    @property
    def edit_field_value(self) -> str | None:
        """Current value to show in the input bar while this menu is active.

        Return ``None`` to leave the input bar unchanged (normal filter /
        fragment mode).  Override in command menus that repurpose the input
        bar for inline field editing.
        """
        return None  # pragma: no cover


# ---------------------------------------------------------------------------
# MenuDriver
# ---------------------------------------------------------------------------


class MenuDriver:
    """Routes rendering and key events to the single active :class:`MenuWidget`.

    Only one widget may be active at a time.  :meth:`open` replaces any
    previously active widget.  :meth:`close` dismisses the widget and resets
    the line-count so the caller's ``_redraw`` can reclaim the space.

    Typical usage inside ``read_line_with_mention``::

        driver = MenuDriver()

        # optionally pre-open a command menu
        if initial_menu is not None:
            driver.open(initial_menu)

        while True:
            if driver.active:
                driver.render(prompt_str, display_buf)
                result = driver.handle_key(key, ch)
                ...
    """

    def __init__(self) -> None:
        self._widget: MenuWidget | None = None
        self._prev_lines: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """``True`` when a widget is currently open."""
        return self._widget is not None

    @property
    def widget(self) -> MenuWidget | None:
        """The currently active widget, or ``None``."""
        return self._widget

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self, widget: MenuWidget) -> None:
        """Open *widget*, replacing any previously active widget."""
        self._widget = widget
        self._prev_lines = 0

    def close(self) -> None:
        """Close the active widget, erasing its rendered rows from the terminal."""
        if self._prev_lines > 0:
            # Erase the rows the widget drew below the input line.
            for _ in range(self._prev_lines):
                sys.stdout.write("\n\r\x1b[2K")
            sys.stdout.write(f"\x1b[{self._prev_lines}A")
            sys.stdout.flush()
        self._widget = None
        self._prev_lines = 0

    # ------------------------------------------------------------------
    # Render / key-handling
    # ------------------------------------------------------------------

    def render(self, prompt_str: str, buf: list[str]) -> None:
        """Delegate rendering to the active widget, tracking line count.

        If no widget is active this is a no-op; the caller is responsible
        for normal ``_redraw`` in that case.
        """
        if self._widget is not None:
            self._prev_lines = self._widget.render(
                prompt_str, buf, self._prev_lines
            )

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        """Forward *key* / *ch* to the active widget.

        If no widget is active, returns :meth:`MenuResult.continue_` so the
        caller can treat the absence of a widget as a transparent no-op.

        When the widget returns :attr:`MenuResultKind.DONE` or
        :attr:`MenuResultKind.CANCEL` the driver automatically calls
        :meth:`close` before returning the result.
        """
        if self._widget is None:
            return MenuResult.continue_()

        result = self._widget.handle_key(key, ch)
        if result.kind != MenuResultKind.CONTINUE:
            self.close()
        return result


# ---------------------------------------------------------------------------
# RendererContext
# ---------------------------------------------------------------------------


@dataclass
class RendererContext:
    """Minimal snapshot of renderer state passed to :class:`MenuWidget` factories.

    Attributes
    ----------
    config:
        The live :class:`~agenthicc.config.AgenthiccConfig` object.  Menus
        that edit configuration receive a reference to this object and mutate
        it in place.
    console:
        A Rich ``Console`` instance used for styled output inside the menu.
    session_id:
        The current session identifier string, used for display purposes.
    """

    config: Any  # AgenthiccConfig live object
    console: Any  # Rich Console
    session_id: str = ""


# ---------------------------------------------------------------------------
# MenuFactory type alias
# ---------------------------------------------------------------------------

#: A callable that accepts a :class:`RendererContext` and returns a fresh
#: :class:`MenuWidget` instance.  Used as the value type in
#: :class:`CommandMenuRegistry`.
MenuFactory = Callable[[RendererContext], MenuWidget]


# ---------------------------------------------------------------------------
# CommandMenuRegistry
# ---------------------------------------------------------------------------


class CommandMenuRegistry:
    """Maps slash-command names to :data:`MenuFactory` callables.

    Registration happens at application startup::

        registry = CommandMenuRegistry()
        registry.register("/config", lambda ctx: ConfigurationMenu(ctx.config, ctx.console))

    At dispatch time, :meth:`get` is called with the command string; if a
    factory is found the caller invokes it with a :class:`RendererContext` to
    obtain a live :class:`MenuWidget`.

    Commands not present in the registry return ``None`` from :meth:`get`,
    allowing the caller to fall through to existing behaviour.
    """

    def __init__(self) -> None:
        self._factories: dict[str, MenuFactory] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, command: str, factory: MenuFactory) -> None:
        """Associate *command* (e.g. ``"/config"``) with *factory*.

        Any previous registration for *command* is silently replaced.
        """
        self._factories[command] = factory

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, command: str) -> MenuFactory | None:
        """Return the factory for *command*, or ``None`` if not registered."""
        return self._factories.get(command)

    def commands(self) -> list[str]:
        """Return a list of all registered command strings."""
        return list(self._factories)

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the number of registered commands."""
        return len(self._factories)

    def __repr__(self) -> str:  # pragma: no cover
        cmds = ", ".join(sorted(self._factories))
        return f"CommandMenuRegistry([{cmds}])"
