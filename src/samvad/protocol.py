# OWNER: P1 -- see docs/WORK.md. Do not edit if you are not P1.
"""The frozen envelope. Spec: docs/PROTOCOL.md

===========================================================================
WEEK 1, JOINT TASK. All four people write this together, then tag it.

This is the single highest-leverage hour of the project. Four AI assistants
each asked to "build the agent server" will confidently design four
incompatible envelopes that all look reasonable. Writing it together, once,
is the only defence.

Build it field by field from docs/PROTOCOL.md:

  identity   protocol_version message_id conversation_id reply_to turn
             lamport timestamp
  addressing sender receiver performative
  content    root_task payload task_status
  artifacts  list[ArtifactRef]   ref/kind/summary/tokens
  claims     list[Claim]         claim/evidence/evidence_type
  spawn      Spawn               parent/depth/max_depth
  budget     Budget              usd_remaining/turns_remaining   CONSERVED
  context    ContextState        used/limit
  cost       Cost                input/output/cache_read/usd/model
  signature  sig

Then:  git tag protocol-v2.0    and nobody changes it alone.
===========================================================================
"""
from enum import StrEnum


class Performative(StrEnum):
    TASK_REQUEST = "task_request"
    TASK_RESULT = "task_result"
    SPAWN_REQUEST = "spawn_request"
    SPAWN_ACK = "spawn_ack"
    SPAWN_REFUSED = "spawn_refused"
    CHILD_RESULT = "child_result"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNCERTAIN = "uncertain"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    NEEDS_REVISION = "needs_revision"
    UNCERTAIN = "uncertain"
    ABANDONED = "abandoned"


class EvidenceType(StrEnum):
    TEST_OUTPUT = "test_output"      # grounded
    FILE_CONTENT = "file_content"    # grounded
    PEER_REPORT = "peer_report"      # grounded only if its source is
    MODEL_PRIOR = "model_prior"      # NOT grounded -- counted as such


PROTOCOL_VERSION = "2.0"
