"""Project-scoped batch calibration without silently changing long-term style."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Any


def select_calibration_representatives(scores: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Choose diverse high-confidence representatives, at most one per category first."""

    limit = max(0, min(5, int(limit)))
    ordered = sorted(
        (item for item in scores if isinstance(item, dict)),
        key=lambda item: (
            float(item.get("score_confidence", 0.0)),
            float(item.get("candidate_potential", 0.0)),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    used_categories: set[str] = set()
    for item in ordered:
        category = str(item.get("primary_category", "other-unsupported"))
        if category in used_categories:
            continue
        selected.append(item)
        used_categories.add(category)
        if len(selected) >= limit:
            return selected
    for item in ordered:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def build_project_calibration(
    scores: list[dict[str, Any]],
    intent: str,
    mode: str,
    sample_limit: int = 5,
    skipped_reason: str | None = None,
) -> dict[str, Any]:
    """Create a project-only calibration record and explicit default assumptions."""

    representatives = select_calibration_representatives(scores, sample_limit)
    if skipped_reason:
        status = "skipped"
    elif mode == "auto":
        status = "skipped-auto-default"
    else:
        status = "ready-for-review"
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in representatives:
        by_category[str(item.get("primary_category", "other-unsupported"))].append(item)
    category_summary = {}
    for category, items in by_category.items():
        category_summary[category] = {
            "sample_count": len(items),
            "mean_luma": round(fmean(float(item.get("mean_luma", 0.5)) for item in items), 4),
            "mean_candidate_potential": round(fmean(float(item.get("candidate_potential", 0.0)) for item in items), 2),
            "default_treatment": "自然增强优先，局部修改需通过语义置信度和质量门",
        }
    return {
        "scope": "current-project-only",
        "status": status,
        "intent": intent,
        "sample_limit": max(0, min(5, int(sample_limit))),
        "representatives": [
            {
                "photo_id": item.get("photo_id"),
                "review_key": item.get("review_key"),
                "category": item.get("primary_category"),
                "score_confidence": item.get("score_confidence"),
                "candidate_potential": item.get("candidate_potential"),
            }
            for item in representatives
        ],
        "category_summary": category_summary,
        "assumptions": [
            "未校准时使用自然增强默认方向",
            "本次校准不会自动写入长期风格记忆",
            "生成式或大幅变换仍需经过操作图和质量门",
        ],
        "reason": skipped_reason or ("auto mode uses bounded default assumptions" if mode == "auto" else "review mode can confirm or skip calibration"),
    }
