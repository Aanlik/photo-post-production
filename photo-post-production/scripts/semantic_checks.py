"""Conservative before/after semantic and artifact checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from image_metrics import analyze_preview
from visual_analysis import run_local_vision


def _count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def compare_semantics(before_path: str, after_path: str, operation_type: str | None = None) -> dict[str, Any]:
    before = Path(before_path).expanduser().resolve()
    after = Path(after_path).expanduser().resolve()
    if not before.is_file() or not after.is_file():
        return {"status": "unavailable", "critical": False, "warnings": ["before_or_after_missing"]}
    before_metrics = analyze_preview(str(before))
    after_metrics = analyze_preview(str(after))
    before_vision = run_local_vision(str(before))
    after_vision = run_local_vision(str(after))
    warnings: list[str] = []
    if before_metrics.get("dimensions") != after_metrics.get("dimensions") and operation_type not in {"large-crop", "generative-expand"}:
        warnings.append("unexpected_dimensions_change")
    if abs(float(after_metrics.get("highlight_clipping", 0)) - float(before_metrics.get("highlight_clipping", 0))) > 0.12:
        warnings.append("highlight_clipping_changed_substantially")
    if float(after_metrics.get("shadow_crush", 0)) > float(before_metrics.get("shadow_crush", 0)) + 0.12:
        warnings.append("shadow_crush_increased")
    if before_vision.get("available") and after_vision.get("available"):
        if _count(before_vision, "faces") != _count(after_vision, "faces"):
            warnings.append("face_detection_count_changed")
        if _count(before_vision, "animals") != _count(after_vision, "animals"):
            warnings.append("animal_detection_count_changed")
        if _count(before_vision, "text") != _count(after_vision, "text") and operation_type not in {"remove-element", "generative-fill", "style-reconstruct"}:
            warnings.append("text_detection_count_changed")
    critical = any(item in {"face_detection_count_changed", "animal_detection_count_changed"} for item in warnings)
    return {
        "status": "checked" if before_vision.get("available") else "technical-only",
        "critical": critical,
        "warnings": warnings,
        "before": {"path": str(before), "metrics": before_metrics, "vision": before_vision},
        "after": {"path": str(after), "metrics": after_metrics, "vision": after_vision},
    }
