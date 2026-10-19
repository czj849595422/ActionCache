"""Reusable parameter-schema validation for skill action calls.

Both the legacy four-line text protocol and the PPA JSON decision protocol
validate expanded action parameters against the manifest ``params_schema``
using these helpers.
"""

from __future__ import annotations

from typing import Any


class SchemaValidationError(ValueError):
    """Raised when an action call does not satisfy its manifest schema."""


def validate_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    """Validate ``value`` against a JSON-schema subset, raising on mismatch."""
    if not schema:
        return
    expected = schema.get("type")
    if expected is not None and not matches_type(value, expected):
        raise SchemaValidationError(f"{path} must be of type {expected}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(f"{path} must be <= {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError(f"{path} is longer than maxLength")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaValidationError(f"{path} has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(f"{path} has more than maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_schema(item, item_schema, path=f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaValidationError(f"{path} is missing required fields: {missing}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(f"{path} contains unknown fields: {unknown}")
        for name, item in value.items():
            item_schema = properties.get(name)
            if isinstance(item_schema, dict):
                validate_schema(item, item_schema, path=f"{path}.{name}")

    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if not any(schema_matches(value, option, path) for option in alternatives):
            raise SchemaValidationError(f"{path} does not match anyOf constraints")
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list) and alternatives:
        matches = sum(schema_matches(value, option, path) for option in alternatives)
        if matches != 1:
            raise SchemaValidationError(f"{path} must match exactly one oneOf constraint")


def schema_matches(value: Any, schema: Any, path: str) -> bool:
    """Return True when ``value`` satisfies ``schema`` without raising."""
    if not isinstance(schema, dict):
        return False
    try:
        validate_schema(value, schema, path=path)
        return True
    except SchemaValidationError:
        return False


def matches_type(value: Any, expected: Any) -> bool:
    """Check a value against a schema ``type`` token (string or list)."""
    if isinstance(expected, list):
        return any(matches_type(value, item) for item in expected)
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "null": lambda item: item is None,
    }
    check = checks.get(str(expected))
    return False if check is None else check(value)
