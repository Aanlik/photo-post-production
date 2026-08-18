"""Metadata-only local style memory backed by SQLite.

Feedback is evidence, not an immediate global style mutation.  Derived guidance
is emitted only after repeated matching events, which keeps a single correction
from causing uncontrolled drift.
"""

from __future__ import annotations

import math
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile_name TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0,
    reset_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reset_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    photo_id TEXT,
    text TEXT,
    affected_region TEXT,
    better_photo_id TEXT,
    worse_photo_id TEXT,
    aspect TEXT,
    weight REAL NOT NULL,
    source_run_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    reset_version INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (profile_name) REFERENCES profiles(profile_name),
    FOREIGN KEY (source_run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS preference_events_active_profile
    ON preference_events(profile_name, active, event_kind);
CREATE TABLE IF NOT EXISTS reference_links (
    reference_id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    title TEXT NOT NULL,
    creator TEXT,
    source_url TEXT NOT NULL,
    license TEXT NOT NULL,
    category TEXT,
    attributes_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (profile_name) REFERENCES profiles(profile_name)
);
CREATE INDEX IF NOT EXISTS reference_links_active_profile
    ON reference_links(profile_name, active, category);
CREATE TABLE IF NOT EXISTS style_recipes (
    profile_name TEXT NOT NULL,
    category TEXT NOT NULL,
    version INTEGER NOT NULL,
    recipe_json TEXT NOT NULL,
    evidence_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_name, category),
    FOREIGN KEY (profile_name) REFERENCES profiles(profile_name)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _ensure_profile(connection: sqlite3.Connection, profile_name: str) -> None:
    if not profile_name or not profile_name.strip():
        raise ValueError("profile_name must be non-empty")
    now = _now()
    connection.execute(
        "INSERT OR IGNORE INTO profiles(profile_name, created_at, updated_at) VALUES (?, ?, ?)",
        (profile_name, now, now),
    )


def init_store(db_path: str) -> None:
    """Create the local metadata store, including its three required tables."""
    parent = Path(db_path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, "default")


def _scope_for_run(run_id: str) -> tuple[str, str]:
    """Return (profile, source-run-id); project runs use project:<name>:<id>."""
    if not run_id or not run_id.strip():
        raise ValueError("run_id must be non-empty")
    match = re.match(r"^project(?::|/)([^:/]+)(?::|/)(.+)$", run_id)
    if match:
        return f"project:{match.group(1)}", run_id
    return "default", run_id


def _validate_weight(weight: float) -> float:
    value = float(weight)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("weight must be finite and greater than zero")
    return value


def _ensure_run(connection: sqlite3.Connection, run_id: str, profile_name: str, now: str) -> None:
    connection.execute(
        """INSERT INTO runs(run_id, profile_name, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
        (run_id, profile_name, now, now),
    )


def record_feedback(
    db_path: str,
    run_id: str,
    photo_id: str,
    kind: str,
    text: str,
    region: str | None,
    weight: float = 1.0,
    aspect: str | None = None,
) -> int:
    """Record positive/negative structured feedback and return its event ID."""
    normalized_kind = {"like": "positive", "approve": "positive", "dislike": "negative", "reject": "negative"}.get(kind, kind)
    if normalized_kind not in {"positive", "negative"}:
        raise ValueError("kind must be positive or negative")
    if not text or not text.strip():
        raise ValueError("text must be non-empty")
    if not photo_id or not photo_id.strip():
        raise ValueError("photo_id must be non-empty")
    if region is not None and not region.strip():
        raise ValueError("region must be non-empty when provided")
    if aspect is not None and not aspect.strip():
        raise ValueError("aspect must be non-empty when provided")
    value = _validate_weight(weight)
    profile_name, source_run_id = _scope_for_run(run_id)
    now = _now()
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, profile_name)
        _ensure_run(connection, source_run_id, profile_name, now)
        version = connection.execute("SELECT version FROM profiles WHERE profile_name = ?", (profile_name,)).fetchone()[0] + 1
        cursor = connection.execute(
            """INSERT INTO preference_events
               (profile_name, event_kind, photo_id, text, affected_region, aspect, weight, source_run_id, recorded_at, reset_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_name, normalized_kind, photo_id, text, region, aspect, value, source_run_id, now, version),
        )
        connection.execute("UPDATE profiles SET version=?, updated_at=? WHERE profile_name=?", (version, now, profile_name))
        return int(cursor.lastrowid)


def record_pairwise_feedback(
    db_path: str,
    run_id: str,
    better_photo_id: str,
    worse_photo_id: str,
    aspect: str | None = None,
    weight: float = 1.0,
) -> int:
    """Record a pairwise preference without storing either image."""
    if not better_photo_id or not worse_photo_id or better_photo_id == worse_photo_id:
        raise ValueError("pairwise photos must be distinct and non-empty")
    if aspect is not None and not aspect.strip():
        raise ValueError("aspect must be non-empty when provided")
    value = _validate_weight(weight)
    profile_name, source_run_id = _scope_for_run(run_id)
    now = _now()
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, profile_name)
        _ensure_run(connection, source_run_id, profile_name, now)
        version = connection.execute("SELECT version FROM profiles WHERE profile_name = ?", (profile_name,)).fetchone()[0] + 1
        cursor = connection.execute(
            """INSERT INTO preference_events
               (profile_name, event_kind, better_photo_id, worse_photo_id, aspect, weight, source_run_id, recorded_at, reset_version)
               VALUES (?, 'pairwise', ?, ?, ?, ?, ?, ?, ?)""",
            (profile_name, better_photo_id, worse_photo_id, aspect, value, source_run_id, now, version),
        )
        connection.execute("UPDATE profiles SET version=?, updated_at=? WHERE profile_name=?", (version, now, profile_name))
        return int(cursor.lastrowid)


