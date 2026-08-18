"""Normalize optional local model signals without making them authoritative."""

from __future__ import annotations

import math
from typing import Any


SIGNAL_SCHEMA_VERSION = "model-signals-2026.08.17-v1"


def _bounded(value: Any, lower: float, upper: float, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(lower, min(upper, parsed))


def normalize_model_signals(payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, dict[str, Any]] = {}
    evidence: list[str] = []
    for name, raw in (payload or {}).items():
        if not isinstance(raw, dict):
            continue
        model = str(raw.get("model") or "unknown").strip() or "unknown"
        version = str(raw.get("version") or "unknown").strip() or "unknown"
        item = {
            "score": round(_bounded(raw.get("score"), 0.0, 100.0), 4),
            "confidence": round(_bounded(raw.get("confidence"), 0.0, 1.0), 4),
            "model": model,
            "version": version,
        }
        if isinstance(raw.get("details"), dict):
            item["details"] = raw["details"]
        normalized[str(name)] = item
        evidence.append(f"{model}:{version}")
    confidence_weight = sum(item["confidence"] for item in normalized.values())
    weighted_score = (
        sum(item["score"] * item["confidence"] for item in normalized.values()) / confidence_weight
        if confidence_weight > 0 else None
    )
    return {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "signals": normalized,
        "evidence": sorted(set(evidence)),
        "aggregate_score": round(weighted_score, 4) if weighted_score is not None else None,
        "authoritative": False,
    }
