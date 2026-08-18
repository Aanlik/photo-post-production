"""Small local pairwise preference model with versioned SQLite storage."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference_models (
    profile_name TEXT NOT NULL,
    category TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    feature_names_json TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    validation_accuracy REAL NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(profile_name, category, version)
);
CREATE INDEX IF NOT EXISTS preference_models_active
    ON preference_models(profile_name, category, active, version);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def init_preference_store(db_path: str) -> None:
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path):
        pass


def _feature_names(comparisons: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for comparison in comparisons:
        for side in ("better", "worse"):
            values = comparison.get(side)
            if isinstance(values, dict):
                names.update(str(key) for key, value in values.items() if isinstance(value, (int, float)) and math.isfinite(float(value)))
    return sorted(names)


def _difference(comparison: dict[str, Any], names: list[str]) -> list[float]:
    better = comparison.get("better") if isinstance(comparison.get("better"), dict) else {}
    worse = comparison.get("worse") if isinstance(comparison.get("worse"), dict) else {}
    return [float(better.get(name, 0.0)) - float(worse.get(name, 0.0)) for name in names]


def _sigmoid(value: float) -> float:
    value = max(-40.0, min(40.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def train_preference_model(
    db_path: str,
    profile_name: str,
    category: str,
    comparisons: list[dict[str, Any]],
    min_samples: int = 6,
) -> dict[str, Any]:
    valid = [item for item in comparisons if isinstance(item, dict) and isinstance(item.get("better"), dict) and isinstance(item.get("worse"), dict)]
    if len(valid) < min_samples:
        return {"status": "insufficient-evidence", "sample_count": len(valid), "required": min_samples, "category": category}
    names = _feature_names(valid)
    if not names:
        return {"status": "insufficient-features", "sample_count": len(valid), "category": category}
    weights = [0.0 for _ in names]
    learning_rate = 0.35 / max(1, len(names))
    for _ in range(600):
        gradient = [0.0 for _ in names]
        for comparison in valid:
            difference = _difference(comparison, names)
            probability = _sigmoid(sum(weight * value for weight, value in zip(weights, difference)))
            event_weight = max(0.01, float(comparison.get("weight", 1.0)))
            for index, value in enumerate(difference):
                gradient[index] += (1.0 - probability) * value * event_weight
        for index in range(len(weights)):
            weights[index] += learning_rate * gradient[index] / len(valid)
            weights[index] *= 0.999
    margins = [sum(weight * value for weight, value in zip(weights, _difference(item, names))) for item in valid]
    accuracy = sum(margin > 0 for margin in margins) / len(margins)
    status = "active" if accuracy >= 0.60 else "rejected-validation"
    init_preference_store(db_path)
    with _connect(db_path) as connection:
        previous = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM preference_models WHERE profile_name=? AND category=?",
            (profile_name, category),
        ).fetchone()[0]
        version = int(previous) + 1
        if status == "active":
            connection.execute("UPDATE preference_models SET active=0 WHERE profile_name=? AND category=?", (profile_name, category))
        connection.execute(
            """INSERT INTO preference_models
               (profile_name, category, version, status, feature_names_json, weights_json,
                sample_count, validation_accuracy, created_at, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_name, category, version, status, json.dumps(names), json.dumps(weights), len(valid), accuracy, _now(), 1 if status == "active" else 0),
        )
    return {
        "status": status,
        "version": version,
        "category": category,
        "sample_count": len(valid),
        "validation_accuracy": round(accuracy, 4),
        "feature_names": names,
        "weights": {name: round(value, 6) for name, value in zip(names, weights)},
    }


def apply_preference_model(db_path: str, profile_name: str, category: str, features: dict[str, Any]) -> dict[str, Any]:
    init_preference_store(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            """SELECT * FROM preference_models
               WHERE profile_name=? AND category=? AND active=1 AND status='active'
               ORDER BY version DESC LIMIT 1""",
            (profile_name, category),
        ).fetchone()
    if row is None:
        return {"status": "no-active-model", "preference_fit": 50.0, "model_version": None, "category": category}
    names = json.loads(row["feature_names_json"])
    weights = json.loads(row["weights_json"])
    margin = sum(float(weight) * float(features.get(name, 0.0)) for name, weight in zip(names, weights))
    fit = _sigmoid(margin) * 100.0
    return {
        "status": "applied",
        "preference_fit": round(fit, 4),
        "model_version": int(row["version"]),
        "validation_accuracy": float(row["validation_accuracy"]),
        "category": category,
    }


def features_from_score(score: dict[str, Any]) -> dict[str, float]:
    """Derive bounded, model-agnostic preference features from score metadata."""

    technical = score.get("technical_analysis") if isinstance(score.get("technical_analysis"), dict) else {}
    visual = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    crop = score.get("proposed_crop") if isinstance(score.get("proposed_crop"), dict) else {}
    try:
        crop_fraction = 1.0 - max(0.0, min(1.0, float(crop.get("area_fraction", 1.0))))
    except (TypeError, ValueError):
        crop_fraction = 0.0
    return {
        "brightness": max(0.0, min(1.0, float(technical.get("mean_luma", score.get("mean_luma", 0.5)) or 0.5))),
        "contrast": max(0.0, min(1.0, float(score.get("light_color", 50.0)) / 100.0)),
        "composition": max(0.0, min(1.0, float(score.get("composition", 50.0)) / 100.0)),
        "moment_story": max(0.0, min(1.0, float(score.get("moment_story", 50.0)) / 100.0)),
        "crop_tightness": crop_fraction,
        "face_presence": 1.0 if int(visual.get("faces", 0) or 0) > 0 else 0.0,
    }