def _event_dict(row: sqlite3.Row) -> dict:
    result = {
        "event_id": row["event_id"],
        "kind": row["event_kind"],
        "weight": row["weight"],
        "source_run_id": row["source_run_id"],
        "recorded_at": row["recorded_at"],
    }
    for key in ("photo_id", "text", "affected_region", "better_photo_id", "worse_photo_id", "aspect"):
        result[key if key != "affected_region" else "region"] = row[key]
    return result


def get_profile(db_path: str, profile_name: str = "default") -> dict:
    """Return active evidence plus conservative, repeated-evidence guidance."""
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, profile_name)
        profile = connection.execute("SELECT * FROM profiles WHERE profile_name=?", (profile_name,)).fetchone()
        rows = connection.execute(
            "SELECT * FROM preference_events WHERE profile_name=? AND active=1 ORDER BY event_id", (profile_name,)
        ).fetchall()
        references = connection.execute(
            "SELECT * FROM reference_links WHERE profile_name=? AND active=1 ORDER BY reference_id", (profile_name,)
        ).fetchall()
        recipes = connection.execute(
            "SELECT * FROM style_recipes WHERE profile_name=? ORDER BY category", (profile_name,)
        ).fetchall()

    feedback = {"positive": [], "negative": []}
    pairwise = []
    groups: dict[tuple[str, str | None, str | None], list[sqlite3.Row]] = {}
    for row in rows:
        event = _event_dict(row)
        if row["event_kind"] == "pairwise":
            pairwise.append(event)
        else:
            feedback[row["event_kind"]].append(event)
            key = (row["event_kind"], row["text"], row["affected_region"], row["aspect"])
            groups.setdefault(key, []).append(row)

    guidance = []
    for (kind, text, region, aspect), evidence in groups.items():
        if len(evidence) < 2:
            continue
        total_weight = sum(float(row["weight"]) for row in evidence)
        guidance.append({
            "kind": kind,
            "text": text,
            "region": region,
            "aspect": aspect,
            "supporting_events": len(evidence),
            "strength": min(1.0, total_weight / 3.0),
            "source_run_ids": [row["source_run_id"] for row in evidence],
        })
    return {
        "profile_name": profile_name,
        "version": profile["version"],
        "reset_count": profile["reset_count"],
        "feedback": feedback,
        "pairwise": pairwise,
        "guidance": guidance,
        "references": [
            {
                "reference_id": row["reference_id"],
                "title": row["title"],
                "creator": row["creator"],
                "source_url": row["source_url"],
                "license": row["license"],
                "category": row["category"],
                "attributes": json.loads(row["attributes_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in references
        ],
        "style_recipes": [
            {
                "category": row["category"],
                "version": row["version"],
                "evidence_count": row["evidence_count"],
                "recipe": json.loads(row["recipe_json"]),
                "updated_at": row["updated_at"],
            }
            for row in recipes
        ],
    }


def record_reference(
    db_path: str,
    profile_name: str,
    title: str,
    source_url: str,
    license_name: str,
    category: str | None,
    attributes: dict,
    creator: str | None = None,
) -> int:
    """Store reference provenance and derived attributes, never image bytes."""
    if not title.strip() or not source_url.strip():
        raise ValueError("reference title and source_url must be non-empty")
    if not license_name.strip():
        raise ValueError("reference license must be explicit")
    if not isinstance(attributes, dict):
        raise ValueError("reference attributes must be an object")
    try:
        encoded = json.dumps(attributes, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError("reference attributes must be JSON serializable") from error
    now = _now()
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, profile_name)
        cursor = connection.execute(
            """INSERT INTO reference_links
               (profile_name, title, creator, source_url, license, category, attributes_json, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_name, title.strip(), creator.strip() if isinstance(creator, str) and creator.strip() else None,
             source_url.strip(), license_name.strip(), category.strip() if isinstance(category, str) and category.strip() else None,
             encoded, now),
        )
        version = connection.execute("SELECT version FROM profiles WHERE profile_name=?", (profile_name,)).fetchone()[0] + 1
        connection.execute("UPDATE profiles SET version=?, updated_at=? WHERE profile_name=?", (version, now, profile_name))
        return int(cursor.lastrowid)


def save_style_recipe(db_path: str, profile_name: str, category: str, recipe: dict, evidence_count: int) -> int:
    """Version and persist a derived, executable style recipe."""
    if not category.strip() or not isinstance(recipe, dict):
        raise ValueError("category and recipe are required")
    if not isinstance(evidence_count, int) or evidence_count < 0:
        raise ValueError("evidence_count must be a non-negative integer")
    now = _now()
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, profile_name)
        old = connection.execute("SELECT version FROM style_recipes WHERE profile_name=? AND category=?", (profile_name, category)).fetchone()
        version = (old[0] if old else 0) + 1
        connection.execute(
            """INSERT INTO style_recipes(profile_name, category, version, recipe_json, evidence_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(profile_name, category) DO UPDATE SET
               version=excluded.version, recipe_json=excluded.recipe_json,
               evidence_count=excluded.evidence_count, updated_at=excluded.updated_at""",
            (profile_name, category, version, json.dumps(recipe, ensure_ascii=False, sort_keys=True), evidence_count, now),
        )
        profile_version = connection.execute("SELECT version FROM profiles WHERE profile_name=?", (profile_name,)).fetchone()[0] + 1
        connection.execute("UPDATE profiles SET version=?, updated_at=? WHERE profile_name=?", (profile_version, now, profile_name))
        return version


def reset_profile(db_path: str, profile_name: str = "default") -> None:
    """Soft-reset one profile while preserving audit history and run IDs."""
    now = _now()
    with _connect(db_path) as connection:
        connection.executescript(_SCHEMA)
        _ensure_profile(connection, profile_name)
        profile = connection.execute("SELECT version, reset_count FROM profiles WHERE profile_name=?", (profile_name,)).fetchone()
        version = profile["version"] + 1
        connection.execute(
            "UPDATE preference_events SET active=0, reset_version=? WHERE profile_name=? AND active=1",
            (version, profile_name),
        )
        connection.execute(
            "UPDATE profiles SET version=?, reset_count=?, reset_at=?, updated_at=? WHERE profile_name=?",
            (version, profile["reset_count"] + 1, now, now, profile_name),
        )
