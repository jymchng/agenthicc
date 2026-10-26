"""Responsive list columns that degrade by priority (AGX-06 §5.2).

Rich's table algorithm shrinks no-wrap columns until they read ``wai…`` or
vanish. The hub instead keeps a column either at or above its minimum width or
drops it: columns are added in priority order while they fit, then the flexible
ones share the remaining width.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["COLUMNS", "Column", "Layout", "layout"]

#: The gutter before the first column: the selection mark (``▸``) and the
#: marked/unseen-activity mark (``✓``/``•``).
GUTTER = 2
#: Space between two columns.
SEPARATOR = 2


@dataclass(frozen=True)
class Column:
    name: str
    minimum: int
    #: Shown only at this terminal width and wider.
    from_width: int = 0
    #: A flexible column grows with the width; a fixed one stays at its minimum.
    flexible: bool = False


#: Priority order: earlier columns are kept, later ones dropped first.
COLUMNS: tuple[Column, ...] = (
    Column("status", 16),
    Column("title", 20, flexible=True),
    Column("age", 4),
    Column("activity", 16, from_width=90, flexible=True),
    Column("project", 8, from_width=100),
    Column("agent", 10, from_width=110),
    Column("cost", 6, from_width=120),
)


@dataclass(frozen=True)
class Layout:
    """Columns to show and each one's width, in display order."""

    width: int
    columns: tuple[str, ...]
    widths: tuple[int, ...]

    def width_of(self, name: str) -> int:
        return self.widths[self.columns.index(name)] if name in self.columns else 0


def layout(width: int, *, present: frozenset[str] | None = None) -> Layout:
    """Lay out the list for a terminal *width*.

    *present* names the optional columns that have any value to show (for
    example ``cost`` only when some row has a cost); absent ones are skipped.
    """
    chosen: list[Column] = []
    used = GUTTER
    for column in COLUMNS:
        if (
            present is not None
            and column.name not in present
            and column.name
            not in (
                "status",
                "title",
                "age",
            )
        ):
            continue
        if width < column.from_width:
            continue
        needed = column.minimum + (SEPARATOR if chosen else 0)
        if used + needed > width:
            continue
        chosen.append(column)
        used += needed
    widths = [column.minimum for column in chosen]
    spare = max(0, width - used)
    flexible = [index for index, column in enumerate(chosen) if column.flexible]
    if flexible:
        # The title takes two shares of the spare width for every one the
        # activity takes; any remainder goes to the title.
        weights = [2 if chosen[index].name == "title" else 1 for index in flexible]
        total = sum(weights)
        for index, weight in zip(flexible, weights, strict=True):
            widths[index] += spare * weight // total
        widths[flexible[0]] += spare - sum(spare * weight // total for weight in weights)
    # Order on screen: status, title, then the rest, with age and cost at the
    # right edge.
    order = ["status", "title", "activity", "project", "agent", "age", "cost"]
    pairs = sorted(
        zip((c.name for c in chosen), widths, strict=True), key=lambda p: order.index(p[0])
    )
    return Layout(width, tuple(name for name, _ in pairs), tuple(value for _, value in pairs))
