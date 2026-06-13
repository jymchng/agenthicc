"""ModeRegistry — ordered collection of Mode instances with lookup and cycling."""
from __future__ import annotations

from typing import Iterator

from .mode import Mode

__all__ = ["ModeRegistry"]


class ModeRegistry:
    """Ordered registry of :class:`Mode` instances.

    Modes are stored in insertion order.  Registering a mode whose ``name``
    already exists replaces the previous entry in-place (preserving position).

    Examples
    --------
    >>> reg = ModeRegistry()
    >>> reg.register(Mode(name="Auto", label="AUTO", description="Automatic"))
    >>> reg.register(Mode(name="Plan", label="PLAN", description="Planning only"))
    >>> len(reg)
    2
    >>> reg.next_after("Auto").name
    'Plan'
    """

    def __init__(self) -> None:
        self._modes: list[Mode] = []
        self._by_name: dict[str, Mode] = {}

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def register(self, mode: Mode) -> None:
        """Register *mode*, replacing any existing entry with the same name."""
        if mode.name in self._by_name:
            idx = next(i for i, m in enumerate(self._modes) if m.name == mode.name)
            self._modes[idx] = mode
        else:
            self._modes.append(mode)
        self._by_name[mode.name] = mode

    def register_many(self, modes: list[Mode]) -> None:
        """Register each mode in *modes* in order."""
        for mode in modes:
            self.register(mode)

    def unregister_source(self, source_id: str) -> int:
        """Remove all modes whose ``source_id`` matches *source_id*.

        Returns
        -------
        int
            The number of modes removed.
        """
        before = len(self._modes)
        self._modes = [m for m in self._modes if m.source_id != source_id]
        self._by_name = {m.name: m for m in self._modes}
        return before - len(self._modes)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get(self, name: str) -> Mode | None:
        """Return the :class:`Mode` with the given *name*, or ``None``."""
        return self._by_name.get(name)

    def all_modes(self) -> list[Mode]:
        """Return a snapshot list of all registered modes in insertion order."""
        return list(self._modes)

    def next_after(self, current_name: str) -> Mode:
        """Return the mode that follows *current_name* in registration order.

        Cycles back to the first mode when *current_name* is the last entry.
        If *current_name* is not found, returns the first mode.

        Raises
        ------
        ValueError
            When the registry is empty.
        """
        if not self._modes:
            raise ValueError("ModeRegistry is empty; cannot cycle modes.")
        names = [m.name for m in self._modes]
        try:
            idx = names.index(current_name)
            return self._modes[(idx + 1) % len(self._modes)]
        except ValueError:
            return self._modes[0]

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._modes)

    def __iter__(self) -> Iterator[Mode]:
        return iter(self._modes)

    def __repr__(self) -> str:  # pragma: no cover
        names = [m.name for m in self._modes]
        return f"ModeRegistry({names!r})"
