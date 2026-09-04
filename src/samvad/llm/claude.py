# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Anthropic backend.

Model routing by task class (docs -> Metering):
    planning, code generation      claude-opus-5      $5.00 / $25.00 per MTok
    "is this done?", classifying   claude-haiku-4-5   $1.00 /  $5.00 per MTok

Prompt caching is a PREFIX match -- any byte change anywhere in the prefix
invalidates everything after it. Render order is tools -> system -> messages.
So: frozen system prompt and root_task first, never mutated; timestamps,
message ids and the peer's latest turn last. Minimum cacheable prefix is
~1024 tokens.

Verify with usage.cache_read_input_tokens. Persistently zero means something
in the prefix is changing -- run scripts/cache_check.py.
"""

RATES_USD_PER_MTOK = {
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class ClaudeBackend:
    async def complete(self, prompt):
        raise NotImplementedError
