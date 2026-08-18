"""Fail-closed quality and release gates for reversible candidate selection."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from run_manifest import verify_run_manifest, verify_trusted_context


MAX_ITERATIONS = 3
REVIEW_CONFIDENCE_THRESHOLD = 0.75
SATURATION_DELTA = 0.5
_TRANSFORMATIVE_TYPES = {
    "remove-element", "add-element", "generative-fill", "generative-expand",
    "replace-sky-or-background", "reshape-geometry", "large-crop",
    "relight-subject", "style-reconstruct",
}
_PROVENANCE_FIELDS = (
    "before_path", "before_sha256", "after_path", "after_sha256", "model",
    "model_version", "software", "prompt", "mask_reference", "mask_sha256",
)
_NON_TRANSFORMATIVE_LEVELS = {None, "", "none", "original", "global", "global-only", "lightroom-global", 0}


def _finite_number(value: Any, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _number(value: Any, default: float = 0.0) -> float:
    number = _finite_number(value)
    return number if number is not None else default


def _warning_code(warning: Any) -> str:
    if isinstance(warning, str):
        return warning
    if isinstance(warning, dict):
        return str(warning.get("code", warning.get("message", "warning")))
    return "warning"


def _is_critical(warning: Any) -> bool:
    if isinstance(warning, dict):
        return warning.get("severity") == "critical" or warning.get("critical") is True
    return isinstance(warning, str) and warning.casefold().startswith("critical")


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _canonical_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return str(Path(value).expanduser().resolve())


def _manifest_context(manifest: Any) -> dict | None:
    if not verify_run_manifest(manifest):
        return None
    return deepcopy(manifest["trusted_context"])


def _context_identity(context: dict) -> tuple[str | None, tuple[tuple[str | None, Any], ...]]:
    assets = context.get("source_assets")
    identities = tuple(sorted(
        (_canonical_path(asset.get("path")), asset.get("sha256"))
        for asset in assets if isinstance(asset, dict)
    )) if isinstance(assets, list) else ()
    return context.get("trust_root_digest"), identities


def _selection_context(report: Any) -> tuple[dict | None, str | None]:
    if not isinstance(report, dict):
        return None, "missing_trusted_context"
    manifest = report.get("run_manifest")
    detached = report.get("trusted_context")
    if manifest is not None:
        context = _manifest_context(manifest)
        if context is None:
            return None, "invalid_or_unsealed_manifest"
        if detached is not None:
            if not verify_trusted_context(detached) or _context_identity(detached) != _context_identity(context):
                return None, "trust_context_conflict"
        return context, None
    if verify_trusted_context(detached):
        return deepcopy(detached), None
    if detached is not None:
        return None, "invalid_trusted_context"
    return None, "missing_trusted_context"


def _candidate_context_error(report: Any, trusted_context: dict) -> str | None:
    if not isinstance(report, dict):
        return "invalid_candidate"
    if "run_manifest" in report:
        context = _manifest_context(report.get("run_manifest"))
        if context is None:
            return "invalid_or_unsealed_manifest"
        if _context_identity(context) != _context_identity(trusted_context):
            return "trust_context_conflict"
    if "trusted_context" in report:
        candidate_context = report.get("trusted_context")
        if not verify_trusted_context(candidate_context):
            return "invalid_trusted_context"
        if _context_identity(candidate_context) != _context_identity(trusted_context):
            return "trust_context_conflict"
    return None


def _source_issues(context: dict, candidate: dict) -> list[str]:
    assets = context.get("source_assets")
    if not isinstance(assets, list) or not assets:
        return ["invalid_source_manifest"]
    normalized: list[tuple[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            return ["invalid_source_manifest"]
        path = _canonical_path(asset.get("path"))
        sha256 = asset.get("sha256")
        if not path or not isinstance(sha256, str) or not sha256.strip():
            return ["invalid_source_manifest"]
        normalized.append((path, sha256))
    if len({path for path, _ in normalized}) != len(normalized):
        return ["ambiguous_source_asset"]
    candidate_path = _canonical_path(candidate.get("source_path"))
    matches = [(path, sha256) for path, sha256 in normalized if path == candidate_path]
    if not candidate_path or not matches:
        return ["source_path_mismatch" if len(normalized) == 1 else "source_not_in_manifest"]
    if len(matches) != 1:
        return ["ambiguous_source_asset"]
    issues: list[str] = []
    candidate_hash = candidate.get("source_sha256", candidate.get("source_hash"))
    if not isinstance(candidate_hash, str) or candidate_hash != matches[0][1]:
        issues.append("source_sha256_mismatch")
    expected_snapshot = context.get("source_snapshot_sha256")
    candidate_snapshot = candidate.get(
        "source_snapshot_sha256",
        candidate.get("source_snapshot_hash", candidate.get("snapshot_sha256")),
    )
    if not isinstance(expected_snapshot, str) or not expected_snapshot or candidate_snapshot != expected_snapshot:
        issues.append("source_snapshot_sha256_mismatch")
    return issues


def _requires_operation_graph(candidate: dict, plan: Any) -> bool:
    level = candidate.get("transformation_level")
    if level not in _NON_TRANSFORMATIVE_LEVELS:
        return True
    if candidate.get("generative_used") is True or candidate.get("generated") is True:
        return True
    generative = candidate.get("generative")
    if isinstance(generative, dict) and generative.get("used") is True:
        return True
    if isinstance(plan, dict):
        operations = plan.get("operations")
        return isinstance(operations, list) and any(
            isinstance(operation, dict)
            and (operation.get("generative") is True or operation.get("type") in _TRANSFORMATIVE_TYPES)
            for operation in operations
        )
    return False


def _operation_issues(plan: Any, require_graph: bool = False) -> list[str]:
    if plan is None:
        return ["missing_edit_plan"] if require_graph else []
    if not isinstance(plan, dict):
        return ["missing_edit_plan"]
    operations = plan.get("operations", [])
    records = plan.get("operation_records", [])
    if not isinstance(operations, list) or not isinstance(records, list):
        return ["invalid_edit_graph"]
    issues: list[str] = []
    if require_graph and not operations:
        _append_unique(issues, "missing_operation_graph")
    operation_ids: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            _append_unique(issues, "invalid_edit_graph")
            continue
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            _append_unique(issues, "blank_operation_id")
        elif operation_id in operation_ids:
            _append_unique(issues, "duplicate_operation_id")
        else:
            operation_ids.add(operation_id)
    graph: dict[str, list[str]] = {}
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        operation_id = operation.get("operation_id")
        dependencies = operation.get("depends_on")
        if not isinstance(operation_id, str) or operation_id not in operation_ids or not isinstance(dependencies, list):
            _append_unique(issues, "invalid_edit_graph")
            continue
        graph[operation_id] = dependencies
        if any(not isinstance(dep, str) or dep not in operation_ids or dep == operation_id for dep in dependencies):
            _append_unique(issues, "broken_operation_dependency")
    visiting: set[str] = set()
    visited: set[str] = set()

    def cyclic(operation_id: str) -> bool:
        if operation_id in visiting:
            return True
        if operation_id in visited:
            return False
        visiting.add(operation_id)
        found = any(cyclic(dependency) for dependency in graph.get(operation_id, []) if dependency in graph)
        visiting.discard(operation_id)
        visited.add(operation_id)
        return found

    if any(cyclic(operation_id) for operation_id in graph):
        _append_unique(issues, "operation_dependency_cycle")
    records_by_id: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            _append_unique(issues, "invalid_operation_record")
            continue
        operation_id = record.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            _append_unique(issues, "blank_operation_record_id")
        elif operation_id in records_by_id:
            _append_unique(issues, "duplicate_operation_record_id")
        else:
            records_by_id[operation_id] = record
    if any(record_id not in operation_ids for record_id in records_by_id):
        _append_unique(issues, "orphaned_operation_record")
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        operation_id = operation.get("operation_id")
        transformative = operation.get("generative") is True or operation.get("type") in _TRANSFORMATIVE_TYPES
        if transformative:
            record = records_by_id.get(operation_id)
            if not isinstance(record, dict) or any(
                not isinstance(record.get(field), str) or not record[field].strip()
                for field in _PROVENANCE_FIELDS
            ):
                _append_unique(issues, "missing_transformation_provenance")
    if require_graph and operations and not any(
        isinstance(operation, dict)
        and (operation.get("generative") is True or operation.get("type") in _TRANSFORMATIVE_TYPES)
        for operation in operations
    ):
        _append_unique(issues, "missing_transformative_operation")
    return issues


def _operation_locality_issues(plan: Any, locality_policy: Any) -> list[str]:
    """Reject an explicit cloud operation when the run is local-only.

    Locality is recorded at operation level for Hybrid provenance. Older local
    plans do not have the optional field, so this remains backward compatible
    while preventing a cloud escape from a sealed local run.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("operations"), list):
        return []
    if locality_policy != "local-only":
        return []
    issues: list[str] = []
    for operation in plan["operations"]:
        if not isinstance(operation, dict):
            continue
        if not (operation.get("generative") is True or operation.get("type") in _TRANSFORMATIVE_TYPES):
            continue
        parameters = operation.get("parameters") if isinstance(operation.get("parameters"), dict) else {}
        locality = operation.get("backend_locality", parameters.get("backend_locality"))
        if locality in {"cloud", "remote"}:
            _append_unique(issues, "generative_locality_policy_violation")
    return issues


