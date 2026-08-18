"""Standard-library validation for the photo post-production contracts."""

from __future__ import annotations

import math
from typing import Any


SCORE_FIELDS = (
    "style_fit", "technical", "composition", "light_color", "moment_story",
    "coherence", "photographic_value", "editability", "expected_gain",
    "keep_value", "candidate_potential",
)
PRIMARY_CATEGORIES = {
    "landscape-nature", "urban-landscape", "architecture-urban-space",
    "street-documentary", "portrait-environmental", "animal-wildlife",
    "plant-macro", "other-unsupported",
}
INTENTS = {
    "documentary-truthful", "natural-enhancement", "editorial-expression",
    "competition-standard", "commercial/creative",
}
TRANSFORMATION_TYPES = {
    "remove-element", "add-element", "generative-fill", "generative-expand",
    "replace-sky-or-background", "reshape-geometry", "large-crop",
    "relight-subject", "style-reconstruct",
}
VARIANT_NAMES = {"natural", "editorial", "competition-standard"}
REVIEW_CONFIDENCE_THRESHOLD = 0.75
AUTO_SELECTION_CANDIDATE_THRESHOLD = 75
CANDIDATE_POTENTIAL_TOLERANCE = 0.01
REQUIRED_BUDGET_FIELDS = {
    "max_global_adjustments", "max_local_adjustments", "max_transformative_operations",
    "max_exposure_delta", "max_crop_fraction", "max_temperature_delta",
    "max_sharpening", "max_geometry_delta", "max_candidates",
}


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _required(mapping: Any, fields: tuple[str, ...], prefix: str = "") -> list[str]:
    if not isinstance(mapping, dict):
        return [f"{prefix or 'record'} must be an object"]
    return [f"{prefix}{field} is required" for field in fields if field not in mapping]


def _range(errors: list[str], name: str, value: Any, minimum: float, maximum: float) -> None:
    if not _is_number(value) or not minimum <= value <= maximum:
        errors.append(f"{name} must be a number from {minimum:g} to {maximum:g}")


def _unexpected(errors: list[str], mapping: dict, allowed: set[str], prefix: str = "") -> None:
    for field in mapping:
        if field not in allowed:
            errors.append(f"{prefix}{field} is not allowed")


