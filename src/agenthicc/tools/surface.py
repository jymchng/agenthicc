"""Immutable generations of a session's discovered extension tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from agenthicc.tools.base import tool_name

if TYPE_CHECKING:
    from agenthicc.tools.base import ToolLike


@dataclass(frozen=True, slots=True)
class ToolSurface:
    """Tools and provenance from one published generation."""

    generation: int
    tools: tuple[ToolLike, ...]
    sources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("A tool surface generation must be nonnegative.")
        by_name = {tool_name(tool): tool for tool in self.tools}
        names = sorted(by_name)
        object.__setattr__(self, "tools", tuple(by_name[name] for name in names))
        object.__setattr__(
            self,
            "sources",
            MappingProxyType({name: self.sources.get(name, "project") for name in names}),
        )


class ToolSurfaceHolder:
    """Hold one session's current surface for lock-free readers."""

    def __init__(self, surface: ToolSurface) -> None:
        self._surface = surface

    def current(self) -> ToolSurface:
        return self._surface

    def publish(self, surface: ToolSurface) -> None:
        """Replace the snapshot from the session's reload owner."""
        if surface.generation < self._surface.generation:
            raise ValueError("A tool surface generation cannot move backwards.")
        self._surface = surface