def _magnitude_issue(name: str, value: Any, budget: dict) -> str | None:
    number = _finite_number(value)
    key = name.casefold().replace("-", "_")
    if number is None:
        return f"invalid_adjustment:{key}"
    rules: list[tuple[set[str], str, bool]] = [
        ({"exposure", "exposure_delta"}, "max_exposure_delta", True),
        ({"crop", "crop_fraction"}, "max_crop_fraction", False),
        ({"temperature_delta", "temperature_shift"}, "max_temperature_delta", True),
        ({"sharpening", "sharpen", "sharpness"}, "max_sharpening", False),
        ({"geometry", "geometry_delta", "rotate", "rotation", "perspective", "vertical", "horizontal"}, "max_geometry_delta", True),
    ]
    for aliases, limit_name, absolute in rules:
        if key in aliases:
            limit = _finite_number(budget.get(limit_name), 0)
            if limit is None or (abs(number) if absolute else number) > limit or (not absolute and number < 0):
                return f"adjustment_magnitude:{key}"
            return None
    if key == "temperature":
        minimum = _finite_number(budget.get("min_temperature", 2000), 0)
        maximum = _finite_number(budget.get("max_temperature", 50000), 0)
        if minimum is None or maximum is None or not minimum <= number <= maximum:
            return "adjustment_magnitude:temperature"
    if key in {"contrast", "highlights", "shadows", "whites", "blacks", "clarity", "texture", "dehaze", "saturation", "vibrance", "tint"}:
        limit = _finite_number(budget.get("max_tone_delta", 100), 0)
        if limit is None or abs(number) > limit:
            return f"adjustment_magnitude:{key}"
    return None


