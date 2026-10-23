"""Typed readers for JSON-shaped workflow values.

Checkpoint payloads and phase-tool hand-off dictionaries hold ``object``
values: they were decoded from JSON, or written by a transition tool the model
called. Each reader narrows one value and falls back to a default instead of
raising, so a malformed field degrades to its documented default rather than
crashing a resume.
"""

from __future__ import annotations

import math

__all__ = [
    "payload_float",
    "payload_int",
    "payload_mapping",
    "payload_mappings",
    "payload_strings",
]


def payload_int(value: object, default: int) -> int:
    """Return *value* as an ``int``; booleans and unparsable values give *default*."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else default
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def payload_float(value: object, default: float) -> float:
    """Return *value* as a ``float``; booleans and unparsable values give *default*."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def payload_mapping(value: object) -> dict[str, object]:
    """Return a JSON object with string keys, or ``{}`` for any other value."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def payload_mappings(value: object) -> list[dict[str, object]]:
    """Return the JSON objects of a list, skipping every other item."""
    if not isinstance(value, list):
        return []
    return [payload_mapping(item) for item in value if isinstance(item, dict)]


def payload_strings(value: object) -> list[str]:
    """Return the strings of a list, skipping every other item."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
