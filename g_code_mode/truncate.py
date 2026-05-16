"""Response truncation — prevents oversized results from blowing up LLM context."""

import json

MAX_TOKENS = 6_000
_CHARS_PER_TOKEN = 4
_MAX_CHARS = MAX_TOKENS * _CHARS_PER_TOKEN


def truncate_response(content: object) -> str:
    text = content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
    if len(text) <= _MAX_CHARS:
        return text
    estimated = len(text) // _CHARS_PER_TOKEN
    return (
        f"{text[:_MAX_CHARS]}\n\n--- TRUNCATED ---\n"
        f"Response was ~{estimated:,} tokens (limit: {MAX_TOKENS:,}). "
        f"Add filters (location, display_name, project) to narrow results."
    )
