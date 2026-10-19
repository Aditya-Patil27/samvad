# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""Child lifecycle: spawn, track, reparent, cap concurrency.

Failure behaviour (docs/PROTOCOL.md):
    child crashes            retry once, then needs_revision upward
    parent dies mid-fan-out  orphans reparent to the GRANDPARENT
    host peer dies           parent re-places the child, unspent slice intact
    max_depth exceeded       spawn_refused; parent does the work itself

Killing a parent mid-fan-out and watching orphans still deliver is the
primary live demo. Build it so that actually works.

DESIGN DECISIONS
-----------------
This class TRACKS children, it does not spawn processes. The transport layer
(P1) and node wiring (node.py) own process lifecycles. Supervisor owns:

* bookkeeping: who are my children, what budget slice did each get
* fan-out coordination: are all children in a group done yet
* reparenting: when a parent dies, move ownership to the grandparent
* concurrency cap: never exceed MAX_CONCURRENCY live children
* retry policy: crash → retry once → then needs_revision upward
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from samvad.protocol import (
    Budget,
    Message,
    Performative,
    TaskStatus,
)

MAX_CONCURRENCY = 8   # rate limits arrive well before CPU limits

#: Maximum retries before reporting needs_revision upward.
MAX_RETRIES = 1


class ChildStatus(enum.StrEnum):
    """Lifecycle states for a tracked child."""
    PENDING = "pending"           # spawn_ack sent, awaiting first result
    ACTIVE = "active"             # has received at least one message
    DONE = "done"                 # delivered a terminal result
    FAILED = "failed"             # crashed, exhausted retries
    RETRYING = "retrying"         # crashed once, retry in progress


@dataclass
class ChildRecord:
    """One tracked child in a fan-out group."""
    path: str
    group_id: str                 # ties siblings in a fan-out together
    budget_slice: Budget
    status: ChildStatus = ChildStatus.PENDING
    retries: int = 0
    result: Message | None = None
    original_request: Message | None = None  # the spawn/task that created it


@dataclass
class FanOutGroup:
    """A set of children spawned together from one request."""
    group_id: str
    parent_message: Message       # the message that triggered the fan-out
    expected: int                 # how many children were spawned
    results: list[Message] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return len(self.results) >= self.expected


