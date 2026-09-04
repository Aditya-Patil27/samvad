# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""Lamport logical clock.

Four devices means four wall clocks that disagree. Order log entries by
(lamport, sender) -- never by timestamp. See docs/PROTOCOL.md.
"""


class LamportClock:
    def __init__(self, start: int = 0) -> None:
        self._t = start

    def tick(self) -> int:
        """Call on send. Returns the value to stamp on the message."""
        raise NotImplementedError

    def observe(self, remote: int) -> int:
        """Call on receive. local = max(local, remote) + 1."""
        raise NotImplementedError

    @property
    def value(self) -> int:
        return self._t