def validate_score_record(record: dict) -> list[str]:
    """Return contract violations for a machine-readable scoring record."""
    required = (
        "primary_category", "secondary_tags", "classification_confidence",
        "score_version", "evidence", "score_confidence", "technical_gates",
        *SCORE_FIELDS, "final_score", "decision", "strengths", "risks",
        "recommended_treatment",
    )
    errors = _required(record, required)
    if errors:
        return errors
    optional = {
        "category_weights_version", "model_signals", "preference_fit",
        "preference_model_version", "comparative_reasons", "burst_rank",
    }
    _unexpected(errors, record, set(required) | optional)

    if not isinstance(record["primary_category"], str) or record["primary_category"] not in PRIMARY_CATEGORIES:
        errors.append("primary_category is not a supported category")
    if not isinstance(record["secondary_tags"], list) or not all(isinstance(tag, str) for tag in record["secondary_tags"]):
        errors.append("secondary_tags must be a list of strings")
    for field in ("classification_confidence", "score_confidence"):
        _range(errors, field, record[field], 0, 1)
    if not isinstance(record["score_version"], str) or not record["score_version"].strip():
        errors.append("score_version must be a non-empty string")
    if "category_weights_version" in record and (
        not isinstance(record["category_weights_version"], str)
        or not record["category_weights_version"].strip()
    ):
        errors.append("category_weights_version must be a non-empty string")
    if "model_signals" in record and not isinstance(record["model_signals"], dict):
        errors.append("model_signals must be an object")
    if "preference_fit" in record:
        _range(errors, "preference_fit", record["preference_fit"], 0, 100)
    if "preference_model_version" in record and record["preference_model_version"] is not None and not isinstance(record["preference_model_version"], int):
        errors.append("preference_model_version must be an integer or null")
    if "comparative_reasons" in record and (
        not isinstance(record["comparative_reasons"], list)
        or not all(isinstance(item, str) for item in record["comparative_reasons"])
    ):
        errors.append("comparative_reasons must be a list of strings")
    if "burst_rank" in record and record["burst_rank"] is not None and (
        not isinstance(record["burst_rank"], int) or record["burst_rank"] < 1
    ):
        errors.append("burst_rank must be a positive integer or null")
    if not isinstance(record["evidence"], list) or not record["evidence"] or not all(isinstance(item, str) and item.strip() for item in record["evidence"]):
        errors.append("evidence must be a non-empty list of strings")
    for field in SCORE_FIELDS:
        _range(errors, field, record[field], 0, 100)
    if all(_is_number(record[field]) for field in ("keep_value", "editability", "expected_gain", "candidate_potential")):
        expected_candidate_potential = (
            record["keep_value"] * 0.65
            + record["editability"] * 0.20
            + record["expected_gain"] * 0.15
        )
        if abs(record["candidate_potential"] - expected_candidate_potential) > CANDIDATE_POTENTIAL_TOLERANCE:
            errors.append(
                "candidate_potential must match the weighted formula within "
                f"{CANDIDATE_POTENTIAL_TOLERANCE:g}"
            )
    if record["final_score"] is not None:
        _range(errors, "final_score", record["final_score"], 0, 100)
    gates = record["technical_gates"]
    if not isinstance(gates, dict) or not gates:
        errors.append("technical_gates must be a non-empty object")
    elif any(not isinstance(status, str) or status not in {"pass", "warn", "fail"} for status in gates.values()):
        errors.append("technical_gates must contain only pass, warn, or fail")
    if record["decision"] not in {"selected", "review", "rejected"}:
        errors.append("decision must be selected, review, or rejected")
    for field in ("strengths", "risks", "recommended_treatment"):
        if not isinstance(record[field], list) or not all(isinstance(item, str) for item in record[field]):
            errors.append(f"{field} must be a list of strings")
    if _is_number(record["score_confidence"]) and record["score_confidence"] < REVIEW_CONFIDENCE_THRESHOLD and record["decision"] != "review":
        errors.append("low score_confidence is review-only; decision must be review")
    if record["decision"] == "selected" and _is_number(record["candidate_potential"]) and record["candidate_potential"] < AUTO_SELECTION_CANDIDATE_THRESHOLD:
        errors.append("selected records must meet the default candidate_potential threshold of 75")
    if isinstance(gates, dict) and "fail" in gates.values() and record["decision"] == "selected":
        errors.append("a failed technical gate cannot be auto-accepted")
    return errors


def _validate_director_brief(brief: Any) -> list[str]:
    fields = ("project_goal", "subject_priority", "target_use", "mood", "photographer_intent", "creative_intensity", "source_fidelity", "transformation_disclosure", "allowed_operations")
    errors = _required(brief, fields, "director_brief.")
    if errors:
        return errors
    _unexpected(errors, brief, set(fields), "director_brief.")
    for field in ("project_goal", "target_use", "mood", "photographer_intent", "transformation_disclosure"):
        if not isinstance(brief[field], str) or not brief[field].strip():
            errors.append(f"director_brief.{field} must be a non-empty string")
    for field in ("creative_intensity", "source_fidelity"):
        _range(errors, f"director_brief.{field}", brief[field], 0, 100)
    for field in ("subject_priority", "allowed_operations"):
        if not isinstance(brief[field], list) or not brief[field] or not all(isinstance(item, str) and item.strip() for item in brief[field]):
            errors.append(f"director_brief.{field} must be a non-empty list of strings")
    return errors


