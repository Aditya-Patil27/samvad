# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Verify prompt caching is actually hitting.

Caching is a PREFIX match: any byte change anywhere in the prefix invalidates
everything after it. If cache_read_input_tokens is zero across repeated calls,
something in your prefix is changing.

Usual suspects: a timestamp or message id in the system prompt, unsorted JSON
in a serialised tool definition, a varying tool list, or a prefix under ~1024
tokens (below the minimum it silently will not cache).
"""


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
