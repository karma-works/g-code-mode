"""SQLite state manager — persists operation state across server restarts (ADR-003)."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(
    os.environ.get("G_CODE_MODE_STATE_PATH", "~/.g-code-mode/state.db")
).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);

CREATE TABLE IF NOT EXISTS operations (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,
    params      TEXT NOT NULL,
    snapshot    TEXT,
    result      TEXT,
    undo_recipe TEXT,
    gcp_op_name TEXT
);
"""


class StateManager:
    def __init__(self, path: Path = _DEFAULT_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── write ──────────────────────────────────────────────────────────────

    def create_operation(self, op_type: str, params: dict[str, Any]) -> str:
        op_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO operations (id, created_at, type, status, params) VALUES (?,?,?,?,?)",
            (op_id, _now(), op_type, "in_flight", json.dumps(params)),
        )
        self._conn.commit()
        return op_id

    def update_status(
        self,
        op_id: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE operations SET status=?, result=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None, op_id),
        )
        self._conn.commit()

    def set_snapshot(self, op_id: str, snapshot: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE operations SET snapshot=? WHERE id=?",
            (json.dumps(snapshot), op_id),
        )
        self._conn.commit()

    def set_undo_recipe(self, op_id: str, recipe: dict[str, str]) -> None:
        self._conn.execute(
            "UPDATE operations SET undo_recipe=? WHERE id=?",
            (json.dumps(recipe), op_id),
        )
        self._conn.commit()

    def set_gcp_op_name(self, op_id: str, gcp_op_name: str) -> None:
        self._conn.execute(
            "UPDATE operations SET gcp_op_name=? WHERE id=?",
            (gcp_op_name, op_id),
        )
        self._conn.commit()

    # ── read ───────────────────────────────────────────────────────────────

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE id=?", (op_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_in_flight(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM operations WHERE status='in_flight' ORDER BY created_at"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for field in ("params", "snapshot", "result", "undo_recipe"):
        if d.get(field):
            d[field] = json.loads(d[field])
    return d