def validate_edit_plan(plan: dict) -> list[str]:
    """Return contract violations for a director brief and reversible edit plan."""
    required = ("director_brief", "intent", "edit_authority", "content_policy", "adjustment_budget", "regions", "operations", "operation_records", "variant_name")
    errors = _required(plan, required)
    if errors:
        return errors
    _unexpected(errors, plan, set(required) | {"global_adjustments", "transformation_level"})
    errors.extend(_validate_director_brief(plan["director_brief"]))
    if not isinstance(plan["intent"], str) or plan["intent"] not in INTENTS:
        errors.append("intent is not supported")
    if plan["edit_authority"] != "full":
        errors.append("edit_authority must be full")
    if plan["edit_authority"] == "full" and plan["content_policy"] != "user-authorized-transformative":
        errors.append("full edit_authority requires content_policy user-authorized-transformative")
    budget = plan["adjustment_budget"]
    if not isinstance(budget, dict) or not budget or any(not _is_number(value) or value < 0 for value in budget.values()):
        errors.append("adjustment_budget must be a non-empty object of non-negative numbers")
    elif not REQUIRED_BUDGET_FIELDS.issubset(budget):
        errors.append("adjustment_budget is missing required magnitude and candidate limits")
    elif budget["max_crop_fraction"] > 1:
        errors.append("adjustment_budget.max_crop_fraction must not exceed 1")
    if not isinstance(plan["variant_name"], str) or plan["variant_name"] not in VARIANT_NAMES:
        errors.append("variant_name is not supported")

    global_adjustments = plan.get("global_adjustments", [])
    if not isinstance(global_adjustments, list):
        errors.append("global_adjustments must be a list")
    else:
        for index, adjustment in enumerate(global_adjustments):
            if not isinstance(adjustment, dict) or not adjustment:
                errors.append(f"global_adjustments[{index}] must be a non-empty object")
            elif any(not isinstance(name, str) or not name.strip() or not _is_number(value) for name, value in adjustment.items()):
                errors.append(f"global_adjustments[{index}] values must be finite numbers")

    regions = plan["regions"]
    region_ids: set[str] = set()
    if not isinstance(regions, list):
        errors.append("regions must be a list")
    else:
        for index, region in enumerate(regions):
            prefix = f"regions[{index}]."
            errors.extend(_required(region, ("id", "label", "mask_type", "purpose", "adjustments", "confidence", "forbidden_changes"), prefix))
            if not isinstance(region, dict):
                continue
            _unexpected(errors, region, {"id", "label", "mask_type", "purpose", "adjustments", "confidence", "forbidden_changes"}, prefix)
            if isinstance(region.get("id"), str) and region["id"].strip():
                if region["id"] in region_ids:
                    errors.append(f"{prefix}id must be unique")
                region_ids.add(region["id"])
            else:
                errors.append(f"{prefix}id must be a non-empty string")
            if not isinstance(region.get("mask_type"), str) or region["mask_type"] not in {"semantic", "geometry"}:
                errors.append(f"{prefix}mask_type must be semantic or geometry")
            _range(errors, f"{prefix}confidence", region.get("confidence"), 0, 1)
            for field in ("label", "purpose"):
                if not isinstance(region.get(field), str) or not region[field].strip():
                    errors.append(f"{prefix}{field} must be a non-empty string")
            if not isinstance(region.get("adjustments"), dict) or any(not _is_number(value) for value in region.get("adjustments", {}).values()):
                errors.append(f"{prefix}adjustments must be an object of numbers")
            if not isinstance(region.get("forbidden_changes"), list) or not all(isinstance(value, str) for value in region["forbidden_changes"]):
                errors.append(f"{prefix}forbidden_changes must be a list of strings")

    operations = plan["operations"]
    operation_ids: set[str] = set()
    transformative_ids: set[str] = set()
    if not isinstance(operations, list):
        errors.append("operations must be a list")
        operations = []
    for index, operation in enumerate(operations):
        prefix = f"operations[{index}]."
        fields = ("operation_id", "type", "depends_on", "backend", "reason", "affected_region", "parameters", "risk", "checkpoint", "generative", "input_layer", "output_layer")
        errors.extend(_required(operation, fields, prefix))
        if not isinstance(operation, dict):
            continue
        _unexpected(errors, operation, set(fields), prefix)
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            errors.append(f"{prefix}operation_id must be a non-empty string")
        elif operation_id in operation_ids:
            errors.append(f"{prefix}operation_id must be unique")
        else:
            operation_ids.add(operation_id)
        if not isinstance(operation.get("type"), str) or operation["type"] not in TRANSFORMATION_TYPES:
            errors.append(f"{prefix}type is not in the transformation vocabulary")
        if not isinstance(operation.get("depends_on"), list) or not all(isinstance(dep, str) and dep.strip() for dep in operation.get("depends_on", [])):
            errors.append(f"{prefix}depends_on must be a list of operation IDs")
        if not isinstance(operation.get("risk"), str) or operation["risk"] not in {"low", "medium", "high"}:
            errors.append(f"{prefix}risk must be low, medium, or high")
        for field in ("backend", "reason", "affected_region", "checkpoint", "input_layer", "output_layer"):
            if not isinstance(operation.get(field), str) or not operation[field].strip():
                errors.append(f"{prefix}{field} must be a non-empty string")
        if not isinstance(operation.get("parameters"), dict):
            errors.append(f"{prefix}parameters must be an object")
        if not isinstance(operation.get("generative"), bool):
            errors.append(f"{prefix}generative must be boolean")
        if isinstance(operation_id, str) and operation_id.strip() and (operation.get("generative") is True or (isinstance(operation.get("type"), str) and operation["type"] in TRANSFORMATION_TYPES)):
            transformative_ids.add(operation_id)
    dependency_graph: dict[str, list[str]] = {}
    for index, operation in enumerate(operations):
        if isinstance(operation, dict) and isinstance(operation.get("depends_on"), list):
            operation_id = operation.get("operation_id")
            if isinstance(operation_id, str) and operation_id in operation_ids:
                dependency_graph[operation_id] = operation["depends_on"]
            for dependency in operation["depends_on"]:
                if dependency not in operation_ids:
                    errors.append(f"operations[{index}].depends_on has broken dependency {dependency}")
                elif dependency == operation_id:
                    errors.append(f"operations[{index}].depends_on cannot contain a self-dependency")

    visiting: set[str] = set()
    visited: set[str] = set()

    def has_cycle(operation_id: str) -> bool:
        if operation_id in visiting:
            return True
        if operation_id in visited:
            return False
        visiting.add(operation_id)
        for dependency in dependency_graph.get(operation_id, []):
            if dependency in dependency_graph and has_cycle(dependency):
                return True
        visiting.remove(operation_id)
        visited.add(operation_id)
        return False

    if any(has_cycle(operation_id) for operation_id in dependency_graph):
        errors.append("operations contains a dependency cycle")

    records = plan["operation_records"]
    record_ids: set[str] = set()
    if not isinstance(records, list):
        errors.append("operation_records must be a list")
        records = []
    for index, record in enumerate(records):
        prefix = f"operation_records[{index}]."
        fields = (
            "operation_id", "before_path", "before_sha256", "after_path", "after_sha256",
            "model", "model_version", "software", "prompt", "mask_reference", "mask_sha256",
        )
        errors.extend(_required(record, fields, prefix))
        if not isinstance(record, dict):
            continue
        _unexpected(errors, record, set(fields), prefix)
        operation_id = record.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            errors.append(f"{prefix}operation_id must be a non-empty string")
        elif operation_id in record_ids:
            errors.append(f"{prefix}duplicate operation record for {operation_id}")
        else:
            record_ids.add(operation_id)
        for field in fields[1:]:
            if not isinstance(record.get(field), str) or not record[field].strip():
                errors.append(f"{prefix}{field} must be a non-empty string")
    for operation_id in sorted(transformative_ids - record_ids):
        errors.append(f"operation {operation_id} requires an operation record with provenance")
    for operation_id in sorted(record_ids - operation_ids):
        errors.append(f"orphaned operation record for {operation_id}")
    return errors