def _adjustment_issues(plan: Any, selected_budget: Any) -> list[str]:
    if not isinstance(plan, dict):
        return []
    if not isinstance(selected_budget, dict) or not selected_budget:
        return ["adjustment_budget"]
    issues: list[str] = []
    global_adjustments = plan.get("global_adjustments", [])
    regions = plan.get("regions", plan.get("local_adjustments", []))
    operations = plan.get("operations", [])
    if not isinstance(global_adjustments, list) or not isinstance(regions, list) or not isinstance(operations, list):
        return ["invalid_adjustments"]
    transformative_count = sum(
        1 for operation in operations if isinstance(operation, dict)
        and (operation.get("generative") is True or operation.get("type") in _TRANSFORMATIVE_TYPES)
    )
    counts = (
        ("max_global_adjustments", len(global_adjustments)),
        ("max_local_adjustments", len(regions)),
        ("max_transformative_operations", transformative_count),
    )
    for limit_name, count in counts:
        limit = _finite_number(selected_budget.get(limit_name), 0)
        if limit is None or count > limit:
            _append_unique(issues, "adjustment_budget")
    maps: list[dict] = []
    maps.extend(item for item in global_adjustments if isinstance(item, dict))
    for region in regions:
        if isinstance(region, dict) and isinstance(region.get("adjustments"), dict):
            maps.append(region["adjustments"])
    for operation in operations:
        if isinstance(operation, dict) and isinstance(operation.get("parameters"), dict):
            maps.append(operation["parameters"])
    for adjustment_map in maps:
        for name, value in adjustment_map.items():
            issue = _magnitude_issue(str(name), value, selected_budget)
            if issue:
                _append_unique(issues, issue)
    return issues


