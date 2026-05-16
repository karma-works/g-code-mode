# ADR-003: State Persistence — Local SQLite

**Status:** Decided  
**Date:** 2026-05-16

## Context

The safety stack requires durable state across operations and server restarts:

- **Operation tracking** — in-flight Vertex AI long-running operation names, so a timed-out deploy can be resumed.
- **Snapshots** — resource state captured before a mutating `execute` call, so rollback has something to restore from.
- **Undo registry** — the inverse operation paired with each executed action, returned in the response and stored for programmatic recall.
- **Execution log** — a record of every `execute` call, its outcome, and its undo recipe, for auditability.

State must survive MCP server restarts (the developer's Claude Code session may restart between operations). State does not need to be shared across machines or users.

Alternatives considered: in-memory Python dict, local JSON file, local SQLite, Firestore, Redis.

## Decision

Use a local SQLite database at `~/.g-code-mode/state.db`.

Schema (initial):

```sql
CREATE TABLE operations (
    id          TEXT PRIMARY KEY,   -- uuid
    created_at  TEXT NOT NULL,      -- ISO 8601
    type        TEXT NOT NULL,      -- 'deploy_agent_engine' | 'delete_agent_engine' | ...
    status      TEXT NOT NULL,      -- 'in_flight' | 'completed' | 'failed' | 'rolled_back'
    params      TEXT NOT NULL,      -- JSON: the execute call params
    snapshot    TEXT,               -- JSON: resource state before mutation (nullable)
    result      TEXT,               -- JSON: operation result after completion
    undo_recipe TEXT,               -- JSON: { description, call } to reverse this operation
    gcp_op_name TEXT                -- long-running operation name for resume (nullable)
);
```

Accessed via Python's built-in `sqlite3` module. No ORM.

## Rationale

- **Zero external dependencies.** SQLite is in the Python standard library. No daemon to run, no connection string to configure, no credentials.
- **Survives restarts.** Unlike in-memory state, SQLite persists across server restarts. A developer can resume an in-flight Agent Engine deploy after their laptop wakes from sleep.
- **Queryable.** If a developer needs to inspect past operations (`SELECT * FROM operations WHERE status = 'in_flight'`), they can open the file directly with any SQLite client.
- **Appropriate scale.** Operations are low-frequency (tens per day at most for a developer tool). SQLite handles this comfortably.
- **Simpler than JSON files.** A single append-only JSON file risks corruption on concurrent writes. SQLite's WAL mode handles concurrent access correctly.

## What This Option Does NOT Do Well

- **Not shareable.** State is local to one developer's machine. A team sharing a remote g-code-mode server would need a shared database. This is out of scope for v1 (stdio transport, local install).
- **No automatic pruning.** Old completed operations accumulate. Add a `--prune` CLI flag in a future release to delete operations older than N days.
- **SQLite is not a job queue.** Long-running operation polling (e.g., waiting for Agent Engine deploy to complete) happens in-process, not via a persistent queue. If the server dies mid-poll, the operation is resumed on the next `inquire` or `execute` call that triggers a status check.

## Consequences

- Database file location: `~/.g-code-mode/state.db`. Configurable via `G_CODE_MODE_STATE_PATH` env var.
- Schema migrations are managed with a simple version table and hand-written `ALTER TABLE` statements. No migration framework needed at this scale.
- The `operations` table is append-only for audit purposes. Rollback updates `status` to `rolled_back`; it does not delete the original row.
- All JSON fields use `json.dumps` / `json.loads`. No SQLite JSON functions required (keeps the minimum SQLite version low).