class Supervisor:
    """Tracks child agents, coordinates fan-outs, handles reparenting.

    Composed into Agent (not subclassed). Agent.__init__ creates
    ``self.supervisor = Supervisor(agent_path)`` and delegates child lifecycle
    events here. handle() routes ``child_result`` performatives to
    ``on_child_result()``.
    """

    def __init__(self, agent_path: str) -> None:
        self.agent_path = agent_path
        self._children: dict[str, ChildRecord] = {}  # keyed by child path
        self._groups: dict[str, FanOutGroup] = {}     # keyed by group_id
        self._next_worker: int = 0                    # monotonic worker counter

    # --- spawning ---------------------------------------------------------

    def spawn(
        self,
        group_id: str,
        budget_slice: Budget,
        *,
        parent_message: Message | None = None,
    ) -> str:
        """Register a new child and return its agent path.

        Raises ValueError if the concurrency cap would be exceeded.
        Does NOT start a process -- that's the transport's job.

        The child path follows the established pattern from agent.py:
        ``{parent}/worker_{n}``.
        """
        if self.active_count >= MAX_CONCURRENCY:
            raise ValueError(
                f"concurrency cap reached: {self.active_count}/{MAX_CONCURRENCY} "
                f"children active for {self.agent_path}"
            )

        self._next_worker += 1
        child_path = f"{self.agent_path}/worker_{self._next_worker}"

        self._children[child_path] = ChildRecord(
            path=child_path,
            group_id=group_id,
            budget_slice=budget_slice,
            original_request=parent_message,
        )
        return child_path

    def register_group(
        self,
        group_id: str,
        parent_message: Message,
        n_children: int,
    ) -> None:
        """Track a fan-out group so we know when all children have reported."""
        self._groups[group_id] = FanOutGroup(
            group_id=group_id,
            parent_message=parent_message,
            expected=n_children,
        )

    # --- results ----------------------------------------------------------

    def on_child_result(self, msg: Message) -> dict[str, Any]:
        """A child delivered a result. Track it and report status.

        Returns a dict with:
          - ``"child_path"``: the sender
          - ``"group_complete"``: True if all siblings in this fan-out are done
          - ``"group_id"``: the fan-out group this child belongs to
          - ``"all_results"``: list of all results if group is complete, else []
          - ``"needs_retry"``: True if this child failed and should be retried
        """
        child_path = msg.sender
        record = self._children.get(child_path)

        if record is None:
            # Unknown child -- could be a reparented orphan arriving late.
            # Accept the result but don't track it in a group.
            return {
                "child_path": child_path,
                "group_complete": False,
                "group_id": "",
                "all_results": [],
                "needs_retry": False,
            }

        # Check for failure: child crashed or reported budget_exhausted
        if msg.performative is Performative.BUDGET_EXHAUSTED or (
            msg.task_status in (TaskStatus.ABANDONED, TaskStatus.NEEDS_REVISION)
            and record.retries < MAX_RETRIES
        ):
            record.retries += 1
            record.status = ChildStatus.RETRYING
            return {
                "child_path": child_path,
                "group_complete": False,
                "group_id": record.group_id,
                "all_results": [],
                "needs_retry": True,
            }

        # Terminal result
        is_failure = msg.task_status in (TaskStatus.ABANDONED, TaskStatus.NEEDS_REVISION)
        record.status = ChildStatus.FAILED if is_failure else ChildStatus.DONE
        record.result = msg

        group = self._groups.get(record.group_id)
        if group is not None:
            group.results.append(msg)
            if group.complete:
                return {
                    "child_path": child_path,
                    "group_complete": True,
                    "group_id": record.group_id,
                    "all_results": list(group.results),
                    "needs_retry": False,
                }

        return {
            "child_path": child_path,
            "group_complete": False,
            "group_id": record.group_id,
            "all_results": [],
            "needs_retry": False,
        }

    # --- reparenting ------------------------------------------------------

    def reparent(self, orphan_paths: list[str], to: str) -> list[str]:
        """Move orphans to a new parent. Returns the list of actually reparented paths.

        On parent death, children are reparented to the GRANDPARENT (one level
        up). This works because routing is by address prefix (P1's routing.py),
        not by supervisor ownership -- so in-flight results still deliver.

        The reparented children's paths are NOT rewritten (the protocol envelope
        carries the sender path and rewriting it would break signatures). Only
        the supervisor's ownership record changes. The grandparent's supervisor
        now tracks these children and will collect their results.
        """
        reparented: list[str] = []
        for path in orphan_paths:
            record = self._children.pop(path, None)
            if record is not None:
                record.status = ChildStatus.PENDING  # reset: new parent will track
                reparented.append(path)

        return reparented

    def adopt(self, child_path: str, group_id: str, budget_slice: Budget) -> None:
        """Take ownership of an orphaned child (called on the grandparent)."""
        self._children[child_path] = ChildRecord(
            path=child_path,
            group_id=group_id,
            budget_slice=budget_slice,
        )

    def orphans_of(self, dead_peer: str) -> list[str]:
        """Children whose paths start with the dead peer's path.

        Used to find which children need reparenting when a peer dies.
        """
        prefix = dead_peer + "/"
        return [
            path for path, record in self._children.items()
            if path.startswith(prefix) and record.status not in (
                ChildStatus.DONE, ChildStatus.FAILED,
            )
        ]

    # --- on_child_crash ---------------------------------------------------

    def on_child_crash(self, child_path: str) -> dict[str, Any]:
        """A child crashed (delivery failed, timeout, etc.).

        Returns:
          - ``"should_retry"``: True if retries remain
          - ``"child_path"``: the crashed child
          - ``"budget_slice"``: the original budget for a retry
          - ``"original_request"``: the message that spawned the child
        """
        record = self._children.get(child_path)
        if record is None:
            return {
                "should_retry": False,
                "child_path": child_path,
                "budget_slice": None,
                "original_request": None,
            }

        if record.retries < MAX_RETRIES:
            record.retries += 1
            record.status = ChildStatus.RETRYING
            return {
                "should_retry": True,
                "child_path": child_path,
                "budget_slice": record.budget_slice,
                "original_request": record.original_request,
            }

        record.status = ChildStatus.FAILED
        return {
            "should_retry": False,
            "child_path": child_path,
            "budget_slice": record.budget_slice,
            "original_request": record.original_request,
        }

    # --- queries ----------------------------------------------------------

    @property
    def active_count(self) -> int:
        """Number of children not in a terminal state."""
        return sum(
            1 for r in self._children.values()
            if r.status not in (ChildStatus.DONE, ChildStatus.FAILED)
        )

    def active_children(self) -> list[str]:
        """Paths of all non-terminal children. For /health and the dashboard."""
        return [
            path for path, r in self._children.items()
            if r.status not in (ChildStatus.DONE, ChildStatus.FAILED)
        ]

    def child_record(self, path: str) -> ChildRecord | None:
        return self._children.get(path)

    def group_status(self, group_id: str) -> dict[str, Any]:
        """Summary of a fan-out group's progress."""
        group = self._groups.get(group_id)
        if group is None:
            return {"group_id": group_id, "exists": False}
        return {
            "group_id": group_id,
            "exists": True,
            "expected": group.expected,
            "received": len(group.results),
            "complete": group.complete,
        }
