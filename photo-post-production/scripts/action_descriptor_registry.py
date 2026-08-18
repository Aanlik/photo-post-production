"""Local registry for reviewed Photoshop batchPlay Action Descriptors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_descriptors (
    descriptor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    photoshop_version TEXT NOT NULL,
    document_modes_json TEXT NOT NULL,
    bit_depths_json TEXT NOT NULL,
    parameter_schema_json TEXT NOT NULL,
    contains_raw_data INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS action_descriptors_operation
    ON action_descriptors(operation_type, verified);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def init_registry(db_path: str) -> None:
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path):
        pass


def _validate_descriptor(descriptor: Any) -> Any:
    if not isinstance(descriptor, (dict, list)):
        raise ValueError("descriptor must be JSON object or array, not executable source text")
    encoded = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, allow_nan=False)
    decoded = json.loads(encoded)
    commands = decoded if isinstance(decoded, list) else [decoded]
    if not commands or any(not isinstance(item, dict) or not isinstance(item.get("_obj"), str) or not item["_obj"].strip() for item in commands):
        raise ValueError("every descriptor command requires a non-empty _obj")
    return decoded


def register_descriptor(
    db_path: str,
    name: str,
    operation_type: str,
    descriptor: Any,
    photoshop_version: str,
    document_modes: list[str],
    bit_depths: list[int],
    parameter_schema: dict[str, Any],
    contains_raw_data: bool = False,
) -> dict[str, Any]:
    if not all(isinstance(value, str) and value.strip() for value in (name, operation_type, photoshop_version)):
        raise ValueError("name, operation_type and photoshop_version are required")
    if not document_modes or not bit_depths or not isinstance(parameter_schema, dict):
        raise ValueError("document modes, bit depths and parameter schema are required")
    normalized = _validate_descriptor(descriptor)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    descriptor_id = hashlib.sha256(f"{name}|{operation_type}|{photoshop_version}|{canonical}".encode("utf-8")).hexdigest()[:24]
    init_registry(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO action_descriptors
               (descriptor_id, name, operation_type, descriptor_json, photoshop_version,
                document_modes_json, bit_depths_json, parameter_schema_json,
                contains_raw_data, verified, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (descriptor_id, name.strip(), operation_type.strip(), canonical, photoshop_version.strip(),
             json.dumps(sorted(set(document_modes))), json.dumps(sorted(set(int(value) for value in bit_depths))),
             json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), int(bool(contains_raw_data)),
             datetime.now(timezone.utc).isoformat(timespec="microseconds")),
        )
    return {"descriptor_id": descriptor_id, "name": name, "operation_type": operation_type, "verified": True}


def find_compatible_descriptor(
    db_path: str,
    operation_type: str,
    photoshop_version: str,
    document_mode: str,
    bit_depth: int,
) -> dict[str, Any] | None:
    init_registry(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM action_descriptors WHERE operation_type=? AND verified=1 ORDER BY created_at DESC",
            (operation_type,),
        ).fetchall()
    for row in rows:
        modes = json.loads(row["document_modes_json"])
        depths = json.loads(row["bit_depths_json"])
        registered_version = str(row["photoshop_version"])
        if document_mode not in modes or int(bit_depth) not in depths:
            continue
        if not str(photoshop_version).startswith(registered_version):
            continue
        return {
            "descriptor_id": row["descriptor_id"],
            "name": row["name"],
            "operation_type": row["operation_type"],
            "descriptor": json.loads(row["descriptor_json"]),
            "photoshop_version": registered_version,
            "parameter_schema": json.loads(row["parameter_schema_json"]),
            "contains_raw_data": bool(row["contains_raw_data"]),
            "verified": True,
        }
    return None


def get_descriptor(db_path: str, descriptor_id: str) -> dict[str, Any] | None:
    """Load one verified descriptor by immutable ID for adapter execution."""

    init_registry(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM action_descriptors WHERE descriptor_id=? AND verified=1",
            (str(descriptor_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "descriptor_id": row["descriptor_id"],
        "name": row["name"],
        "operation_type": row["operation_type"],
        "descriptor": json.loads(row["descriptor_json"]),
        "photoshop_version": row["photoshop_version"],
        "parameter_schema": json.loads(row["parameter_schema_json"]),
        "contains_raw_data": bool(row["contains_raw_data"]),
        "verified": True,
    }
