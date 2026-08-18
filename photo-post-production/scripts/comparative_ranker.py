"""Rank related photos and explain pairwise advantages within a burst."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from category_weighting import category_weights


_LABELS = {
    "technical": "技术完整性",
    "composition": "构图",
    "light_color": "光线与色彩",
    "moment_story": "瞬间与叙事",
    "coherence": "画面完整性",
}


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rank_score(record: dict[str, Any], category: str) -> float:
    weights = category_weights(category)
    component = sum(_number(record.get(name)) * weight for name, weight in weights.items())
    return component * 0.8 + _number(record.get("candidate_potential")) * 0.2


def rank_group(records: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    if not records:
        return []
    ordered = sorted((deepcopy(item) for item in records), key=lambda item: (-_rank_score(item, category), str(item.get("photo_id", ""))))
    runner_up = ordered[1] if len(ordered) > 1 else None
    for index, item in enumerate(ordered, 1):
        item["burst_rank"] = index
        item["comparative_score"] = round(_rank_score(item, category), 4)
        reasons: list[str] = []
        comparison = runner_up if index == 1 else ordered[0]
        if comparison is not None:
            differences = sorted(
                ((name, _number(item.get(name)) - _number(comparison.get(name))) for name in _LABELS),
                key=lambda pair: abs(pair[1]),
                reverse=True,
            )
            for name, difference in differences[:3]:
                if abs(difference) < 1.0:
                    continue
                direction = "更强" if difference > 0 else "较弱"
                reasons.append(f"{_LABELS[name]}{direction} {abs(difference):.1f} 分")
        item["comparative_reasons"] = reasons or ["与组内相邻候选差异有限，需要结合缩略图复核"]
    return ordered
