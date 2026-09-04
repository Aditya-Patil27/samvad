# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""The core loop: a message arrived, produce replies.

INVARIANT: handle() RETURNS messages, it never sends them. Transport belongs
to P1. This keeps the agent core pure and testable.

INVARIANT: an agent may not set task_status=COMPLETE when the last
task_result carried exit_code != 0. Enforced HERE, in a code path -- not
requested in a prompt. The runtime is the arbiter; the model is advisory.
This is the single most effective hallucination control in the system.
"""


class Agent:
    async def handle(self, msg) -> list:
        """-> zero or more outbound messages."""
        raise NotImplementedError
