"""Tests for response truncation."""

from g_code_mode.truncate import MAX_TOKENS, _MAX_CHARS, truncate_response


def test_short_string_unchanged():
    assert truncate_response("hello") == "hello"


def test_dict_serialised():
    result = truncate_response({"key": "value"})
    assert '"key"' in result
    assert '"value"' in result


def test_long_string_truncated():
    long = "x" * (_MAX_CHARS + 10_000)
    result = truncate_response(long)
    assert "TRUNCATED" in result
    assert len(result) < len(long)


def test_truncation_message_contains_token_hint():
    long = "x" * (_MAX_CHARS + 1000)
    result = truncate_response(long)
    # limit appears as "6,000" (locale-formatted)
    assert "6,000" in result
    assert "filters" in result.lower()