def _trusted_evaluation_issues(evaluation: Any, candidate_id: Any) -> tuple[list[str], dict, float, float]:
    issues: list[str] = []
    if not isinstance(evaluation, dict):
        return ["missing_trusted_evaluation"], {}, 0.0, 0.0
    evaluation_id = evaluation.get("candidate_id")
    if evaluation_id is not None and str(evaluation_id) != str(candidate_id):
        _append_unique(issues, "trusted_evaluation_candidate_mismatch")
    gates = evaluation.get("technical_gates")
    if not isinstance(gates, dict) or not gates or any(status not in {"pass", "warn", "fail"} for status in gates.values()):
        _append_unique(issues, "invalid_technical_gates")
        gates = {}
    score_value = _finite_number(evaluation.get("final_score"), 0, 100)
    if score_value is None:
        _append_unique(issues, "invalid_final_score")
        score_value = 0.0
    confidence_value = _finite_number(evaluation.get("score_confidence"), 0, 1)
    if confidence_value is None:
        _append_unique(issues, "invalid_score_confidence")
        confidence_value = 0.0
    # These final-image gates are optional for legacy callers, but mandatory
    # whenever the adapter supplies the corresponding post-render measures.
    thresholds = {
        "technical_score": (75.0, "technical_score_below_release_threshold"),
        "aesthetic_score": (78.0, "aesthetic_score_below_release_threshold"),
        "improvement_score": (10.0, "improvement_score_below_release_threshold"),
    }
    for field, (minimum, issue) in thresholds.items():
        if field not in evaluation:
            continue
        value = _finite_number(evaluation.get(field), 0, 100)
        if value is None or value < minimum:
            _append_unique(issues, issue)
    if evaluation.get("critical_artifacts") is True:
        _append_unique(issues, "critical_semantic_artifact")
    return issues, gates, score_value, confidence_value


