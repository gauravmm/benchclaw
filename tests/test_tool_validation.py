from nanobot.agent.tools.base import _validate_schema

# TODO: Change these tests to check the new structure
parameters = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "mode": {"type": "string", "enum": ["fast", "full"]},
        "meta": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "flags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["tag"],
        },
    },
    "required": ["query", "count"],
}


def test_validate_params_missing_required() -> None:
    errors = _validate_schema({"query": "hi"}, parameters)
    assert "missing required count" in "; ".join(errors)


def test_validate_params_type_and_range() -> None:
    errors = _validate_schema({"query": "hi", "count": 0}, parameters)
    assert any("count must be >= 1" in e for e in errors)

    errors = _validate_schema({"query": "hi", "count": "2"}, parameters)
    assert any("count should be integer" in e for e in errors)


def test_validate_params_enum_and_min_length() -> None:
    errors = _validate_schema({"query": "h", "count": 2, "mode": "slow"}, parameters)
    assert any("query must be at least 2 chars" in e for e in errors)
    assert any("mode must be one of" in e for e in errors)


def test_validate_params_nested_object_and_array() -> None:
    errors = _validate_schema(
        {
            "query": "hi",
            "count": 2,
            "meta": {"flags": [1, "ok"]},
        },
        parameters,
    )
    assert any("missing required meta.tag" in e for e in errors)
    assert any("meta.flags[0] should be string" in e for e in errors)


def test_validate_params_ignores_unknown_fields() -> None:
    errors = _validate_schema({"query": "hi", "count": 2, "extra": "x"}, parameters)
    assert errors == []
