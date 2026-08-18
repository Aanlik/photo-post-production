"""Resumable execution boundary for Lightroom, Photoshop and chat image_gen.

The local process never pretends to be Adobe or the chat-window tool.  It
accepts a small JSON adapter protocol, records every response, and leaves work
in a paused state when the host capability is absent.  This makes the same
plan executable from a future connected Codex session without changing the
analysis result.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import shutil
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durable_queue import claim_item, get_item, transition, update_checkpoint
from operation_graph import build_operation_graph, validate_operation_graph
from edit_plans import attach_execution_audit, materialize_executable_plan
from quality_gate import choose_best_candidate, evaluate_candidate
from semantic_checks import compare_semantics
from validate_export import validate_export
from image_metrics import compare_rendered_pixels, evaluate_rendered_candidate
from resource_guard import check_disk_budget, estimate_peak_disk_bytes
from closed_loop import evaluate_iteration


PROTOCOL_VERSION = "photo-post-production-adapter-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _directory_size(root: Path) -> int:
    """Return the current output footprint without following symlinks."""

    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _disk_budget_bytes(run_manifest: dict[str, Any]) -> int:
    resource_budget = run_manifest.get("resource_budget") if isinstance(run_manifest, dict) else None
    configured = resource_budget.get("disk_budget_bytes") if isinstance(resource_budget, dict) else None
    if configured is None:
        configured = os.environ.get("PHOTO_POST_DISK_BUDGET_BYTES", 40 * 1024 ** 3)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = 40 * 1024 ** 3
    return value if value > 0 else 0


def _document_identity_matches(expected: Any, actual: Any) -> bool:
    """Allow the first document identity, then require exact reuse."""

    if expected in (None, ""):
        return isinstance(actual, str) and bool(actual.strip())
    return isinstance(actual, str) and actual.strip() == str(expected).strip()


def _verified_document_identity_rollover(
    expected: Any,
    actual: Any,
    evidence: dict[str, Any],
    runtime_context: dict[str, Any],
) -> bool:
    """Accept a Photoshop session refresh only with same-path evidence."""

    if not isinstance(actual, str) or not actual.strip():
        return False
    if evidence.get("document_identity_reconciled") is not True:
        return False
    if str(evidence.get("document_identity_previous") or "").strip() != str(expected or "").strip():
        return False
    expected_path = runtime_context.get("working_path")
    actual_path = evidence.get("document_path") or evidence.get("before_path")
    if not isinstance(expected_path, str) or not expected_path.strip():
        return False
    if not isinstance(actual_path, str) or not actual_path.strip():
        return False
    try:
        return Path(expected_path).expanduser().resolve() == Path(actual_path).expanduser().resolve()
    except OSError:
        return False


def _job_disk_budget_decision(job: dict[str, Any], current_bytes: int, budget_bytes: int) -> dict[str, Any]:
    """Estimate a job's peak footprint before opening Lightroom/Photoshop."""

    score = job.get("score") if isinstance(job.get("score"), dict) else {}
    dimensions = job.get("display_dimensions") or score.get("display_dimensions") or score.get("raw_dimensions") or [6000, 4000]
    try:
        width, height = int(dimensions[0]), int(dimensions[1])
    except (IndexError, TypeError, ValueError):
        width, height = 6000, 4000
    source_path = Path(str(job.get("source_path") or score.get("source_path") or ""))
    try:
        source_bytes = source_path.stat().st_size if source_path.is_file() else int(job.get("source_bytes") or score.get("source_bytes") or 0)
    except (OSError, TypeError, ValueError):
        source_bytes = 0
    projected = estimate_peak_disk_bytes(
        source_bytes=source_bytes,
        width=width,
        height=height,
        bit_depth=int(job.get("bit_depth") or score.get("bit_depth") or 16),
        photoshop_layers=int(job.get("photoshop_layers") or score.get("photoshop_layers") or 8),
        keep_working_tiff=bool(job.get("has_photoshop", True)),
    )
    return check_disk_budget(current_bytes, projected, budget_bytes)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_for_backend(backend: str) -> tuple[str | None, str]:
    if backend == "lightroom-mcp":
        configured = os.environ.get("PHOTO_LIGHTROOM_ADAPTER_COMMAND")
        fallback = Path(__file__).with_name("lightroom_mcp_adapter.py")
        return configured or (f"{shlex.quote(os.environ.get('PHOTO_POST_PRODUCTION_PYTHON', 'python3'))} {shlex.quote(str(fallback))}" if fallback.is_file() else None), "lightroom"
    if backend == "photoshop-fine-edit":
        configured = os.environ.get("PHOTO_PHOTOSHOP_ADAPTER_COMMAND")
        fallback = Path(__file__).with_name("photoshop_mcp_adapter.py")
        return configured or (f"{shlex.quote(os.environ.get('PHOTO_POST_PRODUCTION_PYTHON', 'python3'))} {shlex.quote(str(fallback))}" if fallback.is_file() else None), "photoshop"
    return None, "host"


