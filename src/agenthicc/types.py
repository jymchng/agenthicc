"""Shared structural types for open data crossing Agenthicc boundaries.

This module deliberately contains vocabulary rather than domain models.  Kernel,
workflow, TUI, and integration models remain owned by their respective
packages; open JSON-shaped data uses these aliases until it is validated into
one of those models.
"""

from __future__ import annotations

from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

__all__ = ["JsonObject", "JsonScalar", "JsonValue"]
