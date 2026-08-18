"""Analyze authorized reference images into local, high-level style recipes.

The module stores only provenance and derived numeric attributes. It does not
download or copy internet artwork; a URL is metadata, and the user must supply
an authorized local image when pixel analysis is desired.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from image_metrics import analyze_preview
from style_memory import record_reference, save_style_recipe


_CATEGORIES = {
    "landscape-nature", "urban-landscape", "architecture-urban-space",
    "street-documentary", "portrait-environmental", "animal-wildlife", "other-unsupported",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def analyze_reference(path: str, category: str | None = None, labels: list[str] | None = None) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"reference image does not exist: {source}")
    metrics = analyze_preview(str(source))
    if category is not None and category not in _CATEGORIES:
        raise ValueError(f"unsupported reference category: {category}")
    mean_luma = _number(metrics.get("mean_luma"), 0.5)
    contrast = max(0.0, min(1.0, _number(metrics.get("noise_proxy")) + (1.0 - _number(metrics.get("blur_proxy")))))
    attributes = {
        "dimensions": metrics.get("dimensions"),
        "mean_luma": round(mean_luma, 6),
        "mean_chroma": round(_number(metrics.get("mean_chroma")), 6),
        "highlight_clipping": round(_number(metrics.get("highlight_clipping")), 6),
        "shadow_crush": round(_number(metrics.get("shadow_crush")), 6),
        "contrast_proxy": round(contrast, 6),
        "labels": sorted({str(item).strip() for item in (labels or []) if str(item).strip()}),
    }
    return {"path": str(source), "category": category, "attributes": attributes}


def register_reference(
    db_path: str,
    path: str,
    title: str,
    source_url: str,
    license_name: str,
    profile_name: str = "default",
    category: str | None = None,
    creator: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    analysis = analyze_reference(path, category=category, labels=labels)
    reference_id = record_reference(db_path, profile_name, title, source_url, license_name, category, analysis["attributes"], creator)
    return {"reference_id": reference_id, "profile_name": profile_name, "source_url": source_url, **analysis}


def derive_style_recipe(db_path: str, profile_name: str = "default", category: str = "other-unsupported") -> dict[str, Any]:
    """Aggregate reference attributes into bounded Lightroom/Photoshop guidance."""
    from style_memory import get_profile

    profile = get_profile(db_path, profile_name)
    refs = [item for item in profile.get("references", []) if item.get("category") in {category, None}]
    if not refs:
        return {"profile_name": profile_name, "category": category, "evidence_count": 0, "status": "no_reference_evidence", "recipe": {}}
    attrs = [item.get("attributes", {}) for item in refs]

    def average(field: str, default: float = 0.5) -> float:
        values = [_number(item.get(field), default) for item in attrs]
        return sum(values) / len(values)

    mean_luma = average("mean_luma")
    chroma = average("mean_chroma")
    feedback = profile.get("guidance", []) if isinstance(profile.get("guidance"), list) else []
    feedback_adjustments = _feedback_adjustments(feedback)
    recipe = {
        "source": "authorized-reference-and-repeated-user-feedback",
        "reference_ids": [item["reference_id"] for item in refs],
        "feedback_guidance": feedback,
        "lightroom": {
            "exposure_bias": round(max(-0.7, min(0.7, (mean_luma - 0.48) * 1.4)), 3),
            "contrast_bias": round(max(-20.0, min(20.0, (average("contrast_proxy") - 0.45) * 35.0)), 2),
            "saturation_bias": round(max(-12.0, min(12.0, (chroma - 0.28) * 30.0)), 2),
            "highlight_protection": round(max(0.0, min(45.0, average("highlight_clipping") * 180.0)), 2),
            "shadow_lift": round(max(0.0, min(35.0, average("shadow_crush") * 120.0)), 2),
        },
        "photoshop": {
            "preferred_locality": "subject-and-background-separation",
            "preserve_source_fidelity": True,
            "avoid_global_saturation_as_substitute_for_style": True,
        },
        "confidence": round(min(0.95, 0.45 + min(0.5, len(refs) * 0.08)), 3),
    }
    for name, value in feedback_adjustments.items():
        recipe["lightroom"][name] = round(recipe["lightroom"].get(name, 0.0) + value, 3)
    if feedback_adjustments:
        recipe["feedback_confidence"] = round(min(0.95, 0.45 + min(0.45, len(feedback) * 0.08)), 3)
    save_style_recipe(db_path, profile_name, category, recipe, len(refs))
    return {"profile_name": profile_name, "category": category, "evidence_count": len(refs), "status": "derived", "recipe": recipe}


def _feedback_adjustments(guidance: list[dict[str, Any]]) -> dict[str, float]:
    """Convert repeated natural-language corrections into bounded LR deltas."""

    adjustments = {"exposure_bias": 0.0, "highlight_protection": 0.0, "shadow_lift": 0.0, "contrast_bias": 0.0, "saturation_bias": 0.0}
    for item in guidance:
        text = str(item.get("text", ""))
        strength = max(0.25, min(1.0, float(item.get("strength", 0.5))))
        if any(token in text for token in ("太暗", "一眼黑", "提亮", "暗部不够", "看不清")):
            adjustments["exposure_bias"] += 0.10 * strength
            adjustments["shadow_lift"] += 8.0 * strength
        if any(token in text for token in ("太亮", "过曝", "高光", "刺眼")):
            adjustments["exposure_bias"] -= 0.06 * strength
            adjustments["highlight_protection"] += 8.0 * strength
        if any(token in text for token in ("太艳", "过饱和", "颜色太重", "克制")):
            adjustments["saturation_bias"] -= 5.0 * strength
        if any(token in text for token in ("没层次", "发灰", "平", "对比度")):
            adjustments["contrast_bias"] += 5.0 * strength
        if any(token in text for token in ("保留环境", "不要只看人物", "环境感")):
            adjustments["shadow_lift"] += 4.0 * strength
    return {
        key: round(max(-12.0 if key == "saturation_bias" else -0.7, min(35.0 if key == "shadow_lift" else (20.0 if key == "contrast_bias" else 0.7), value)), 3)
        for key, value in adjustments.items()
        if abs(value) > 0.001
    }