def _call_json_adapter(command: str, request: dict[str, Any], timeout: int = 600) -> dict[str, Any]:
    """Call a user-configured local JSON adapter without invoking a shell."""

    argv = shlex.split(command)
    if not argv:
        return {"status": "unavailable", "reason": "empty_adapter_command"}
    process = None
    try:
        process = subprocess.Popen(
            [*argv, "--photo-post-production-json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(
            input=json.dumps(request, ensure_ascii=False),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.communicate()
        return {"status": "paused", "reason": "adapter_timeout"}
    except OSError as error:
        return {"status": "unavailable", "reason": f"adapter_start_failed:{type(error).__name__}"}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "reason": "adapter_returned_invalid_json",
            "returncode": process.returncode if process is not None else None,
            "stderr_tail": (stderr or "")[-1000:],
        }
    if not isinstance(payload, dict):
        return {"status": "failed", "reason": "adapter_result_must_be_object"}
    if process is not None and process.returncode != 0 and payload.get("status") == "completed":
        payload["status"] = "failed"
        payload.setdefault("reason", f"adapter_returncode:{process.returncode}")
    return payload


def _pending_reason(operation: dict[str, Any], processing_locality: str) -> str:
    backend = operation.get("backend")
    if backend == "chat-window-imagegen":
        return "chat_window_imagegen_requires_current_host_tool_call"
    if processing_locality == "local-only" and operation.get("generative"):
        return "local_only_generation_requires_independent_local_backend"
    if backend == "lightroom-mcp":
        return "lightroom_mcp_adapter_not_configured_in_current_host"
    if backend == "photoshop-fine-edit":
        return "photoshop_adapter_not_configured_in_current_host"
    return "adapter_not_configured"


def _job_graph(job: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    graph = job.get("operation_graph")
    if not isinstance(graph, dict):
        graph = build_operation_graph(job.get("score", {}), job.get("edit_plan", {}), job.get("adapter_plan", {}), job.get("capabilities", {}))
    return graph, validate_operation_graph(graph)


_STRUCTURED_PHOTOSHOP_OPERATIONS = {
    "layer_operation",
    "smart_object",
    "layer_mask",
    "region_mask_operation",
    "selective_color",
    "curves_local",
    "noise_reduction",
    "sharpening",
    "portrait_beauty",
    "apply_crop",
}

_DESCRIPTOR_ONLY_PHOTOSHOP_OPERATIONS = {
    "dodge_burn",
    "healing",
    "clone_stamp",
    "eraser_mask",
    "content_aware_remove",
    "perspective_warp",
    "liquify",
    "recorded_action",
}


def _operation_execution_gate(operation: dict[str, Any]) -> tuple[bool, str | None]:
    """Return whether a planned node has a verified local execution path.

    A command file being present is not proof that every Photoshop tool is
    safe to invoke.  Structured operations may use the adapter directly;
    brush/geometry operations require a verified route or Action Descriptor.
    """

    backend = str(operation.get("backend") or "")
    if backend == "chat-window-imagegen":
        return False, "chat_window_imagegen_requires_current_host_tool_call"
    if backend != "photoshop-fine-edit":
        return True, None
    route = operation.get("execution_route") if isinstance(operation.get("execution_route"), dict) else {}
    tier = str(route.get("tier") or "")
    tool = str(operation.get("adapter_operation") or "")
    if tier in {"descriptor-verified", "stable-auto"}:
        return True, None
    if tier in {"unsupported", "ui-assisted"}:
        return False, str(route.get("reason") or f"photoshop_route_{tier}")
    if operation.get("requires_descriptor") is True or tool in _DESCRIPTOR_ONLY_PHOTOSHOP_OPERATIONS:
        return False, "verified_action_descriptor_required"
    if tool in _STRUCTURED_PHOTOSHOP_OPERATIONS:
        parameters = operation.get("parameters") if isinstance(operation.get("parameters"), dict) else {}
        meaningful_parameters = set(parameters) - {"tool", "variant", "descriptor_id"}
        if operation.get("required", True) is False and not meaningful_parameters:
            return False, "optional_operation_missing_executable_parameters"
        return True, None
    return False, "no_verified_executor"


def _execution_operations(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every graph node in dependency order, including optional nodes."""

    return [item for item in graph.get("operations", []) if isinstance(item, dict)]


def _operation_summary(graph: dict[str, Any], responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize actual node coverage without treating planning as evidence."""

    operations = _execution_operations(graph)
    response_by_id = {
        str(item.get("operation_id")): item
        for item in responses
        if isinstance(item, dict) and item.get("operation_id") is not None
    }
    counts = {"total": len(operations), "completed": 0, "skipped": 0, "blocked": 0, "planned": 0}
    records: list[dict[str, Any]] = []
    for operation in operations:
        operation_id = str(operation.get("operation_id"))
        response = response_by_id.get(operation_id, {})
        status = str(response.get("status") or operation.get("status") or "planned")
        bucket = status if status in counts else "blocked" if status in {"paused", "failed"} else "planned"
        counts[bucket] += 1
        records.append({
            "operation_id": operation_id,
            "backend": operation.get("backend"),
            "required": operation.get("required", True) is not False,
            "status": status,
            "reason": response.get("reason"),
        })
    photoshop = [item for item in records if item.get("backend") == "photoshop-fine-edit"]
    photoshop_completed = sum(item.get("status") == "completed" for item in photoshop)
    return {
        "counts": counts,
        "photoshop": {
            "total": len(photoshop),
            "completed": photoshop_completed,
            "coverage": round(photoshop_completed / len(photoshop), 4) if photoshop else 0.0,
            "fine_edit_verified": bool(photoshop) and photoshop_completed == len(photoshop),
        },
        "operations": records,
    }


def _finalize_execution_result(result: dict[str, Any], graph_status: str) -> dict[str, Any]:
    """Synchronize the top-level graph status with its verified node results."""

    graph = result.get("operation_graph") if isinstance(result.get("operation_graph"), dict) else {}
    graph["status"] = graph_status
    result["operation_graph"] = graph
    result["operation_summary"] = _operation_summary(graph, result.get("operation_results", []))
    return result


def _operation_request(
    job: dict[str, Any],
    operation: dict[str, Any],
    run_id: str,
    iteration: int = 1,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = job.get("source_path") or job.get("score", {}).get("source_path")
    context = runtime_context if isinstance(runtime_context, dict) else {}
    graph = job.get("operation_graph") if isinstance(job.get("operation_graph"), dict) else {}
    graph_operations = _execution_operations(graph)
    executable_operations = [item for item in graph_operations if _operation_execution_gate(item)[0]]
    is_final_operation = bool(executable_operations) and str(executable_operations[-1].get("operation_id")) == str(operation.get("operation_id"))
    return {
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "photo_id": job.get("photo_id"),
        "variant_name": job.get("variant_name", "natural"),
        "operation": deepcopy(operation),
        "source_path": str(source) if source else None,
        "source_read_only": True,
        "idempotency_key": operation.get("operation_id"),
        "iteration": iteration,
        "iteration_key": f"{operation.get('operation_id')}:iteration-{iteration}",
        "output_dir": job.get("output_dir"),
        "adapter_plan": deepcopy(job.get("adapter_plan", {})),
        "score": deepcopy(job.get("score", {})),
        "problem_driven_plan": deepcopy(job.get("score", {}).get("problem_driven_plan", {})) if isinstance(job.get("score"), dict) else {},
        "document_id": context.get("document_id") or (
            context.get("last_evidence", {}).get("document_id")
            if isinstance(context.get("last_evidence"), dict)
            else None
        ) or job.get("document_id"),
        "working_path": context.get("working_path"),
        "previous_evidence": deepcopy(context.get("last_evidence", {})),
        "is_final_operation": is_final_operation,
        "history_policy": {
            "mode": "photoshop-history",
            "snapshot_name": operation.get("checkpoint"),
            "persist_intermediate": False,
            "final_save_only": True,
        },
    }


def _normalise_adapter_result(result: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(result)
    normalized.setdefault("status", "failed")
    normalized.setdefault("operation_id", operation.get("operation_id"))
    if normalized.get("operation_id") != operation.get("operation_id"):
        normalized.update({"status": "failed", "reason": "operation_id_mismatch"})
    if normalized.get("status") == "completed":
        evidence = normalized.get("evidence")
        if not isinstance(evidence, dict):
            normalized.update({"status": "failed", "reason": "completed_adapter_result_missing_evidence"})
        elif not evidence.get("after_path") and not normalized.get("after_path"):
            normalized.update({"status": "failed", "reason": "completed_adapter_result_missing_after_path"})
    return normalized


def execute_job(job: dict[str, Any], queue_path: str, run_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Execute one job or record a recoverable pending state."""

    item_id = str(job.get("item_id") or f"{run_id}:{job.get('photo_id')}:{job.get('variant_name', 'natural')}")
    graph, graph_errors = _job_graph(job)
    result: dict[str, Any] = {
        "item_id": item_id,
        "photo_id": job.get("photo_id"),
        "variant_name": job.get("variant_name", "natural"),
        "started_at": _now(),
        "status": "planned",
        "operation_results": [],
        "operation_graph": deepcopy(graph),
        "graph_errors": graph_errors,
        "blockers": [],
        "downgrades": [],
        "iteration_history": [],
        "closed_loop_history": [],
    }
    if graph_errors:
        result.update({"status": "failed", "blockers": ["invalid_operation_graph"]})
        return _finalize_execution_result(result, "failed")
    if dry_run:
        result.update({"status": "dry-run", "blockers": ["dry_run_no_adapter_write"]})
        return _finalize_execution_result(result, "dry-run")

    queue_item = get_item(queue_path, item_id)
    if queue_item and queue_item.get("state") in {"paused", "failed"}:
        try:
            transition(queue_path, item_id, "queued")
        except ValueError:
            pass
        queue_item = get_item(queue_path, item_id)
    if queue_item and queue_item.get("state") == "queued":
        claimed = claim_item(queue_path, item_id)
        if claimed is None:
            result.update({"status": "paused", "blockers": ["queue_claim_conflict"]})
            return _finalize_execution_result(result, "paused")
    operations = _execution_operations(graph)
    has_lightroom = any(operation.get("backend") == "lightroom-mcp" for operation in operations)
    has_photoshop = any(operation.get("backend") == "photoshop-fine-edit" for operation in operations)
    # A real photograph always needs a Lightroom render/export boundary.  If
    # Photoshop is planned, export a working TIFF before it opens the document;
    # otherwise export the final Lightroom result after any Develop settings.
    if not has_lightroom:
        synthetic = {
            "operation_id": f"{job.get('photo_id')}:lr-export",
            "type": "relight-subject",
            "depends_on": [],
            "backend": "lightroom-mcp",
            "reason": "读取原始 RAW 并以新目录导出成片",
            "affected_region": "global-frame",
            "parameters": {"export_only": True, "working_tiff": has_photoshop},
            "risk": "low",
            "checkpoint": "before-lightroom-export",
            "generative": False,
            "input_layer": "raw-develop",
            "output_layer": "lightroom-export",
            "required": True,
        }
        operations = [synthetic, *operations]
        result["operation_graph"]["operations"] = deepcopy(operations)
    elif not has_photoshop and not any(operation.get("parameters", {}).get("export_only") for operation in operations if isinstance(operation.get("parameters"), dict)):
        last = operations[-1] if operations else None
        synthetic = {
            "operation_id": f"{job.get('photo_id')}:lr-export",
            "type": "relight-subject",
            "depends_on": [last.get("operation_id")] if isinstance(last, dict) else [],
            "backend": "lightroom-mcp",
            "reason": "在 Lightroom 恢复目录状态后导出最终成片",
            "affected_region": "global-frame",
            "parameters": {"export_only": True},
            "risk": "low",
            "checkpoint": "after-lightroom-export",
            "generative": False,
            "input_layer": "raw-develop",
            "output_layer": "lightroom-export",
            "required": True,
        }
        operations.append(synthetic)
        result["operation_graph"]["operations"] = deepcopy(operations)
    max_iterations = max(1, min(3, int(graph.get("max_iterations", 3) or 3)))
    runtime_context: dict[str, Any] = {}
    no_op_streak = 0
    stopped_for_no_effect = False
    stopped_for_quality = False
    best_iteration: dict[str, Any] | None = None
    for iteration in range(1, max_iterations + 1):
        iteration_needs_retry = False
        for operation in operations:
            backend = str(operation.get("backend"))
            required_operation = operation.get("required", True) is not False
            executable, gate_reason = _operation_execution_gate(operation)
            command, _ = _command_for_backend(backend) if executable else (None, None)
            if not executable:
                response = {
                    "status": "paused" if required_operation else "skipped",
                    "reason": gate_reason or _pending_reason(operation, str(job.get("processing_locality", "mixed"))),
                    "capability_gate": "blocked" if required_operation else "unsupported",
                }
            elif not command:
                response = {
                    "status": "paused" if required_operation else "skipped",
                    "reason": _pending_reason(operation, str(job.get("processing_locality", "mixed"))),
                    "capability_gate": "blocked" if required_operation else "unsupported",
                }
            else:
                response = _call_json_adapter(command, _operation_request(job, operation, run_id, iteration=iteration, runtime_context=runtime_context))
            response = _normalise_adapter_result(response, operation)
            if response.get("status") != "completed" and not required_operation:
                response["original_status"] = response.get("status")
                response["status"] = "skipped"
                response.setdefault("capability_gate", "unsupported")
            response["iteration"] = iteration
            result["operation_results"].append(response)
            if response.get("status") == "skipped":
                result["downgrades"].append({
                    "operation_id": operation.get("operation_id"),
                    "backend": backend,
                    "reason": response.get("reason", "optional_operation_not_executed"),
                })
            if response.get("status") == "completed" and isinstance(response.get("evidence"), dict):
                returned_document_id = response["evidence"].get("document_id")
                if runtime_context.get("document_id") and not _document_identity_matches(runtime_context["document_id"], returned_document_id):
                    if _verified_document_identity_rollover(
                        runtime_context["document_id"],
                        returned_document_id,
                        response["evidence"],
                        runtime_context,
                    ):
                        response["document_identity_rollover"] = {
                            "from": runtime_context["document_id"],
                            "to": returned_document_id,
                            "path": response["evidence"].get("document_path") or response["evidence"].get("before_path"),
                        }
                    else:
                        response.update({"status": "failed", "reason": "document_identity_mismatch"})
                if response.get("status") != "completed":
                    result["status"] = "failed"
                    result["blockers"].append("document_identity_mismatch")
                    result["stopping_reason"] = "document_identity_mismatch"
                    result["finished_at"] = _now()
                    return _finalize_execution_result(result, "failed")
                runtime_context["last_evidence"] = deepcopy(response["evidence"])
                document_id = response["evidence"].get("document_id")
                if isinstance(document_id, str) and document_id.strip():
                    runtime_context["document_id"] = document_id
                working_path = response["evidence"].get("working_path") or response["evidence"].get("after_path") or response["evidence"].get("export_path")
                if isinstance(working_path, str) and working_path.strip():
                    runtime_context["working_path"] = working_path
            for graph_operation in result["operation_graph"].get("operations", []):
                if graph_operation.get("operation_id") == operation.get("operation_id"):
                    graph_operation["status"] = response.get("status")
                    graph_operation["adapter_result"] = deepcopy(response)
                    break
            checkpoint = {
                "operation_id": operation.get("operation_id"),
                "iteration": iteration,
                "status": response.get("status"),
                "response": response,
                "recorded_at": _now(),
            }
            try:
                update_checkpoint(queue_path, item_id, checkpoint)
            except KeyError:
                pass
            if response.get("status") not in {"completed", "skipped"}:
                result["status"] = "paused" if response.get("status") in {"paused", "unavailable"} else "failed"
                result["blockers"].append(str(response.get("reason", "adapter_not_completed")))
                result["iteration_history"].append({"iteration": iteration, "status": result["status"], "stopping_reason": result["blockers"][-1]})
                try:
                    transition(queue_path, item_id, "paused" if result["status"] == "paused" else "failed", result["blockers"][-1])
                except (KeyError, ValueError):
                    pass
                result["finished_at"] = _now()
                return _finalize_execution_result(result, result["status"])
            quality = response.get("quality") if isinstance(response.get("quality"), dict) else {}
            if quality.get("material_change") is False:
                no_op_streak += 1
            elif quality.get("material_change") is True:
                no_op_streak = 0
            if no_op_streak >= 2:
                stopped_for_no_effect = True
                result["blockers"].append("consecutive_no_effect_operations")
                result["stopping_reason"] = "consecutive_no_effect_operations"
                break
            # Adapters may return a lightweight per-iteration quality envelope.
            # When present, use it to accept, retry or stop rather than trusting
            # a successful tool call as proof of a better photograph.
            if all(key in quality for key in ("score", "technical_pass", "semantic_pass")):
                candidate = {
                    "iteration": iteration,
                    "score": float(quality.get("score", 0.0)),
                    "technical_pass": quality.get("technical_pass") is True,
                    "semantic_pass": quality.get("semantic_pass") is True,
                    "material_change": quality.get("material_change") is not False,
                    "operation_id": operation.get("operation_id"),
                    "checkpoint": operation.get("checkpoint"),
                }
                previous = best_iteration or {
                    "iteration": 0,
                    "score": float(quality.get("baseline_score", job.get("score", {}).get("candidate_potential", 0.0)) or 0.0),
                    "technical_pass": True,
                    "semantic_pass": True,
                    "material_change": True,
                    "checkpoint": "source-read-only",
                }
                loop_decision = evaluate_iteration(previous, candidate, no_op_streak=no_op_streak)
                result["closed_loop_history"].append(loop_decision)
                best_iteration = deepcopy(loop_decision["best"])
                runtime_context["best_checkpoint"] = best_iteration.get("checkpoint")
                if not loop_decision["accepted"]:
                    response["rollback_required"] = True
                    response["rollback_checkpoint"] = best_iteration.get("checkpoint")
                if loop_decision["stop"]:
                    result["quality_stop"] = True
                    result["stopping_reason"] = loop_decision["stopping_reason"]
                    result["blockers"].append(loop_decision["stopping_reason"])
                    stopped_for_no_effect = loop_decision["stopping_reason"] == "consecutive_no_effect_operations"
                    stopped_for_quality = True
                    iteration_needs_retry = False
                    break
            iteration_needs_retry = iteration_needs_retry or response.get("needs_iteration") is True or quality.get("needs_iteration") is True
        if stopped_for_no_effect or stopped_for_quality:
            result["iteration_history"].append({
                "iteration": iteration,
                "status": "completed",
                "needs_iteration": False,
                "stopping_reason": result.get("stopping_reason", "quality_gate_stopped"),
            })
            break
        result["iteration_history"].append({
            "iteration": iteration,
            "status": "completed",
            "needs_iteration": iteration_needs_retry,
            "stopping_reason": "quality_requests_retry" if iteration_needs_retry else "quality_satisfied",
        })
        if not iteration_needs_retry:
            break
    else:
        result["iteration_history"][-1]["stopping_reason"] = "iteration_cap_reached"
        result["blockers"].append("iteration_cap_reached")
    result["status"] = "completed"
    if stopped_for_no_effect:
        result["quality_stop"] = True
    if best_iteration is not None:
        result["best_iteration"] = best_iteration
    try:
        transition(queue_path, item_id, "completed")
    except (KeyError, ValueError):
        pass
    result["finished_at"] = _now()
    graph_status = "completed-with-downgrades" if result["downgrades"] else "completed"
    return _finalize_execution_result(result, graph_status)


def build_execution_jobs(
    scores: list[dict[str, Any]],
    run_id: str,
    output_dir: str,
    processing_locality: str,
    capabilities: dict[str, Any],
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for score in scores:
        if score.get("decision") != "selected":
            continue
        variants = score.get("variant_plans") if isinstance(score.get("variant_plans"), list) else []
        for variant in variants or [{"variant_name": "natural", "adapter_plan": {}}]:
            adapter_plan = variant.get("adapter_plan") if isinstance(variant, dict) else {}
            adapter_plan = adapter_plan if isinstance(adapter_plan, dict) else {}
            stored_graph = variant.get("operation_graph") if isinstance(variant, dict) else None
            graph = deepcopy(stored_graph) if isinstance(stored_graph, dict) and not validate_operation_graph(stored_graph) else build_operation_graph(score, variant, adapter_plan, capabilities)
            executable_plan = materialize_executable_plan(variant, adapter_plan, graph)
            jobs.append({
                "job_id": f"{run_id}:{score.get('photo_id')}:{variant.get('variant_name', 'natural')}",
                "item_id": f"{run_id}:{score.get('photo_id')}:{variant.get('variant_name', 'natural')}",
                "run_id": run_id,
                "photo_id": score.get("photo_id"),
                "asset_group_id": score.get("asset_group_id"),
                "stable_photo_id": score.get("stable_photo_id") or score.get("photo_id"),
                "source_path": score.get("source_path"),
                "preview_path": score.get("preview_path"),
                "variant_name": variant.get("variant_name", "natural"),
                "score": deepcopy(score),
                "edit_plan": executable_plan,
                "adapter_plan": deepcopy(adapter_plan),
                "operation_graph": graph,
                "capabilities": deepcopy(capabilities),
                "processing_locality": processing_locality,
                "output_dir": str(Path(output_dir).expanduser().resolve()),
                "status": "planned",
            })
    return jobs


def _render_target_mean_luma(score: dict[str, Any]) -> tuple[float, str]:
    """Use a scene-aware luminance target for the independent render score."""
    category = str(score.get("primary_category") or "")
    tags = {str(item).casefold() for item in score.get("secondary_tags", []) if item}
    evidence = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    labels = {
        str(item[0]).casefold(): float(item[1])
        for item in (evidence.get("labels") or [])
        if isinstance(item, (list, tuple)) and len(item) >= 2
    }
    if category == "architecture-urban-space" and (
        "night" in tags or labels.get("night sky", 0.0) >= 0.30
    ):
        return 0.26, "night-architecture"
    if "low-light" in tags:
        return 0.30, "low-light"
    return 0.48, "general"


def _quality_for_completed_job(job: dict[str, Any], execution: dict[str, Any], run_manifest: dict[str, Any]) -> dict[str, Any]:
    score = job.get("score") if isinstance(job.get("score"), dict) else {}
    target_mean_luma, scoring_scene = _render_target_mean_luma(score)
    responses = execution.get("operation_results", [])
    evidence = [item.get("evidence", {}) for item in responses if isinstance(item, dict) and isinstance(item.get("evidence"), dict)]
    latest = evidence[-1] if evidence else {}
    before_path = latest.get("before_path") or job.get("source_path")
    after_path = latest.get("after_path") or latest.get("export_path")
    candidate: dict[str, Any] = {
        "candidate_id": execution.get("job_id") or f"{job.get('photo_id')}:{job.get('variant_name')}",
        "iteration": 1,
        "source_path": job.get("source_path"),
        "source_sha256": _sha256(Path(job["source_path"])) if isinstance(job.get("source_path"), str) and Path(job["source_path"]).is_file() else None,
        "source_snapshot_sha256": run_manifest.get("source_snapshot_sha256"),
        "before_path": before_path,
        "after_path": after_path,
        "processing_locality": job.get("processing_locality"),
        "locality_policy": run_manifest.get("locality_policy"),
        "edit_plan": job.get("edit_plan") or execution.get("operation_graph"),
        "operation_manifest": execution.get("operation_graph") or job.get("operation_graph"),
        "warnings": [],
        "run_manifest": deepcopy(run_manifest),
        "requested_label": job.get("variant_name") or run_manifest.get("intent"),
    }
    operation_manifests = [
        deepcopy(item.get("operation_manifest"))
        for item in evidence
        if isinstance(item.get("operation_manifest"), dict)
        and item.get("operation_manifest", {}).get("status") == "valid"
    ]
    if operation_manifests:
        candidate["operation_manifests"] = operation_manifests
    graph = execution.get("operation_graph") if isinstance(execution.get("operation_graph"), dict) else (
        job.get("operation_graph") if isinstance(job.get("operation_graph"), dict) else {}
    )
    photoshop_ids = {
        str(operation.get("operation_id"))
        for operation in graph.get("operations", [])
        if isinstance(operation, dict)
        and operation.get("backend") == "photoshop-fine-edit"
        and isinstance(operation.get("operation_id"), str)
    }
    required_photoshop_ids = {
        str(operation.get("operation_id"))
        for operation in graph.get("operations", [])
        if isinstance(operation, dict)
        and operation.get("backend") == "photoshop-fine-edit"
        and operation.get("required", True) is not False
        and isinstance(operation.get("operation_id"), str)
    }
    completed_ids = {
        str(item.get("operation_id"))
        for item in responses
        if isinstance(item, dict) and item.get("status") == "completed" and isinstance(item.get("operation_id"), str)
    }
    coverage = _operation_summary(graph, responses)
    candidate["operation_coverage"] = coverage
    if photoshop_ids and photoshop_ids.issubset(completed_ids):
        candidate["adapter_status"] = {
            "available": True,
            "fine_edit_mode": True,
            "mode": "fine-edit",
            "verified_operations": sorted(photoshop_ids),
            "verified_required_operations": sorted(required_photoshop_ids),
        }
    elif photoshop_ids:
        candidate["adapter_status"] = {
            "available": bool(photoshop_ids & completed_ids),
            "fine_edit_mode": False,
            "mode": "partial-fine-edit",
            "verified_operations": sorted(photoshop_ids & completed_ids),
            "missing_operations": sorted(photoshop_ids - completed_ids),
        }
        candidate["warnings"].append("photoshop_operation_coverage_incomplete")
    editable_master = next(
        (deepcopy(item.get("editable_master")) for item in evidence if isinstance(item.get("editable_master"), dict)),
        None,
    )
    if editable_master:
        candidate["editable_master"] = editable_master
    plan_for_disclosure = candidate.get("edit_plan") if isinstance(candidate.get("edit_plan"), dict) else {}
    director_brief = plan_for_disclosure.get("director_brief") if isinstance(plan_for_disclosure.get("director_brief"), dict) else {}
    disclosure = director_brief.get("transformation_disclosure")
    if isinstance(disclosure, str) and disclosure.strip():
        candidate["transformation_disclosure"] = disclosure
    semantic_operation_type = None
    graph_operations = graph.get("operations", []) if isinstance(graph, dict) else graph
    if any(
        isinstance(item, dict) and item.get("type") in {"large-crop", "generative-expand"}
        for item in graph_operations
    ):
        semantic_operation_type = "large-crop"
    if before_path and after_path and Path(str(before_path)).is_file() and Path(str(after_path)).is_file():
        semantic = compare_semantics(str(before_path), str(after_path), semantic_operation_type)
    else:
        semantic = {"status": "unavailable", "critical": False, "warnings": ["before_or_after_missing"]}
    candidate["semantic_check"] = semantic
    candidate["warnings"].extend(semantic.get("warnings", []))
    exports: dict[str, str] = {}
    for evidence_item in evidence:
        listed = evidence_item.get("exports")
        if isinstance(listed, dict):
            for profile, path in listed.items():
                if isinstance(path, str) and path.strip():
                    exports[str(profile)] = path
        elif isinstance(listed, list):
            for item in listed:
                if not isinstance(item, dict):
                    continue
                profile = item.get("profile") or item.get("export_profile")
                path = item.get("path") or item.get("export_path")
                if isinstance(profile, str) and isinstance(path, str) and path.strip():
                    exports[profile] = path
        path = evidence_item.get("export_path") or evidence_item.get("path")
        if isinstance(path, str) and path.strip():
            profile = str(evidence_item.get("export_profile") or evidence_item.get("profile") or "competition-quality")
            exports.setdefault(profile, path)

    source_path = str(job.get("source_path"))
    source_checksum = candidate.get("source_sha256")
    export_validations: dict[str, dict[str, Any]] = {}
    requested_profiles = None
    lightroom_plan = job.get("adapter_plan", {}).get("lightroom") if isinstance(job.get("adapter_plan"), dict) else None
    if isinstance(lightroom_plan, dict):
        export_plan = lightroom_plan.get("export")
        if isinstance(export_plan, dict) and isinstance(export_plan.get("profiles"), list):
            requested_profiles = {str(item) for item in export_plan["profiles"] if str(item) in {"web-share", "competition-quality", "print-master"}}
    output_profiles = requested_profiles or {"web-share", "competition-quality"}
    for profile, path_value in exports.items():
        export_path = str(path_value)
        result_checksum = _sha256(Path(export_path)) if Path(export_path).is_file() else None
        evidence_policy = next((item for item in evidence if item.get("export_path") == export_path or item.get("path") == export_path), {})
        editable_master = evidence_policy.get("editable_master") or next(
            (item.get("editable_master") for item in evidence if isinstance(item.get("editable_master"), dict)),
            None,
        )
        export_validations[profile] = validate_export(export_path, source_path, profile, {
            "source_checksum": source_checksum,
            "result_snapshot_path": export_path,
            "result_snapshot_sha256": result_checksum,
            "result_checksum": result_checksum,
            "expected_source_path": source_path,
            "expected_icc_profile": "sRGB" if profile != "print-master" else None,
            "source_is_transformed": False,
            "editable_master": editable_master,
            "require_exif": True,
        })

    export_path = exports.get("competition-quality") or exports.get("web-share") or exports.get("print-master")
    export_validation = export_validations.get("competition-quality") or (
        next(iter(export_validations.values())) if export_validations else None
    )
    candidate["export_validation"] = export_validation
    candidate["export_validations"] = export_validations
    candidate["export_profiles"] = {
        "required": sorted(output_profiles),
        "received": sorted(exports),
        "missing": sorted(output_profiles - set(exports)),
    }
    if candidate["export_profiles"]["missing"]:
        candidate["warnings"].append("missing_export_profiles:" + ",".join(candidate["export_profiles"]["missing"]))

    rendered_before = before_path if before_path and Path(str(before_path)).is_file() else None
    rendered_after = after_path if after_path and Path(str(after_path)).is_file() else export_path
    rendered_quality = (
        evaluate_rendered_candidate(rendered_before, str(rendered_after), target_mean_luma)
        if rendered_after else {"status": "unavailable", "reason": "rendered_output_missing"}
    )
    candidate["scoring_scene"] = scoring_scene
    candidate["post_render_quality"] = rendered_quality
    render_delta = compare_rendered_pixels(rendered_before, str(rendered_after)) if rendered_after else {"status": "unavailable", "reason": "rendered_output_missing"}
    candidate["render_delta"] = render_delta
    material_change = render_delta.get("material_change") is True
    if not material_change:
        candidate["warnings"].append("no_material_pixel_or_geometry_change")
    if rendered_quality.get("status") == "evaluated":
        final_score = float(rendered_quality["final_score"])
        technical_score = float(rendered_quality["technical_score"])
        aesthetic_score = float(rendered_quality["aesthetic_score"])
        improvement_score = float(rendered_quality["improvement_score"])
    else:
        final_score = 0.0
        technical_score = 0.0
        aesthetic_score = 0.0
        improvement_score = 0.0
        candidate["warnings"].append("post_render_quality_unavailable")
    trusted_evaluation = {
        "candidate_id": candidate["candidate_id"],
        "final_score": final_score,
        "score_confidence": min(float(score.get("score_confidence", 0.0)), 0.95) if rendered_quality.get("status") == "evaluated" else 0.0,
        "technical_gates": {
            **(score.get("technical_gates", {}) if isinstance(score.get("technical_gates"), dict) else {}),
            "rendered_pixels": "pass" if rendered_quality.get("status") == "evaluated" else "fail",
            "material_change": "pass" if material_change else "fail",
            "export_profiles": "pass" if not candidate["export_profiles"]["missing"] and all(item.get("valid") is True for item in export_validations.values()) else "fail",
        },
        "technical_score": technical_score,
        "aesthetic_score": aesthetic_score,
        "improvement_score": improvement_score,
        "critical_artifacts": bool(semantic.get("critical")),
    }
    evaluated = evaluate_candidate(run_manifest, candidate, trusted_evaluation)
    # Keep every release-threshold input on the evaluated candidate so the
    # later cross-variant selector cannot accidentally accept a candidate
    # after the first quality evaluation rejected it.
    evaluated.update({
        "technical_score": technical_score,
        "aesthetic_score": aesthetic_score,
        "improvement_score": improvement_score,
        "critical_artifacts": bool(semantic.get("critical")),
    })
    evaluated["quality_status"] = "evaluated"
    evaluated["export_validation"] = export_validation
    evaluated["source_path"] = candidate["source_path"]
    evaluated["photo_id"] = job.get("photo_id")
    evaluated["variant_name"] = job.get("variant_name")
    evaluated["render_delta"] = render_delta
    evaluated["material_change"] = material_change
    return evaluated


def merge_final_scores(scores: list[dict[str, Any]], quality_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Write verified post-render scores back to the user-facing score records."""

    if not isinstance(scores, list) or not isinstance(quality_report, dict):
        return scores
    quality_items = quality_report.get("items") if isinstance(quality_report.get("items"), list) else []
    by_candidate = {
        str(item.get("candidate_id")): item
        for item in quality_items
        if isinstance(item, dict) and item.get("candidate_id") is not None
    }
    selections = quality_report.get("chosen_candidates") if isinstance(quality_report.get("chosen_candidates"), list) else []
    chosen_by_photo = {
        str(item.get("photo_id")): item.get("selection", {}).get("candidate_id")
        for item in selections
        if isinstance(item, dict) and isinstance(item.get("selection"), dict)
    }
    for score in scores:
        if not isinstance(score, dict):
            continue
        candidate_id = chosen_by_photo.get(str(score.get("photo_id")))
        final = by_candidate.get(str(candidate_id)) if candidate_id is not None else None
        if not isinstance(final, dict) or final.get("final_score") is None:
            continue
        score["final_score"] = final.get("final_score")
        score["final_score_source"] = "post-render-pixels"
        score["final_quality_status"] = final.get("quality_status", "evaluated")
        score["final_variant"] = final.get("variant_name")
        score["final_export_profiles"] = final.get("export_profiles")
        if isinstance(score.get("score_record"), dict):
            score["score_record"]["final_score"] = final.get("final_score")
    return scores


def _rehydrate_legacy_editable_master(value: Any) -> dict[str, Any] | None:
    """Verify a legacy bridge master that predates the explicit ``valid`` flag.

    Old runs may have a real final PSD plus proven layer/mask IDs but lack the
    field required by the current release gate.  This migration is deliberately
    narrow: it only adds a positive verdict when the PSD still exists and all
    existing evidence satisfies the same editable-master contract.
    """

    if not isinstance(value, dict):
        return None
    path_value = value.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value).expanduser()
    layer_ids = value.get("layer_ids")
    mask_ids = value.get("mask_ids")
    valid = bool(
        value.get("valid") is not False
        and path.is_file()
        and path.suffix.casefold() == ".psd"
        and str(value.get("format") or "PSD").upper() == "PSD"
        and value.get("editable") is True
        and value.get("layered") is True
        and isinstance(layer_ids, list) and bool(layer_ids)
        and isinstance(mask_ids, list) and bool(mask_ids)
    )
    return {
        **deepcopy(value),
        "path": str(path),
        "valid": valid,
    }


def _reconcile_quality_release_labels(quality_report: dict[str, Any]) -> bool:
    """Refresh release labels from persisted evidence without rerunning Adobe."""

    if not isinstance(quality_report, dict):
        return False
    items = quality_report.get("items")
    if not isinstance(items, list):
        return False
    changed = False
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        master = _rehydrate_legacy_editable_master(item.get("editable_master"))
        if master is not None and master != item.get("editable_master"):
            item["editable_master"] = master
            changed = True
        photo_id = item.get("photo_id")
        if item.get("quality_status") == "evaluated" and photo_id is not None:
            groups.setdefault(str(photo_id), []).append(item)

    if not groups:
        return changed
    prior = quality_report.get("chosen_candidates")
    prior_order = [
        str(entry.get("photo_id"))
        for entry in prior
        if isinstance(entry, dict) and entry.get("photo_id") is not None
    ] if isinstance(prior, list) else []
    photo_order = [photo_id for photo_id in prior_order if photo_id in groups]
    photo_order.extend(photo_id for photo_id in groups if photo_id not in photo_order)
    reconciled: list[dict[str, Any]] = []
    for photo_id in photo_order:
        candidates = groups[photo_id]
        evaluations = [{
            "candidate_id": item.get("candidate_id"),
            "final_score": item.get("final_score"),
            "score_confidence": item.get("score_confidence"),
            "technical_gates": item.get("technical_gates"),
            "technical_score": item.get("technical_score"),
            "aesthetic_score": item.get("aesthetic_score"),
            "improvement_score": item.get("improvement_score"),
            "critical_artifacts": item.get("critical_artifacts"),
        } for item in candidates]
        requested_label = candidates[0].get("requested_label")
        selection = choose_best_candidate(
            candidates,
            evaluations,
            {"requested_label": requested_label} if isinstance(requested_label, str) else None,
        )
        reconciled.append({"photo_id": photo_id, "selection": selection})
    if reconciled != prior:
        quality_report["chosen_candidates"] = reconciled
        changed = True
    return changed


def _materialize_selected_outputs(root: Path, photo_id: str, winner: dict[str, Any], variant_name: str) -> None:
    """Expose canonical deliverables without creating duplicate copies."""

    del root, photo_id, variant_name
    selected_paths: dict[str, str] = {}

    source_after = winner.get("after_path")
    if isinstance(source_after, str) and Path(source_after).is_file():
        selected_paths["preview"] = source_after

    validations = winner.get("export_validations")
    if isinstance(validations, dict):
        for profile, validation in validations.items():
            if not isinstance(validation, dict) or validation.get("valid") is not True:
                continue
            path_value = validation.get("path")
            if not isinstance(path_value, str) or not Path(path_value).is_file():
                continue
            selected_paths[str(profile)] = path_value

    master = winner.get("editable_master")
    if isinstance(master, dict):
        master_path = master.get("path")
        if isinstance(master_path, str) and Path(master_path).is_file():
            selected_paths["editable-master"] = master_path

    if selected_paths:
        winner["selected_output_paths"] = selected_paths
        winner["selected_output_path"] = selected_paths.get("competition-quality") or selected_paths.get("web-share") or selected_paths.get("preview")


def _cleanup_transient_working_renders(root: Path, execution: dict[str, Any]) -> list[str]:
    """Remove only run-local Lightroom TIFF working files after validation."""

    working_root = (root / "working").resolve()
    removed: list[str] = []
    for item in execution.get("operation_results", []) if isinstance(execution, dict) else []:
        evidence = item.get("evidence") if isinstance(item, dict) else None
        if not isinstance(evidence, dict):
            continue
        path_value = evidence.get("working_path")
        if not isinstance(path_value, str):
            continue
        path = Path(path_value).expanduser().resolve()
        try:
            path.relative_to(working_root)
        except ValueError:
            continue
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    for directory in sorted((item for item in working_root.rglob("*") if item.is_dir()), reverse=True) if working_root.is_dir() else []:
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def execute_batch(
    jobs: list[dict[str, Any]],
    queue_path: str,
    output_dir: str,
    run_manifest: dict[str, Any],
    mode: str = "auto",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run jobs, write durable execution/quality/rollback artifacts, and stop safely."""

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_id = str(run_manifest.get("run_id", "photo-run"))
    plans_root = root / "plans"
    for job in jobs:
        stable_id = str(job.get("stable_photo_id") or job.get("photo_id") or "photo")
        variant_name = str(job.get("variant_name", "natural"))
        plan_root = plans_root / stable_id / variant_name
        plan_root.mkdir(parents=True, exist_ok=True)
        (plan_root / "edit-plan.json").write_text(json.dumps(job.get("edit_plan", {}), ensure_ascii=False, indent=2), encoding="utf-8")
        (plan_root / "edit-manifest.json").write_text(json.dumps(job.get("operation_graph", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    executions: list[dict[str, Any]] = []
    if mode == "review":
        executions = [{"job_id": job.get("job_id"), "photo_id": job.get("photo_id"), "variant_name": job.get("variant_name"), "status": "awaiting-user-approval", "blockers": ["review_mode"]} for job in jobs]
    else:
        disk_budget = _disk_budget_bytes(run_manifest)
        budget_paused = False
        for job in jobs:
            disk_usage = _directory_size(root)
            budget_decision = _job_disk_budget_decision(job, disk_usage, disk_budget)
            if budget_paused or not budget_decision["allowed"] or (disk_budget and disk_usage >= disk_budget):
                precise_reason = budget_decision["reason"] if not budget_decision["allowed"] else "disk_budget_exceeded"
                executions.append({
                    "job_id": job.get("job_id"),
                    "photo_id": job.get("photo_id"),
                    "variant_name": job.get("variant_name"),
                    "status": "paused",
                    "blockers": list(dict.fromkeys([precise_reason, "disk_budget_exceeded"])),
                    "disk_usage_bytes": disk_usage,
                    "disk_budget_bytes": disk_budget,
                    "projected_peak_bytes": budget_decision["projected_peak_bytes"],
                    "projected_total_bytes": budget_decision["projected_total_bytes"],
                })
                budget_paused = True
                continue
            execution = execute_job(job, queue_path, run_id, dry_run=dry_run)
            execution["disk_usage_bytes_after_job"] = _directory_size(root)
            execution["disk_budget_bytes"] = disk_budget
            if disk_budget and execution["disk_usage_bytes_after_job"] >= disk_budget:
                budget_paused = True
                execution.setdefault("blockers", []).append("disk_budget_reached_after_job")
            executions.append(execution)
    quality_items: list[dict[str, Any]] = []
    by_photo: dict[str, list[dict[str, Any]]] = {}
    for job, execution in zip(jobs, executions):
        if execution.get("status") == "completed":
            quality = _quality_for_completed_job(job, execution, run_manifest)
            quality_items.append(quality)
            by_photo.setdefault(str(job.get("photo_id")), []).append(quality)
        else:
            quality_items.append({
                "photo_id": job.get("photo_id"),
                "variant_name": job.get("variant_name"),
                "quality_status": "pending",
                "final_score": None,
                "decision": "review",
                "release_blockers": list(execution.get("blockers", [])) + ["adapter_execution_not_verified"],
                "operation_graph_status": job.get("operation_graph", {}).get("status"),
            })
    chosen: list[dict[str, Any]] = []
    for photo_id, candidates in by_photo.items():
        if not candidates:
            continue
        evaluations = [{
            "candidate_id": item.get("candidate_id"),
            "final_score": item.get("final_score"),
            "score_confidence": item.get("score_confidence"),
            "technical_gates": item.get("technical_gates"),
            "technical_score": item.get("technical_score"),
            "aesthetic_score": item.get("aesthetic_score"),
            "improvement_score": item.get("improvement_score"),
            "critical_artifacts": item.get("critical_artifacts"),
        } for item in candidates]
        selection = choose_best_candidate(
            candidates,
            evaluations,
            {"requested_label": run_manifest.get("intent")},
        )
        chosen.append({"photo_id": photo_id, "selection": selection})
        if selection.get("candidate_id"):
            winner = next((item for item in candidates if item.get("candidate_id") == selection.get("candidate_id")), None)
            if isinstance(winner, dict) and selection.get("decision") == "selected":
                _materialize_selected_outputs(root, str(winner.get("photo_id") or photo_id), winner, str(winner.get("variant_name", "natural")))
    transient_removed = []
    for execution in executions:
        # A paused Adobe job must retain its Lightroom TIFF so it can resume
        # from the same Photoshop document/checkpoint.  Cleanup is only safe
        # after that job completed and its quality evaluation was recorded.
        if execution.get("status") == "completed":
            transient_removed.extend(_cleanup_transient_working_renders(root, execution))
    all_execution_completed = bool(executions) and all(item.get("status") == "completed" for item in executions)
    all_quality_evaluated = bool(quality_items) and all(item.get("quality_status") == "evaluated" for item in quality_items)
    quality_report = {
        "run_id": run_id,
        "generated_at": _now(),
        "status": "completed" if all_execution_completed and all_quality_evaluated else (
            "completed-with-quality-gates" if all_execution_completed else "pending-adapter-or-review"
        ),
        "items": quality_items,
        "chosen_candidates": chosen,
        "thresholds": {"technical": 75, "aesthetic": 78, "improvement": 10, "competition_aesthetic": 85, "max_iterations": 3},
        "disclosure": "最终成片分只在适配器返回真实图像、语义检查和导出检查之后计算；等待态不被标记为完成。",
        "release_ready_count": sum(1 for item in quality_items if item.get("decision") == "selected" and item.get("technical_gate_passed") is True),
        "quality_evaluated_count": sum(1 for item in quality_items if item.get("quality_status") == "evaluated"),
        "resource_budget": {
            "disk_budget_bytes": _disk_budget_bytes(run_manifest),
            "disk_usage_bytes": _directory_size(root),
            "disk_budget_policy": "pause-before-starting-next-job",
        },
        "transient_working_files_removed": transient_removed,
    }
    execution_report = {
        "run_id": run_id,
        "generated_at": _now(),
        "mode": mode,
        "dry_run": dry_run,
        "jobs": jobs,
        "executions": executions,
        "queue_path": str(Path(queue_path).expanduser().resolve()),
    }
    for job, execution in zip(jobs, executions):
        stable_id = str(job.get("stable_photo_id") or job.get("photo_id") or "photo")
        variant_name = str(job.get("variant_name", "natural"))
        plan_path = plans_root / stable_id / variant_name / "edit-plan.json"
        lightroom_runtime: dict[str, Any] = {}
        lr_result = next((
            item for item in execution.get("operation_results", [])
            if isinstance(item, dict) and item.get("backend") == "lightroom-mcp"
        ), None)
        checkpoint_path = lr_result.get("checkpoint", {}).get("path") if isinstance(lr_result, dict) and isinstance(lr_result.get("checkpoint"), dict) else None
        if isinstance(checkpoint_path, str) and Path(checkpoint_path).is_file():
            lightroom_runtime = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        audited_plan = attach_execution_audit(job.get("edit_plan", {}), execution, lightroom_runtime)
        plan_path.write_text(json.dumps(audited_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    rollback = {
        "run_id": run_id,
        "source_read_only": True,
        "best_verified_checkpoints": [
            {"photo_id": item.get("photo_id"), "candidate_id": item.get("selection", {}).get("candidate_id"), "rollback_to": item.get("selection", {}).get("rollback_to")}
            for item in chosen
        ],
        "unverified_jobs_are_not_released": True,
    }
    (root / "execution-results.json").write_text(json.dumps(execution_report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "quality-report.json").write_text(json.dumps(quality_report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "rollback-ledger.json").write_text(json.dumps(rollback, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"execution": execution_report, "quality": quality_report, "rollback": rollback}


def continue_from_execution_plan(plan_path: str, mode: str = "auto", dry_run: bool = False) -> dict[str, Any]:
    """Resume paused jobs after Adobe adapter commands become available."""

    plan_file = Path(plan_path).expanduser().resolve()
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    manifest_value = plan.get("run_manifest")
    if not isinstance(manifest_value, str):
        raise ValueError("execution plan is missing run_manifest")
    manifest_path = Path(manifest_value).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    jobs = plan.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("execution plan is missing jobs")
    queue_path = plan_file.parent / "queue.sqlite3"
    result = execute_batch(jobs, str(queue_path), str(plan_file.parent), manifest, mode=mode, dry_run=dry_run)
    _sync_execution_plan(plan_file, result.get("execution", {}))
    _sync_top_level_report(plan_file, jobs, result, str(queue_path))
    return result


def _sync_execution_plan(plan_file: Path, execution_report: dict[str, Any]) -> dict[str, Any] | None:
    """Persist real execution stages into the resumable execution plan."""

    if not plan_file.is_file():
        return None
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    executions = execution_report.get("executions") if isinstance(execution_report, dict) else None
    executions = executions if isinstance(executions, list) else []

    def find_execution(item: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(item.get("item_id") or item.get("job_id") or "")
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            if item_id and str(execution.get("item_id") or execution.get("job_id") or "") == item_id:
                return execution
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            if (
                str(execution.get("photo_id")) == str(item.get("photo_id"))
                and str(execution.get("variant_name")) == str(item.get("variant_name"))
            ):
                return execution
        return None

    status_to_stage = {
        "completed": "completed",
        "awaiting-user-approval": "awaiting-user-approval",
        "paused": "adapter-retry",
        "failed": "adapter-retry",
    }
    items = plan.get("items") if isinstance(plan.get("items"), list) else []
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        execution = find_execution(item)
        if execution is None:
            continue
        status = str(execution.get("status") or "unknown")
        item["execution_status"] = status
        item["stage"] = status_to_stage.get(status, status)
        if execution.get("blockers"):
            item["blockers"] = deepcopy(execution["blockers"])
        elif "blockers" in item:
            item.pop("blockers", None)
        counts[status] = counts.get(status, 0) + 1

    statuses = [
        str(execution.get("status") or "unknown")
        for execution in executions
        if isinstance(execution, dict)
    ]
    derived_status = str(execution_report.get("status") or "")
    if not derived_status:
        if statuses and all(status == "completed" for status in statuses):
            derived_status = "completed"
        elif any(status in {"paused", "failed"} for status in statuses):
            derived_status = "paused"
        elif statuses:
            derived_status = "in-progress"
        else:
            derived_status = "unknown"
    plan["execution"] = {
        "status": derived_status,
        "updated_at": _now(),
        "counts": counts,
        "results_path": str(plan_file.parent / "execution-results.json"),
    }
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def _sync_top_level_report(plan_file: Path, jobs: list[dict[str, Any]], result: dict[str, Any], queue_path: str) -> dict[str, Any] | None:
    """Keep report.json truthful after a resumable execution command."""

    report_path = plan_file.parent / "report.json"
    if not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    scores = report.get("scores") if isinstance(report.get("scores"), list) else []
    merge_final_scores(scores, result.get("quality", {}))
    pending: list[dict[str, Any]] = []
    for job in jobs:
        item_id = str(job.get("item_id") or job.get("job_id") or "")
        queue_item = get_item(queue_path, item_id) if item_id else None
        if not isinstance(queue_item, dict) or queue_item.get("state") == "completed":
            continue
        pending.append({
            "item_id": item_id,
            "photo_id": job.get("photo_id"),
            "variant_name": job.get("variant_name"),
            "stage": "adapter-retry",
            "state": queue_item.get("state"),
            "reason": queue_item.get("error"),
        })
    report["scores"] = scores
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    quality_items = quality.get("items") if isinstance(quality.get("items"), list) else []
    release_ready_count = int(quality.get("release_ready_count") or 0)
    release_status = (
        "ready"
        if quality_items and release_ready_count == len(quality_items)
        else "rejected"
        if quality_items and all(item.get("quality_status") == "evaluated" for item in quality_items if isinstance(item, dict))
        else "pending"
    )
    report["execution"] = {
        "results": str(plan_file.parent / "execution-results.json"),
        "job_count": len(jobs),
        "status": quality.get("status"),
        "release_status": release_status,
        "release_ready_count": release_ready_count,
    }
    quality_status = str(quality.get("status") or "pending")
    report["status"] = (
        "completed"
        if quality_status == "completed" and release_status == "ready" and not pending
        else "completed-with-quality-gates"
        if quality_status in {"completed", "completed-with-quality-gates"} and not pending
        else "pending-adapter-or-review"
    )
    report["quality_report"] = str(plan_file.parent / "quality-report.json")
    report["rollback_ledger"] = str(plan_file.parent / "rollback-ledger.json")
    report["queue"] = {"path": str(Path(queue_path).expanduser().resolve()), "pending_adapter_work": pending}
    existing_downgrades = report.get("downgrades") if isinstance(report.get("downgrades"), list) else []
    report["downgrades"] = [
        item for item in existing_downgrades
        if item not in {"lightroom_mcp_execution_requires_host_tool_call", "photoshop_capability_requires_host_tool_call", "local_adobe_adapter_pending"}
    ]
    if pending:
        report["downgrades"].insert(0, "local_adobe_adapter_pending")
    report["next_step"] = (
        "本次已完成 Lightroom RAW 解码、Photoshop 精修、PSD/JPG 导出和质量门验证，成片已达到发布门槛；请从 quality-report.json 或 selected/ 查看结果。"
        if release_status == "ready" and not pending
        else "本次已完成处理和质量评估，但质量门禁拒绝发布；请查看 quality-report.json 的 failure_reasons，原始 RAW 未被修改。"
        if release_status == "rejected" and not pending
        else "自动模式会继续执行已配置的本地适配器；等待态任务会从 execution-results.json 的检查点恢复。"
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="恢复照片后期适配器任务并写入质量报告")
    parser.add_argument("--plan", required=True, help="运行目录中的 execution-plan.json")
    parser.add_argument("--mode", choices=("auto", "review"), default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync-report", action="store_true", help="仅用已有 execution/quality artifacts 同步顶层 report.json")
    args = parser.parse_args()
    plan_path = Path(args.plan).expanduser().resolve()
    if args.sync_report:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        quality_path = plan_path.parent / "quality-report.json"
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        if _reconcile_quality_release_labels(quality):
            quality_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {
            "execution": json.loads((plan_path.parent / "execution-results.json").read_text(encoding="utf-8")),
            "quality": quality,
        }
        _sync_execution_plan(plan_path, result["execution"])
        _sync_top_level_report(plan_path, plan.get("jobs", []), result, str(plan_path.parent / "queue.sqlite3"))
        print(json.dumps({"status": "report-synced", "report": str(plan_path.parent / "report.json")}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(continue_from_execution_plan(args.plan, mode=args.mode, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
