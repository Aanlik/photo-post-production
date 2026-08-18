"""Small SQLite-backed pausable queue for per-photo work isolation."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATES = {"queued", "processing", "paused", "completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect(path: str) -> sqlite3.Connection:
    parent = Path(path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS queue_items (
        item_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        photo_id TEXT NOT NULL,
        state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        checkpoint_json TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS queue_run_state ON queue_items(run_id, state, updated_at)")
    return connection


def enqueue(queue_path: str, run_id: str, photo_id: str, payload: dict[str, Any], item_id: str | None = None) -> str:
    if not run_id.strip() or not photo_id.strip() or not isinstance(payload, dict):
        raise ValueError("run_id, photo_id, and payload are required")
    key = item_id or f"{run_id}:{photo_id}"
    now = _now()
    with _connect(queue_path) as connection:
        connection.execute(
            """INSERT INTO queue_items(item_id, run_id, photo_id, state, payload_json, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (key, run_id, photo_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), now, now),
        )
    return key


def claim_next(queue_path: str, run_id: str) -> dict[str, Any] | None:
    with _connect(queue_path) as connection:
        row = connection.execute(
            "SELECT * FROM queue_items WHERE run_id=? AND state='queued' ORDER BY created_at, item_id LIMIT 1", (run_id,)
        ).fetchone()
        if row is None:
            return None
        now = _now()
        connection.execute("UPDATE queue_items SET state='processing', attempts=attempts+1, updated_at=? WHERE item_id=? AND state='queued'", (now, row["item_id"]))
        connection.commit()
        return get_item(queue_path, row["item_id"])


def claim_item(queue_path: str, item_id: str) -> dict[str, Any] | None:
    """Claim one exact item for a resumable adapter retry."""
    with _connect(queue_path) as connection:
        row = connection.execute("SELECT * FROM queue_items WHERE item_id=? AND state='queued'", (item_id,)).fetchone()
        if row is None:
            return None
        now = _now()
        connection.execute(
            "UPDATE queue_items SET state='processing', attempts=attempts+1, updated_at=? WHERE item_id=? AND state='queued'",
            (now, item_id),
        )
        connection.commit()
    return get_item(queue_path, item_id)


def update_checkpoint(queue_path: str, item_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint must be an object")
    now = _now()
    with _connect(queue_path) as connection:
        connection.execute("UPDATE queue_items SET checkpoint_json=?, updated_at=? WHERE item_id=?", (json.dumps(checkpoint, ensure_ascii=False, sort_keys=True), now, item_id))
        row = connection.execute("SELECT state FROM queue_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        return {"item_id": item_id, "state": row["state"], "checkpoint": checkpoint}


def transition(queue_path: str, item_id: str, state: str, error: str | None = None) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"unsupported queue state: {state}")
    with _connect(queue_path) as connection:
        row = connection.execute("SELECT state FROM queue_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        current = row["state"]
        allowed = {
            "queued": {"processing", "cancelled"},
            "processing": {"paused", "completed", "failed", "cancelled"},
            "paused": {"queued", "cancelled"},
            "failed": {"queued", "cancelled"},
            "completed": set(),
            "cancelled": set(),
        }
        if state not in allowed[current]:
            raise ValueError(f"invalid queue transition {current}->{state}")
        now = _now()
        connection.execute("UPDATE queue_items SET state=?, error=?, updated_at=? WHERE item_id=?", (state, error, now, item_id))
    return get_item(queue_path, item_id) or {}


def get_item(queue_path: str, item_id: str) -> dict[str, Any] | None:
    with _connect(queue_path) as connection:
        row = connection.execute("SELECT * FROM queue_items WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        return None
    return {
        "item_id": row["item_id"], "run_id": row["run_id"], "photo_id": row["photo_id"],
        "state": row["state"], "payload": json.loads(row["payload_json"]),
        "checkpoint": json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None,
        "attempts": row["attempts"], "error": row["error"], "updated_at": row["updated_at"],
    }


def summarize(queue_path: str, run_id: str) -> dict[str, Any]:
    with _connect(queue_path) as connection:
        rows = connection.execute("SELECT state, COUNT(*) AS count FROM queue_items WHERE run_id=? GROUP BY state", (run_id,)).fetchall()
    counts = {state: 0 for state in STATES}
    counts.update({row["state"]: row["count"] for row in rows})
    return {"run_id": run_id, "counts": counts, "complete": counts["completed"] + counts["rejected"] if "rejected" in counts else counts["completed"]}
