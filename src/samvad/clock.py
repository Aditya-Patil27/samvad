# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""Lamport logical clock.

Four devices means four wall clocks that disagree. Order log entries by
(lamport, sender) -- never by timestamp. See docs/PROTOCOL.md.
"""


class LamportClock:
    def __init__(self, start: int = 0) -> None:
        if start < 0:
            raise ValueError(f"lamport must be >= 0, got {start}")
        self._t = start

    def tick(self) -> int:
        """Call on send. Returns the value to stamp on the message."""
        self._t += 1
        return self._t

    def observe(self, remote: int) -> int:
        """Call on receive. local = max(local, remote) + 1.

        `remote` arrives off the wire, so it is validated here rather than
        trusted: a negative value would drag the clock backwards and silently
        break causal ordering for every message that followed.

        The read-modify-write has no await point, so concurrent handlers on
        one asyncio loop cannot interleave inside it. It is NOT safe across
        threads -- the node is single-loop by design.
        """
        if remote < 0:
            raise ValueError(f"lamport must be >= 0, got {remote}")
        self._t = max(self._t, remote) + 1
        return self._t

    @property
    def value(self) -> int:
        return self._t
