"""Reactive primitives: _Observable mixin and ReactiveProperty descriptor.

These two pieces form the foundation of the component state system:

_Observable
    Mixin that maintains a list of zero-argument callbacks.  Any class can
    inherit from it and call ``_notify()`` to inform all registered watchers.

ReactiveProperty
    Python descriptor that intercepts ``__set__`` and calls ``owner._notify()``
    whenever the stored value actually changes.  Declare fields on a state
    class and all assignments become reactive automatically — no explicit
    ``_notify()`` calls needed at every setter site.

Usage::

    from agenthicc.tui.reactive import _Observable, ReactiveProperty

    class MyState(_Observable):
        name  = ReactiveProperty("")
        count = ReactiveProperty(0)
        items = ReactiveProperty(default_factory=list)

    state = MyState()
    state.on_change(lambda: print("changed!"))
    state.name = "hello"   # prints "changed!"
    state.count = 1        # prints "changed!"
    state.count = 1        # silent — value unchanged
"""

from __future__ import annotations

from typing import Callable


class _Observable:
    """Mixin: maintain a list of callbacks; fire them on ``_notify()``."""

    def __init__(self) -> None:
        self._observers: list[Callable[[], None]] = []

    def on_change(self, cb: Callable[[], None]) -> None:
        """Register *cb* to be called (with no arguments) on every state change."""
        self._observers.append(cb)

    def off_change(self, cb: Callable[[], None]) -> None:
        """Remove a previously registered callback."""
        try:
            self._observers.remove(cb)
        except ValueError:
            pass

    def _notify(self) -> None:
        """Call all registered observers.  Exceptions are swallowed silently."""
        for cb in self._observers:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass


class ReactiveProperty:
    """Descriptor: backs the attribute and calls ``_notify()`` on every write.

    Parameters
    ----------
    default
        Default scalar value returned when no value has been set yet.
    default_factory
        Zero-argument callable used to produce the default for mutable types
        (e.g. ``default_factory=list`` to avoid shared-list bugs).

    Notes
    -----
    The backing attribute is stored under ``_rp_<name>`` on the instance so it
    never shadows the class-level descriptor.  Equality comparison uses ``is``
    for mutable containers (list / dict / set) to avoid the cost of deep
    equality; for scalars it uses ``!=``.
    """

    def __init__(
        self, default: object = None, *, default_factory: Callable[[], object] | None = None
    ) -> None:
        self._default = default
        self._factory = default_factory
        self._attr: str = ""  # filled in by __set_name__

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"_rp_{name}"

    def _get_default(self) -> object:
        return self._factory() if self._factory is not None else self._default

    def __get__(self, obj: object, objtype: type | None = None) -> object:
        if obj is None:
            return self
        if not hasattr(obj, self._attr):
            return self._get_default()
        return object.__getattribute__(obj, self._attr)

    def __set__(self, obj: object, value: object) -> None:
        prev = self.__get__(obj)
        object.__setattr__(obj, self._attr, value)
        changed = (value is not prev) if isinstance(value, (list, dict, set)) else (value != prev)
        if changed and hasattr(obj, "_notify"):
            obj._notify()
