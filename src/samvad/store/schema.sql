-- P3. One database per agent.

CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,       -- idempotency key
    conversation_id TEXT NOT NULL,
    reply_to        TEXT,
    turn            INTEGER NOT NULL,
    lamport         INTEGER NOT NULL,
    sender          TEXT NOT NULL,
    receiver        TEXT NOT NULL,
    performative    TEXT NOT NULL,
    task_status     TEXT NOT NULL,
    body            TEXT NOT NULL,          -- full envelope as JSON
    response_id     TEXT,                   -- cached reply, for idempotency
    received_at     TEXT NOT NULL
);

-- ordering is (lamport, sender). NEVER by timestamp: four devices, four clocks.
CREATE INDEX IF NOT EXISTS idx_order  ON messages (lamport, sender);
CREATE INDEX IF NOT EXISTS idx_convo  ON messages (conversation_id, turn);

CREATE TABLE IF NOT EXISTS blobs (
    ref        TEXT PRIMARY KEY,            -- sha256:...
    kind       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    tokens     INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS costs (
    message_id TEXT PRIMARY KEY,
    input      INTEGER NOT NULL,
    output     INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    usd        REAL NOT NULL,
    model      TEXT NOT NULL
);
