# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""MOCK_LLM=1 -- canned responses, zero cost.

Build this SECOND, right after the interface. All transport, routing, spawn
topology and failure-mode work runs against it. You will iterate on the wire
hundreds of times; none of it should cost anything.

Doubles as the demo-day fallback.
"""


class MockBackend:
    async def complete(self, prompt):
        raise NotImplementedError
