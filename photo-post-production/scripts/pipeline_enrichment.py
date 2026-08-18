"""Attach comparison, preference and problem-driven planning evidence."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from comparative_ranker import rank_group
from edit_planner import build_problem_driven_plan
from preference_model import apply_preference_model, features_from_score
from action_descriptor_registry import find_compatible_descriptor, init_registry
from executor_router import route_operation


def _comparison_group(record: dict[str, Any]) -> str | None:
    for field in ("burst_group_id", "duplicate_cluster_id", "near_duplicate_cluster_id", "series_group_id"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return f"{field}:{value.strip()}"
    return None


def enrich_scores(scores: list[dict[str, Any]], preference_db: str, profile_name: str = "default") -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scores:
        group = _comparison_group(item)
        if group:
            groups[group].append(item)
    ranked_by_id: dict[str, dict[str, Any]] = {}
    for records in groups.values():
        if len(records) < 2:
            continue
        category = str(records[0].get("primary_category") or "other-unsupported")
        for ranked in rank_group(records, category):
            ranked_by_id[str(ranked.get("photo_id"))] = ranked

    for item in scores:
        ranked = ranked_by_id.get(str(item.get("photo_id")))
        if ranked:
            item["burst_rank"] = ranked.get("burst_rank")
            item["comparative_score"] = ranked.get("comparative_score")
            item["comparative_reasons"] = ranked.get("comparative_reasons", [])
        else:
            item.setdefault("comparative_reasons", [])
        preference = apply_preference_model(
            preference_db,
            profile_name,
            str(item.get("primary_category") or "other-unsupported"),
            features_from_score(item),
        )
        item["preference_fit"] = preference["preference_fit"]
        item["preference_model_version"] = preference["model_version"]
        item["preference_model_status"] = preference["status"]
        item["problem_driven_plan"] = build_problem_driven_plan(item, max_operations=3)
        score_record = item.get("score_record")
        if isinstance(score_record, dict):
            score_record["preference_fit"] = item["preference_fit"]
            score_record["preference_model_version"] = item["preference_model_version"]
            score_record["comparative_reasons"] = item["comparative_reasons"]
            score_record["burst_rank"] = item.get("burst_rank")
    return scores


_ROUTABLE_PROBLEM_TYPES = {
    "subject-relight": "subject-relight",
    "background-restraint": "background-restraint",
    "crop-and-straighten": "crop-and-straighten",
    "skin-tone-correct": "skin-tone-correct",
    "selective-sharpen": "selective-sharpen",
    "global-tone": "global-tone",
}


def route_problem_plan(
    score: dict[str, Any],
    capabilities: dict[str, Any],
    descriptor_db: str,
    photoshop_version: str = "",
    document_mode: str = "RGB",
    bit_depth: int = 16,
) -> dict[str, Any]:
    """Attach an honest executor route to each bounded problem-driven edit.

    A route is planning metadata, not proof that an Adobe operation ran.  The
    only route that may claim structured Photoshop execution is a registered
    and version-compatible descriptor; otherwise the result is UI-assisted or
    unsupported and must remain visible to the review/quality gates.
    """

    plan = score.get("problem_driven_plan") if isinstance(score.get("problem_driven_plan"), dict) else {}
    operations = plan.get("operations") if isinstance(plan.get("operations"), list) else []
    init_registry(descriptor_db)
    photoshop = capabilities.get("photoshop") if isinstance(capabilities.get("photoshop"), dict) else {}
    router_capabilities = {
        "stable_tools": photoshop.get("stable_tools", []),
        "ui_available": photoshop.get("ui_available") is True,
        "generative_available": capabilities.get("generative", {}).get("ready_for_execution") is True
        if isinstance(capabilities.get("generative"), dict) else False,
    }
    routed: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        operation_type = _ROUTABLE_PROBLEM_TYPES.get(str(operation.get("type")), str(operation.get("type")))
        descriptor = find_compatible_descriptor(
            descriptor_db,
            operation_type=operation_type,
            photoshop_version=photoshop_version or str(photoshop.get("version") or "0"),
            document_mode=document_mode,
            bit_depth=int(bit_depth),
        )
        route = route_operation({"type": operation_type, "parameters": operation.get("parameters", {})}, router_capabilities, descriptor)
        routed.append({**operation, "execution_type": operation_type, "execution_route": route})
    return {**plan, "operations": routed, "routing_version": "executor-routing-2026.08.17-v1"}
