# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""HTTP to 127.0.0.1 -- several agent processes on one laptop."""
from __future__ import annotations

from typing import Any

import httpx

from samvad.transport.lan import LanTransport

LOOPBACK_HOST = "127.0.0.1"


class LoopbackTransport(LanTransport):
    """The LAN transport pointed at localhost.

    Deliberately a subclass and not a reimplementation. Measurement 1 compares
    inproc, loopback and lan, and its whole claim is that ONLY delivery differs
    between them. Two separate HTTP implementations would leave a second
    variable in the experiment -- serialisation, retry policy, header set --
    and the difference it reported would no longer be the network alone.

    It also means four agents can run on one laptop over real HTTP, which is
    how the system is demoed when four devices are not on the same network.
    """

    def __init__(
        self,
        peers: dict[str, dict[str, Any]] | None = None,
        secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_port: int = 8000,
    ) -> None:
        if peers is None:
            # One process per peer, consecutive ports on this machine.
            peers = {
                name: {"host": LOOPBACK_HOST, "port": base_port + i}
                for i, name in enumerate(("agent_a", "agent_b", "agent_c", "agent_d"))
            }
        else:
            peers = {
                name: {**cfg, "host": LOOPBACK_HOST} for name, cfg in peers.items()
            }
        super().__init__(peers, secret=secret, client=client)
