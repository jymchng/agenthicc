"""Bounded JSON-value contracts for structured agent and subagent results."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import TypeAlias

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

__all__ = [
    "JSONMapping",
    "JSONValue",
    "MAX_RESULT_BYTES",
    "MAX_SCHEMA_BYTES",
    "ResultProjectionError",
    "ResultValidationError",
    "normalize_output_schema",
    "parse_output_schema",
    "project_result_value",
    "render_result_value",
    "validate_result_value",
]

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
JSONMapping: TypeAlias = dict[str, JSONValue]

MAX_SCHEMA_BYTES = 16 * 1024
MAX_RESULT_BYTES = 64 * 1024
_MAX_SCHEMA_DEPTH = 8
_MAX_SCHEMA_NODES = 256
_MAX_RESULT_DEPTH = 16
_MAX_RESULT_NODES = 8_192

_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$comment",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
)
_SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_SECRET_FIELD_PARTS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "credential",
        "credentials",
        "apikey",
        "privatekey",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
    }
)
_SECRET_FIELD_PART_RE = re.compile(r"[^a-z0-9]+")


class ResultProjectionError(ValueError):
    """A submitted result could not be safely projected to bounded JSON."""


class ResultValidationError(ValueError):
    """A projected result does not satisfy its declared JSON Schema."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant {value!r}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _encode_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("value is not finite, serializable JSON") from exc


def _copy_json_value(value: object, *, max_depth: int, max_nodes: int) -> JSONValue:
    nodes = 0

    def visit(item: object, depth: int) -> JSONValue:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise ResultProjectionError("value exceeds the JSON node limit")
        if depth > max_depth:
            raise ResultProjectionError("value exceeds the JSON nesting limit")
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ResultProjectionError("value contains a non-finite number")
            return item
        if isinstance(item, Mapping):
            result: JSONMapping = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ResultProjectionError("object keys must be strings")
                result[key] = visit(child, depth + 1)
            return result
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        raise ResultProjectionError(f"value contains unsupported type {type(item).__name__}")

    return visit(value, 0)


def project_result_value(value: Mapping[str, object]) -> JSONMapping:
    """Copy one submission into finite, serializable JSON within strict bounds."""
    projected = _copy_json_value(
        value,
        max_depth=_MAX_RESULT_DEPTH,
        max_nodes=_MAX_RESULT_NODES,
    )
    if not isinstance(projected, dict):
        raise ResultProjectionError("the submitted result must be an object")
    try:
        encoded = _encode_json(projected)
    except ValueError as exc:
        raise ResultProjectionError(str(exc)) from exc
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ResultProjectionError(f"value exceeds the {MAX_RESULT_BYTES}-byte limit")
    return projected


def _schema_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    return _object_without_duplicates(pairs)


def parse_output_schema(raw: str) -> JSONMapping:
    """Parse and validate the single-line JSON object used by ``output:``."""
    if len(raw.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError(f"output schema exceeds the {MAX_SCHEMA_BYTES}-byte limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_schema_object_pairs,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"output must be a valid JSON Schema object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("output must be a JSON Schema object")
    return normalize_output_schema(value)


def _validate_schema_tree(schema: Mapping[str, object]) -> None:
    nodes = 0

    def visit(node: Mapping[str, object], depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_SCHEMA_NODES:
            raise ValueError(f"output schema exceeds the {_MAX_SCHEMA_NODES}-node limit")
        if depth > _MAX_SCHEMA_DEPTH:
            raise ValueError(f"output schema exceeds the {_MAX_SCHEMA_DEPTH}-level depth limit")
        unknown = set(node) - _SCHEMA_KEYWORDS
        if unknown:
            raise ValueError(f"unsupported output schema keyword {sorted(unknown)[0]!r}")

        schema_type = node.get("type")
        if isinstance(schema_type, str):
            types = [schema_type]
        elif isinstance(schema_type, list) and all(isinstance(item, str) for item in schema_type):
            types = schema_type
        elif schema_type is None:
            types = []
        else:
            raise ValueError("output schema 'type' must be a JSON type or list of types")
        if any(item not in _SCHEMA_TYPES for item in types):
            raise ValueError("output schema 'type' contains an unsupported JSON type")

        properties = node.get("properties")
        if properties is not None:
            if not isinstance(properties, dict) or not all(
                isinstance(key, str) and isinstance(child, dict)
                for key, child in properties.items()
            ):
                raise ValueError("output schema 'properties' must map names to schemas")
            for name, child in properties.items():
                _validate_field_name(name)
                visit(child, depth + 1)

        for keyword in ("items", "additionalProperties"):
            child = node.get(keyword)
            if isinstance(child, dict):
                visit(child, depth + 1)
            elif keyword == "items" and child is not None:
                raise ValueError("output schema 'items' must be a schema object")
            elif (
                keyword == "additionalProperties"
                and child is not None
                and not isinstance(child, bool)
            ):
                raise ValueError("output schema 'additionalProperties' must be a boolean or schema")

    visit(schema, 0)
    if schema.get("type") != "object":
        raise ValueError("output schema root 'type' must be 'object'")


def _validate_field_name(name: str) -> None:
    normalized = _SECRET_FIELD_PART_RE.sub("", name.casefold())
    if normalized in _SECRET_FIELD_PARTS or (
        normalized.endswith("token") and normalized not in {"tokencount", "tokensused"}
    ):
        raise ValueError(f"output schema field {name!r} is secret-shaped")


def normalize_output_schema(schema: Mapping[str, object]) -> JSONMapping:
    """Validate a bounded, local-only schema and return an isolated JSON copy."""
    copied = _copy_json_value(
        schema,
        max_depth=_MAX_SCHEMA_DEPTH + 4,
        max_nodes=_MAX_SCHEMA_NODES * 8,
    )
    if not isinstance(copied, dict):
        raise ValueError("output schema must be an object")
    encoded = _encode_json(copied)
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError(f"output schema exceeds the {MAX_SCHEMA_BYTES}-byte limit")
    _validate_schema_tree(copied)
    try:
        Draft202012Validator.check_schema(copied)
    except SchemaError as exc:
        detail = str(exc.message).replace("\n", " ")[:240]
        raise ValueError(f"invalid output schema: {detail}") from exc
    return copied


def validate_result_value(schema: Mapping[str, JSONValue], value: Mapping[str, JSONValue]) -> None:
    """Raise an error with a field path when *value* violates *schema*."""
    validator = Draft202012Validator(dict(schema))
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: (
            len(error.absolute_path),
            tuple(str(part) for part in error.absolute_path),
        ),
    )
    if not errors:
        return
    error = errors[0]
    path = _error_path(error)
    rule = str(error.validator or "schema")
    location = path or "<root>"
    raise ResultValidationError(f"{location}: value violates the {rule!r} constraint")


def _error_path(error: ValidationError) -> str:
    parts: list[str] = []
    for part in error.absolute_path:
        if isinstance(part, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{part}]"
            else:
                parts.append(f"[{part}]")
        else:
            parts.append(str(part))
    return ".".join(parts)


def render_result_value(value: Mapping[str, JSONValue]) -> str:
    """Render validated structured output as stable, readable JSON prose."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
