"""Menu widget system — MenuResult, MenuDriver, CommandMenuRegistry, RendererContext."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "CommandMenuRegistry",
    "MenuDriver",
    "MenuResult",
    "MenuResultKind",
    "MenuWidget",
    "RendererContext",
]


class MenuResultKind(Enum):
    CONTINUE = auto()
    DONE = auto()
    CANCEL = auto()


@dataclass
class MenuResult:
    kind: MenuResultKind
    data: Any = None

    @classmethod
    def continue_(cls) -> MenuResult:
        return cls(kind=MenuResultKind.CONTINUE)

    @classmethod
    def done(cls, data: Any = None) -> MenuResult:
        return cls(kind=MenuResultKind.DONE, data=data)

    @classmethod
    def cancel(cls) -> MenuResult:
        return cls(kind=MenuResultKind.CANCEL)


@runtime_checkable
class MenuWidget(Protocol):
    """Protocol for interactive menu widgets."""

    def render(self, prompt_str: str, buf: list, prev: int) -> int:
        ...

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        ...

    @property
    def edit_field_value(self) -> Any:
        ...


@dataclass
class RendererContext:
    """Rendering context passed to menu factories."""
    config: Any = None
    console: Any = None
    session_id: str = ""
    # Additional fields for new-style usage
    width: int = 80
    height: int = 24
    colors: bool = True


class MenuDriver:
    """Controls the lifecycle of an active MenuWidget."""

    def __init__(self, registry: Any = None) -> None:
        self._widget: MenuWidget | None = None
        self._registry = registry
        self._prev_lines: int = 0

    @property
    def active(self) -> bool:
        return self._widget is not None

    @property
    def widget(self) -> MenuWidget | None:
        return self._widget

    def open(self, widget: Any, name: str | None = None) -> None:
        self._widget = widget

    def close(self) -> None:
        self._widget = None
        self._prev_lines = 0

    def handle_key(self, key: Any, ch: str = "") -> MenuResult:
        if self._widget is None:
            return MenuResult.continue_()
        result = self._widget.handle_key(key, ch)
        if result.kind in (MenuResultKind.DONE, MenuResultKind.CANCEL):
            self.close()
        return result

    def render(self, prompt_str: str, buf: list, prev: int = 0) -> int:
        if self._widget is None:
            return 0
        return self._widget.render(prompt_str, buf, prev)


class CommandMenuRegistry:
    """Maps slash-command strings to MenuWidget factory callables."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable] = {}
        # Also support old-style list-based menus for compat
        self._menus: dict[str, list[tuple[str, str]]] = {}

    def register(self, command: str, factory: Callable) -> None:
        self._factories[command] = factory

    def get(self, command: str) -> Callable | None:
        return self._factories.get(command)

    def get_items(self, name: str) -> list[tuple[str, str]]:
        return self._menus.get(name, [])

    def commands(self) -> list[str]:
        return list(self._factories.keys())

    def __len__(self) -> int:
        return len(self._factories)
