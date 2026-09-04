# OWNER: P3 -- see docs/WORK.md. Do not edit if you are not P3.
"""Content-addressed artifact store -- how git works.

Four agents with 200K windows is not an 800K pool. Naive history-passing
means all four hold the SAME tokens, so the effective pool is 200K duplicated
four times. Dedup is the entire win: partition, do not broadcast.

Messages carry refs and summaries. The receiver fetches GET /blob/{hash} only
if it decides it needs the bytes -- and `tokens` tells it the price first.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from samvad.protocol import ArtifactRef

SCHEMA = Path(__file__).with_name("schema.sql")

#: Bytes per token, for the pre-call estimate only.
#:
#: This is the ONE number in the store that is not measured. `tokens` has to be
#: known before anything is sent -- it is what lets a receiver decide whether to
#: hydrate BEFORE it pays -- and the only exact source is the API's own counter,
#: which costs a call. ~4 bytes/token is the usual figure for code and English.
#:
#: It is deliberately NOT used for context accounting: ContextTracker reads the
#: API's reported input tokens instead, because docs/PROTOCOL.md is explicit
#: that backpressure on a guess is worthless.
BYTES_PER_TOKEN = 4


def count_tokens(data: bytes) -> int:
    """Estimated hydration cost of a blob. See BYTES_PER_TOKEN."""
    return max(1, (len(data) + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN)


def summarise(data: bytes, kind: str) -> str:
    """<= 200 chars. What the receiver decides from without fetching.

    First real line plus the size: enough to recognise a traceback or a diff,
    cheap enough to put in every message that references it.
    """
    head = ""
    for raw in data[:400].splitlines():
        line = raw.decode("utf-8", "replace").strip()
        if line:
            head = line
            break
    label = f"{kind}, {len(data)} bytes"
    summary = f"{label} -- {head}" if head else label
    return summary[:200]


class BlobStore:
    """sha256-addressed bytes. Storing the same content twice stores it once."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA.read_text(encoding="utf-8"))

    def put(self, data: bytes, kind: str) -> ArtifactRef:
        """-> ArtifactRef with sha256 ref, summary and token count.

        The ref is DERIVED from the content, never assigned. That is what makes
        the same bytes from two different agents collapse to one row without
        either of them coordinating -- which is the whole mechanism.
        """
        ref = "sha256:" + hashlib.sha256(data).hexdigest()
        artifact = ArtifactRef(
            ref=ref, kind=kind, summary=summarise(data, kind), tokens=count_tokens(data)
        )
        # INSERT OR IGNORE, not INSERT: a re-put of identical content is the
        # normal case, not an error. Content addressing means the row that is
        # already there is byte-identical to the one being written.
        self._db.execute(
            "INSERT OR IGNORE INTO blobs (ref, kind, summary, tokens, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ref, kind, artifact.summary, artifact.tokens, data,
             datetime.now(UTC).isoformat()),
        )
        self._db.commit()
        return artifact

    def get(self, ref: str) -> bytes | None:
        row = self._db.execute("SELECT data FROM blobs WHERE ref = ?", (ref,)).fetchone()
        return row[0] if row else None

    def has(self, ref: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM blobs WHERE ref = ?", (ref,)).fetchone() is not None
        )

    def meta(self, ref: str) -> ArtifactRef | None:
        """The ref without the bytes -- for deciding whether to hydrate."""
        row = self._db.execute(
            "SELECT ref, kind, summary, tokens FROM blobs WHERE ref = ?", (ref,)
        ).fetchone()
        return ArtifactRef(ref=row[0], kind=row[1], summary=row[2], tokens=row[3]) if row else None

    def close(self) -> None:
        self._db.close()