def _requested_release_label(candidate: dict, release_context: dict) -> str | None:
    """Resolve the requested delivery label from verified run/candidate evidence."""
    for value in (
        release_context.get("requested_label"),
        candidate.get("requested_label"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    plan = candidate.get("edit_plan") or candidate.get("operation_manifest") or candidate.get("edit_graph")
    if isinstance(plan, dict):
        value = plan.get("intent")
        if isinstance(value, str) and value.strip():
            return value.strip()
    manifest = candidate.get("run_manifest")
    if isinstance(manifest, dict):
        value = manifest.get("intent")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def evaluate_candidate(before: dict, after: dict, trusted_evaluation: dict) -> dict:
    """Evaluate a candidate using a sealed trust root and separate evaluator output."""
    candidate = after if isinstance(after, dict) else {}
    context = deepcopy(before) if verify_trusted_context(before) else _manifest_context(before)
    merged = deepcopy(candidate)
    warnings = [_warning_code(item) for item in candidate.get("warnings", [])] if isinstance(candidate.get("warnings", []), list) else ["invalid_warnings"]
    failure_reasons: list[str] = []
    if context is None:
        context = deepcopy(before) if isinstance(before, dict) else {}
        _append_unique(failure_reasons, "invalid_or_unsealed_trusted_context")
    candidate_id = candidate.get("candidate_id", candidate.get("iteration"))
    evaluation_issues, gates, score, confidence = _trusted_evaluation_issues(trusted_evaluation, candidate_id)
    for issue in evaluation_issues:
        _append_unique(warnings, issue)
        _append_unique(failure_reasons, issue)
    if context.get("intent") == "competition-standard" and score < 85.0:
        _append_unique(warnings, "competition_final_score_below_85")
        _append_unique(failure_reasons, "competition_final_score_below_85")
    if gates and "fail" in gates.values():
        _append_unique(warnings, "technical_gate_failed")
        _append_unique(failure_reasons, "technical_gate_failed")
    if any(_is_critical(item) for item in candidate.get("warnings", []) if isinstance(candidate.get("warnings", []), list)):
        _append_unique(warnings, "critical_warning")
        _append_unique(failure_reasons, "critical_warning")
    for issue in _source_issues(context, candidate):
        _append_unique(warnings, issue)
        _append_unique(failure_reasons, issue)
    policy = context.get("locality_policy")
    locality = candidate.get("processing_locality")
    allowed_localities = {
        "local-only": {"local-only", "local"},
        "mixed": {"mixed", "local-only", "local", "cloud"},
        "mixed-locality": {"mixed", "local-only", "local", "cloud"},
        "allow-cloud-generation": {"cloud", "mixed", "local-only", "local"},
        "remote": {"remote"},
    }.get(policy, set())
    if locality not in allowed_localities:
        _append_unique(warnings, "locality_policy_violation")
        _append_unique(failure_reasons, "locality_policy_violation")
    plan = candidate.get("edit_plan") or candidate.get("operation_manifest") or candidate.get("edit_graph")
    require_graph = _requires_operation_graph(candidate, plan)
    for issue in _adjustment_issues(plan, context.get("intent_budget")):
        _append_unique(warnings, issue)
        _append_unique(failure_reasons, issue)
    for issue in _operation_issues(plan, require_graph):
        _append_unique(warnings, issue)
        _append_unique(failure_reasons, issue)
    for issue in _operation_locality_issues(plan, policy):
        _append_unique(warnings, issue)
        _append_unique(failure_reasons, issue)
    merged.update({
        "final_score": score,
        "score_confidence": confidence,
        "technical_gates": deepcopy(gates),
        "warnings": warnings,
        "technical_gate_passed": bool(gates) and "fail" not in gates.values(),
        "failure_reasons": failure_reasons,
    })
    if failure_reasons:
        merged.update({"valid": False, "decision": "rejected", "rejection_reason": failure_reasons[0]})
    elif confidence < REVIEW_CONFIDENCE_THRESHOLD:
        _append_unique(warnings, "low_confidence")
        merged.update({"valid": True, "decision": "review", "rejection_reason": None})
    else:
        merged.update({"valid": True, "decision": "selected", "rejection_reason": None})
    return merged


def decide_release(candidate: dict, release_context: dict | None = None) -> dict:
    """Return an executable final label; competition-standard is fail-closed."""
    candidate = candidate if isinstance(candidate, dict) else {}
    context = release_context if isinstance(release_context, dict) else {}
    if candidate.get("decision") == "rejected":
        return {"final_label": "rejected", "release_blockers": ["candidate_rejected"]}
    if candidate.get("decision") != "selected":
        return {"final_label": "review", "release_blockers": ["candidate_requires_review"]}
    requested = _requested_release_label(candidate, context)
    if requested != "competition-standard":
        return {"final_label": "global-only", "release_blockers": ["competition_standard_not_requested"]}
    blockers: list[str] = []
    adapter = context.get("adapter_status") or candidate.get("adapter_status")
    if not isinstance(adapter, dict) or not (
        adapter.get("available") is True
        and adapter.get("fine_edit_mode") is True
        and adapter.get("mode") == "fine-edit"
    ):
        _append_unique(blockers, "photoshop_fine_edit_unavailable")
    score = _finite_number(candidate.get("final_score"), 0, 100)
    if score is None or score < 85:
        _append_unique(blockers, "aesthetic_score_below_85")
    gates = candidate.get("technical_gates")
    if not isinstance(gates, dict) or not gates or any(status != "pass" for status in gates.values()):
        _append_unique(blockers, "technical_gates_not_all_pass")
    plan = candidate.get("edit_plan") or candidate.get("operation_manifest") or candidate.get("edit_graph")
    if _requires_operation_graph(candidate, plan):
        if _operation_issues(plan, True):
            _append_unique(blockers, "incomplete_transformation_provenance")
    if not all(isinstance(candidate.get(field), str) and candidate[field].strip() for field in ("source_path", "source_sha256", "source_snapshot_sha256")):
        _append_unique(blockers, "incomplete_source_provenance")
    master = context.get("editable_master") or candidate.get("editable_master")
    if not isinstance(master, dict) or not (
        master.get("valid") is True
        and master.get("editable") is True
        and master.get("layered") is True
        and isinstance(master.get("layer_ids"), list) and bool(master["layer_ids"])
        and isinstance(master.get("mask_ids"), list) and bool(master["mask_ids"])
    ):
        _append_unique(blockers, "editable_master_incomplete")
    export = context.get("export_validation") or candidate.get("export_validation")
    if not isinstance(export, dict) or export.get("valid") is not True or export.get("profile") != "competition-quality" or export.get("release_blockers"):
        _append_unique(blockers, "competition_export_invalid")
    disclosure = context.get("transformation_disclosure") or candidate.get("transformation_disclosure")
    if not isinstance(disclosure, str) or not disclosure.strip():
        _append_unique(blockers, "transformation_disclosure_missing")
    return {
        "final_label": "competition-standard" if not blockers else "global-only",
        "release_blockers": blockers,
    }


def _evaluation_map(evaluations: Any) -> dict[str, dict]:
    if isinstance(evaluations, dict):
        if "candidate_id" in evaluations:
            return {str(evaluations["candidate_id"]): evaluations}
        return {str(key): value for key, value in evaluations.items() if isinstance(value, dict)}
    if isinstance(evaluations, list):
        return {
            str(item["candidate_id"]): item
            for item in evaluations if isinstance(item, dict) and item.get("candidate_id") is not None
        }
    return {}


def choose_best_candidate(
    reports: list[dict],
    trusted_evaluations: list[dict] | dict[str, dict] | None = None,
    release_context: dict | None = None,
) -> dict:
    """Choose the best verified checkpoint using external trusted evaluations."""
    empty = {
        "candidate_id": None, "decision": "rejected", "final_label": "rejected",
        "stopping_reason": "no_candidates", "evaluated_iterations": 0,
        "rejected_candidates": [], "rollback_to": None,
    }
    if not isinstance(reports, list) or not reports:
        return empty
    trusted_context, context_error = _selection_context(reports[0])
    if context_error:
        return {**empty, "stopping_reason": context_error}
    evaluations = _evaluation_map(trusted_evaluations)
    candidate_limit_value = _finite_number(trusted_context.get("intent_budget", {}).get("max_candidates"), 0)
    candidate_limit = min(MAX_ITERATIONS, int(candidate_limit_value)) if candidate_limit_value is not None else 0
    best: dict | None = None
    rejected: list[str] = []
    evaluated = 0
    stopping_reason = "candidates_exhausted"
    for report in reports:
        if evaluated >= candidate_limit:
            stopping_reason = "max_iterations"
            break
        evaluated += 1
        candidate_id = str(report.get("candidate_id", report.get("iteration", evaluated))) if isinstance(report, dict) else str(evaluated)
        context_issue = _candidate_context_error(report, trusted_context)
        if context_issue:
            rejected.append(candidate_id)
            continue
        result = evaluate_candidate(trusted_context, report, evaluations.get(candidate_id))
        if result["decision"] == "rejected":
            rejected.append(candidate_id)
            continue
        if best is None or result["final_score"] > best["final_score"]:
            previous_score = best["final_score"] if best else None
            best = result
            improvement = result["final_score"] - previous_score if previous_score is not None else None
            saturation_limit = _number(result.get("quality_saturation_threshold", SATURATION_DELTA), SATURATION_DELTA)
            if previous_score is not None and improvement <= saturation_limit:
                stopping_reason = "quality_saturation"
                break
        else:
            rejected.append(candidate_id)
        if result.get("brief_satisfied") or result.get("director_brief_satisfied"):
            stopping_reason = "director_brief_satisfied"
            break
    else:
        if evaluated >= candidate_limit:
            stopping_reason = "max_iterations"
    if best is None:
        return {
            **empty,
            "stopping_reason": stopping_reason,
            "evaluated_iterations": evaluated,
            "rejected_candidates": rejected,
        }
    release = decide_release(best, release_context)
    result = deepcopy(best)
    result.update({
        "stopping_reason": stopping_reason,
        "evaluated_iterations": evaluated,
        "rejected_candidates": rejected,
        "rollback_to": best.get("after_path"),
        "final_label": release["final_label"],
        "release_blockers": release["release_blockers"],
    })
    return result
