"""Create bounded, reviewable variant plans from a score record.

This module only plans work. Lightroom/Photoshop execution remains capability
checked by the host Skill and each plan is isolated by variant name.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


VARIANTS = ("natural", "editorial", "competition-standard")

PLAN_OPERATION_FIELDS = (
    "operation_id", "type", "depends_on", "backend", "reason",
    "affected_region", "parameters", "risk", "checkpoint", "generative",
    "input_layer", "output_layer", "adapter_operation", "required", "status",
    "applicable", "success_criteria", "execution_route",
)


def _numeric_settings(settings: Any) -> dict[str, float]:
    if not isinstance(settings, dict):
        return {}
    return {
        str(key): float(value)
        for key, value in settings.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _planned_operations(graph: Any) -> list[dict[str, Any]]:
    if not isinstance(graph, dict) or not isinstance(graph.get("operations"), list):
        return []
    return [
        {
            field: deepcopy(operation[field])
            for field in PLAN_OPERATION_FIELDS
            if field in operation
        }
        for operation in graph["operations"]
        if isinstance(operation, dict)
    ]


def materialize_executable_plan(
    edit_plan: dict[str, Any],
    adapter_plan: dict[str, Any] | None,
    operation_graph: dict[str, Any] | None,
) -> dict[str, Any]:
    """Synchronize the readable plan with the exact executable graph.

    ``operations`` remains the strict, provenance-bearing completed-operation
    field. Planned work is exposed separately so a pre-execution plan never
    invents before/after evidence.
    """

    result = deepcopy(edit_plan if isinstance(edit_plan, dict) else {})
    adapters = adapter_plan if isinstance(adapter_plan, dict) else {}
    graph = operation_graph if isinstance(operation_graph, dict) else {}
    lightroom = adapters.get("lightroom") if isinstance(adapters.get("lightroom"), dict) else {}
    photoshop = adapters.get("photoshop") if isinstance(adapters.get("photoshop"), dict) else {}
    lr_settings = lightroom.get("settings") if isinstance(lightroom.get("settings"), dict) else {}
    color_strategy = lightroom.get("color_strategy") if isinstance(lightroom.get("color_strategy"), dict) else {}
    planned = _planned_operations(graph)

    numeric_global = _numeric_settings(lr_settings)
    if numeric_global:
        result["global_adjustments"] = [numeric_global]

    color_layers = [
        {
            "operation_id": operation.get("operation_id"),
            "backend": operation.get("backend"),
            "region": operation.get("affected_region"),
            "parameters": deepcopy(operation.get("parameters", {})),
            "required": operation.get("required", True),
            "status": operation.get("status", "planned"),
        }
        for operation in planned
        if operation.get("adapter_operation") == "selective_color"
    ]
    detail_operations = [
        deepcopy(operation)
        for operation in planned
        if operation.get("adapter_operation") in {"sharpening", "noise_reduction"}
    ]
    hsl = lr_settings.get("hsl") if isinstance(lr_settings.get("hsl"), dict) else {}
    white_balance = {
        key: deepcopy(lr_settings[key])
        for key in ("white_balance", "temperature", "tint")
        if key in lr_settings
    }
    unsupported = color_strategy.get("unsupported_color_features")
    unsupported = list(unsupported) if isinstance(unsupported, list) else []

    result["planned_operations"] = planned
    result["optional_operations"] = deepcopy(graph.get("optional_operations", []))
    result["color_plan"] = {
        "strategy": deepcopy(color_strategy),
        "lightroom": {
            "vibrance": lr_settings.get("vibrance"),
            "saturation": lr_settings.get("saturation"),
            "white_balance": white_balance,
            "hsl": deepcopy(hsl),
        },
        "photoshop_layers": color_layers,
        "unsupported_features": unsupported,
        "mechanism_disclosure": (
            "Photoshop selective_color nodes use masked hue/saturation layers; "
            "channel-specific grading is not claimed unless a verified descriptor is present."
        ),
    }
    result["detail_plan"] = {
        "lightroom": {
            key: deepcopy(lr_settings[key])
            for key in (
                "sharpening", "luminance_smoothing",
                "luminance_noise_reduction_detail",
                "luminance_noise_reduction_contrast", "color_noise_reduction",
                "color_noise_reduction_detail", "color_noise_reduction_smoothness",
            )
            if key in lr_settings
        },
        "photoshop_operations": detail_operations,
        "execution_note": "Adapter capability and restore checks may downgrade individual settings at runtime.",
    }
    result["plan_consistency"] = {
        "status": "synchronized",
        "authoritative_source": "operation_graph",
        "global_adjustments_match_lightroom": bool(numeric_global) and result.get("global_adjustments") == [numeric_global],
        "hsl_recorded": bool(hsl),
        "planned_operation_count": len(planned),
        "required_operation_count": sum(operation.get("required") is True for operation in planned),
        "optional_operation_count": len(result["optional_operations"]),
        "photoshop_document_policy": deepcopy(photoshop.get("document_policy", {})),
    }
    return result


def attach_execution_audit(
    edit_plan: dict[str, Any],
    execution: dict[str, Any] | None,
    lightroom_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Overlay verified runtime statuses without changing planned parameters."""

    result = deepcopy(edit_plan if isinstance(edit_plan, dict) else {})
    runtime = execution if isinstance(execution, dict) else {}
    lr_runtime = lightroom_runtime if isinstance(lightroom_runtime, dict) else {}
    skipped_settings = deepcopy(lr_runtime.get("skipped_settings", []))
    operation_results = runtime.get("operation_results") if isinstance(runtime.get("operation_results"), list) else []
    status_by_id = {
        str(item.get("operation_id")): str(item.get("status"))
        for item in operation_results
        if isinstance(item, dict) and item.get("operation_id") and item.get("status")
    }
    lightroom_operation_id = lr_runtime.get("operation_id")
    if skipped_settings and lightroom_operation_id in status_by_id and status_by_id[lightroom_operation_id] == "completed":
        status_by_id[str(lightroom_operation_id)] = "completed-with-skips"

    def apply_status(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            status = status_by_id.get(str(item.get("operation_id")))
            if status:
                item["status"] = status

    graph = result.get("operation_graph") if isinstance(result.get("operation_graph"), dict) else {}
    apply_status(graph.get("operations"))
    apply_status(result.get("planned_operations"))
    color_plan = result.get("color_plan") if isinstance(result.get("color_plan"), dict) else {}
    apply_status(color_plan.get("photoshop_layers"))
    detail_plan = result.get("detail_plan") if isinstance(result.get("detail_plan"), dict) else {}
    apply_status(detail_plan.get("photoshop_operations"))

    completed = sum(status == "completed" for status in status_by_id.values())
    completed_with_skips = sum(status == "completed-with-skips" for status in status_by_id.values())
    result["execution_audit"] = {
        "status": runtime.get("status", "not-executed"),
        "operation_counts": {
            "total": len(operation_results),
            "completed": completed,
            "completed_with_skips": completed_with_skips,
            "skipped": sum(status == "skipped" for status in status_by_id.values()),
            "blocked": sum(status == "blocked" for status in status_by_id.values()),
        },
        "downgrades": deepcopy(runtime.get("downgrades", [])),
        "blockers": deepcopy(runtime.get("blockers", [])),
        "lightroom": {
            "restore_status": lr_runtime.get("restore_status"),
            "requested_settings": deepcopy(lr_runtime.get("requested_settings", {})),
            "skipped_settings": skipped_settings,
        },
    }
    consistency = result.get("plan_consistency") if isinstance(result.get("plan_consistency"), dict) else {}
    consistency["execution_status"] = runtime.get("status", "not-executed")
    consistency["executed_operation_count"] = completed + completed_with_skips
    result["plan_consistency"] = consistency
    return result


def _brief(intent: str, category: str, creative_intensity: float, source_fidelity: float) -> dict[str, Any]:
    return {
        "project_goal": "把已筛选照片处理为可分享的高完成度摄影成片",
        "subject_priority": [category],
        "target_use": "shareable-photo",
        "mood": "有层次、可信、有摄影感",
        "photographer_intent": "保留主体和现场逻辑，允许经用户授权的精细后期",
        "creative_intensity": creative_intensity,
        "source_fidelity": source_fidelity,
        "transformation_disclosure": "明确记录局部修复、内容变换和生成式操作",
        "allowed_operations": [
            "remove-element", "add-element", "generative-fill", "generative-expand",
            "replace-sky-or-background", "reshape-geometry", "large-crop",
            "relight-subject", "style-reconstruct",
        ],
    }


def build_edit_plan(score: dict[str, Any], intent: str = "natural-enhancement", variant_name: str = "natural", creative_intensity: float = 35, source_fidelity: float = 80) -> dict[str, Any]:
    """Build a valid analysis-stage plan with no unsupported pixel promises."""
    if variant_name not in VARIANTS:
        raise ValueError(f"unsupported variant: {variant_name}")
    category = str(score.get("primary_category", "other-unsupported"))
    if variant_name == "natural":
        intensity, fidelity = min(55.0, creative_intensity), max(65.0, source_fidelity)
    elif variant_name == "editorial":
        intensity, fidelity = max(creative_intensity, 55.0), min(85.0, source_fidelity)
    else:
        intensity, fidelity = max(creative_intensity, 65.0), min(90.0, source_fidelity)
    treatments = score.get("recommended_treatment") if isinstance(score.get("recommended_treatment"), list) else []
    region_label = "主体区域" if category != "other-unsupported" else "待确认主体"
    regions = [{
        "id": "subject-review",
        "label": region_label,
        "mask_type": "semantic",
        "purpose": treatments[0] if treatments else "先确认主体后再进行局部精修",
        # Concrete local adjustments come from the Photoshop tool plan, after
        # the base layer and edit group exist.  This avoids applying a region
        # adjustment before Photoshop's editable document structure.
        "adjustments": {},
        "confidence": min(0.85, max(0.35, float(score.get("classification_confidence", 0.5)))),
        "forbidden_changes": ["不得在未确认前改变主体身份、建筑几何、文字和关键反射"],
    }]
    global_adjustments = [{"exposure": 0.12, "highlights": -18.0, "shadows": 12.0, "contrast": 6.0, "vibrance": 8.0, "saturation": -1.0}]
    if variant_name == "editorial":
        global_adjustments = [{"exposure": 0.22, "highlights": -26.0, "shadows": 18.0, "whites": 6.0, "blacks": -8.0, "contrast": 10.0, "clarity": 4.0, "vibrance": 14.0, "saturation": -2.0}]
    elif variant_name == "competition-standard":
        global_adjustments = [{"exposure": 0.28, "highlights": -32.0, "shadows": 22.0, "whites": 9.0, "blacks": -12.0, "contrast": 12.0, "texture": 5.0, "clarity": 6.0, "dehaze": 3.0, "vibrance": 18.0, "saturation": -2.0}]
    return {
        "director_brief": _brief(intent, category, intensity, fidelity),
        "intent": intent,
        "edit_authority": "full",
        "content_policy": "user-authorized-transformative",
        "adjustment_budget": {
            "max_global_adjustments": 12,
            "max_local_adjustments": 8,
            "max_transformative_operations": 8 if variant_name != "natural" else 3,
            "max_exposure_delta": 1.5 if variant_name == "natural" else 2.0,
            "max_crop_fraction": 0.30 if variant_name == "natural" else 0.60,
            "max_temperature_delta": 2000,
            "max_sharpening": 100,
            "max_geometry_delta": 12 if variant_name == "natural" else 22,
            "max_candidates": 1,
        },
        "global_adjustments": deepcopy(global_adjustments),
        "regions": regions,
        "operations": [],
        "operation_records": [],
        "variant_name": variant_name,
        "transformation_level": "source-faithful" if variant_name == "natural" else "enhanced",
    }


def build_variant_specs(score: dict[str, Any], intent: str = "natural-enhancement", max_candidates: int = 1) -> list[dict[str, Any]]:
    """Return one executable final treatment per source photo.

    Candidate exploration used to create a fresh TIFF and PSD per look.  That
    contradicts the one-photo/one-PSD delivery contract, so the runtime now
    selects one direction before Adobe work begins.  Future look comparisons
    must be represented as layer groups inside that same document.
    """
    del max_candidates
    variant_name = {
        "competition-standard": "competition-standard",
        "editorial-expression": "editorial",
    }.get(intent, "natural")
    return [build_edit_plan(score, intent=intent, variant_name=variant_name)]
