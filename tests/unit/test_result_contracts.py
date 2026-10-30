from __future__ import annotations

import pytest

from agenthicc.result_contracts import (
    ResultProjectionError,
    ResultValidationError,
    normalize_output_schema,
    parse_output_schema,
    project_result_value,
    render_result_value,
    validate_result_value,
)

pytestmark = pytest.mark.unit


def test_parse_output_schema_accepts_a_bounded_object_contract() -> None:
    schema = parse_output_schema(
        '{"type":"object","properties":{"summary":{"type":"string"}},'
        '"required":["summary"],"additionalProperties":false}'
    )

    assert schema["type"] == "object"
    assert schema["required"] == ["summary"]


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"type":"array"}',
        '{"type":"object","$ref":"https://example.test/schema.json"}',
        '{"type":"object","properties":{"api_token":{"type":"string"}}}',
        '{"type":"object","properties":{"x":{"type":"not-a-type"}}}',
        '{"type":"object","type":"array"}',
    ],
)
def test_parse_output_schema_rejects_invalid_contracts(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_output_schema(raw)


def test_normalize_output_schema_returns_an_isolated_json_copy() -> None:
    source: dict[str, object] = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
    }

    normalized = normalize_output_schema(source)
    source["properties"] = {}

    assert normalized["properties"] == {"count": {"type": "integer"}}


def test_project_validate_and_render_result_value() -> None:
    schema = parse_output_schema(
        '{"type":"object","properties":{"count":{"type":"integer"},'
        '"summary":{"type":"string"}},"required":["count","summary"],'
        '"additionalProperties":false}'
    )
    value = project_result_value({"summary": "done", "count": 2})

    validate_result_value(schema, value)

    assert render_result_value(value) == '{\n  "count": 2,\n  "summary": "done"\n}'


def test_validation_error_identifies_a_stable_field_path() -> None:
    schema = parse_output_schema(
        '{"type":"object","properties":{"count":{"type":"integer"}},'
        '"required":["count"],"additionalProperties":false}'
    )
    value = project_result_value({"count": "two"})

    with pytest.raises(ResultValidationError, match="count: value violates the 'type' constraint"):
        validate_result_value(schema, value)


def test_projector_rejects_non_json_values() -> None:
    with pytest.raises(ResultProjectionError, match="non-finite"):
        project_result_value({"score": float("nan")})

    with pytest.raises(ResultProjectionError, match="unsupported type"):
        project_result_value({"value": object()})
