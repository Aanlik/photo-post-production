"""Series anchor, ordering, and style-drift guidance for a batch."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _group_key(item: dict[str, Any]) -> str:
    return str(item.get("series_id") or item.get("capture_series_id") or item.get("asset_group_id") or "ungrouped")


def _fingerprint(item: dict[str, Any]) -> str | None:
    technical = item.get("technical_analysis") if isinstance(item.get("technical_analysis"), dict) else {}
    value = item.get("perceptual_hash") or technical.get("perceptual_hash")
    return str(value) if isinstance(value, str) and value.strip() else None


def _hash_similarity(left: str, right: str) -> float:
    try:
        differing = bin(int(left, 16) ^ int(right, 16)).count("1")
        bit_count = max(len(left), len(right)) * 4
    except (TypeError, ValueError):
        return 0.0
    return 1.0 - (differing / bit_count) if bit_count else 0.0


def _infer_visual_series(scores: list[dict[str, Any]], threshold: float = 0.72) -> dict[str, list[dict[str, Any]]]:
    """Infer same-scene groups without merging unrelated same-category photos."""

    candidates = [item for item in scores if _fingerprint(item) and not item.get("series_id") and not item.get("capture_series_id")]
    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left, first in enumerate(candidates):
        for right in range(left + 1, len(candidates)):
            second = candidates[right]
            if first.get("primary_category") != second.get("primary_category"):
                continue
            first_dir = str(first.get("source_path", "")).rsplit("/", 1)[0]
            second_dir = str(second.get("source_path", "")).rsplit("/", 1)[0]
            if first_dir != second_dir:
                continue
            if _hash_similarity(_fingerprint(first), _fingerprint(second)) >= threshold:
                union(left, right)

    inferred: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, item in enumerate(candidates):
        inferred[f"visual:{find(index)}"].append(item)
    return {key: value for key, value in inferred.items() if len(value) > 1}


def build_series_plan(scores: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scores:
        if isinstance(item, dict):
            groups[_group_key(item)].append(item)
    assigned_visual: set[str] = set()
    for inferred_key, items in _infer_visual_series(scores).items():
        group_key = f"inferred-{inferred_key}"
        groups[group_key] = items
        assigned_visual.update(str(item.get("photo_id")) for item in items)
    for group_key in list(groups):
        if group_key.startswith("inferred-"):
            continue
        if group_key.startswith("visual:"):
            del groups[group_key]
        elif assigned_visual:
            groups[group_key] = [item for item in groups[group_key] if str(item.get("photo_id")) not in assigned_visual]
            if not groups[group_key]:
                del groups[group_key]
    series: list[dict[str, Any]] = []
    for group_id, items in sorted(groups.items()):
        selected = [item for item in items if item.get("decision") == "selected"]
        pool = selected or items
        if not pool:
            continue
        anchor = max(pool, key=lambda item: (float(item.get("score_confidence", 0.0)), float(item.get("candidate_potential", 0.0))))
        anchor_luma = float(anchor.get("mean_luma", 0.5))
        anchor_category = str(anchor.get("primary_category", "other-unsupported"))
        members = []
        for index, item in enumerate(sorted(pool, key=lambda entry: float(entry.get("candidate_potential", 0.0)), reverse=True), start=1):
            luma = float(item.get("mean_luma", anchor_luma))
            member = {
                "photo_id": item.get("photo_id"),
                "review_key": item.get("review_key"),
                "sequence_order": index,
                "narrative_role": ["opening", "environment", "transition", "peak", "detail", "closing"][min(index - 1, 5)],
                "style_drift": {
                    "mean_luma_delta": round(luma - anchor_luma, 4),
                    "category_matches_anchor": str(item.get("primary_category", "other-unsupported")) == anchor_category,
                },
                "allow_per_photo_exposure_correction": True,
            }
            if _fingerprint(item) and _fingerprint(anchor):
                member["visual_similarity_to_anchor"] = round(_hash_similarity(_fingerprint(item), _fingerprint(anchor)), 4)
            members.append(member)
        series.append({
            "series_id": group_id,
            "anchor_photo_id": anchor.get("photo_id"),
            "anchor_category": anchor_category,
            "consistency_controls": ["white-balance logic", "contrast/black-point logic", "crop language", "visual density"],
            "members": members,
            "deduplication": "asset-group and similarity selection remain separate from narrative order",
            "inference": "visual-fingerprint-and-category" if group_id.startswith("inferred-") else "explicit-or-asset-group",
        })
    return {"status": "planned", "series_count": len(series), "series": series}
