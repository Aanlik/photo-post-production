#!/usr/bin/env python3
"""Real local Photoshop MCP adapter for the photo-post-production runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from action_descriptor_registry import get_descriptor
from image_metrics import compare_rendered_pixels, evaluate_rendered_candidate
from semantic_checks import compare_semantics


DEFAULT_CLI = shutil.which("dcc-mcp-cli") or "dcc-mcp-cli"


class AdapterError(RuntimeError):
    pass


def _final_quality_evidence(before_path: str, after_path: str) -> dict[str, Any]:
    """Return independent pixel/semantic evidence for the closed-loop host."""

    rendered = evaluate_rendered_candidate(before_path, after_path)
    delta = compare_rendered_pixels(before_path, after_path)
    semantic = compare_semantics(before_path, after_path)
    evaluated = rendered.get("status") == "evaluated"
    material_change = delta.get("material_change") is True
    technical_score = float(rendered.get("technical_score", 0.0)) if evaluated else 0.0
    semantic_pass = semantic.get("critical") is not True
    return {
        "score": float(rendered.get("final_score", 0.0)) if evaluated else 0.0,
        "technical_pass": bool(evaluated and technical_score >= 55.0),
        "semantic_pass": semantic_pass,
        "material_change": material_change,
        "needs_iteration": False,
        "source": "photoshop-final-render-independent-check",
        "rendered_quality": rendered,
        "render_delta": delta,
        "semantic_check": semantic,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(value: Any) -> str:
    text = str(value or "photo").strip()
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in text)[:100] or "photo"


def _bridge_safe_operation_id(value: Any) -> str:
    """Match the Photoshop fine-edit skill's on-disk operation-id encoding."""

    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return text[:120]


def _canonical_manifest_hash(entry: dict[str, Any]) -> str:
    encoded = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _matching_manifest_evidence(
    manifest: Any,
    expected_operation_id: str,
    *,
    source: str,
    artifact_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return only evidence that belongs to the requested operation.

    The Photoshop host retains idempotency results in memory.  When a caller
    crashes after the host has executed an operation but before the skill
    wrapper returns its durable provenance, a replay contains a provisional
    manifest.  It must never be satisfied by the previous operation's
    manifest; recover the exact graph node from the document manifest instead.
    """

    if not isinstance(manifest, dict):
        return None
    operations = manifest.get("operations")
    if not isinstance(operations, list):
        return None
    entry = next(
        (
            item
            for item in reversed(operations)
            if isinstance(item, dict) and item.get("operation_id") == expected_operation_id
        ),
        None,
    )
    if not isinstance(entry, dict):
        return None
    evidence: dict[str, Any] = {
        "status": "valid",
        "operation_id": expected_operation_id,
        "manifest_hash": _canonical_manifest_hash(entry),
        "entry": entry,
        "recovered_from": source,
    }
    document_manifest_hash = manifest.get("manifest_hash")
    if isinstance(document_manifest_hash, str) and document_manifest_hash:
        evidence["document_manifest_hash"] = document_manifest_hash
    if artifact_path is not None and artifact_path.is_file():
        evidence["path"] = str(artifact_path)
        evidence["sha256"] = _sha256(artifact_path)
    return evidence


def _replay_manifest_artifact(expected_operation_id: str) -> dict[str, Any] | None:
    """Load the durable per-operation manifest written before a caller crash."""

    safe_id = _bridge_safe_operation_id(expected_operation_id)
    if not safe_id:
        return None
    root = (
        Path.home()
        / "Library"
        / "Application Support"
        / "PhotoPostProduction"
        / "ps-operation-runs"
        / safe_id
    )
    path = root / f"{safe_id}-operation-manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _matching_manifest_evidence(
        manifest,
        expected_operation_id,
        source="photoshop_fine_edit.persisted_operation_manifest",
        artifact_path=path,
    )


def _result_manifest_matches(manifest: Any, expected_operation_id: str) -> bool:
    if not isinstance(manifest, dict) or manifest.get("status") != "valid":
        return False
    if manifest.get("operation_id") != expected_operation_id:
        return False
    entry = manifest.get("entry")
    return not isinstance(entry, dict) or entry.get("operation_id") == expected_operation_id


def _crop_geometry_applied(result: Any) -> bool:
    """Recognize a crop that changed the live canvas but missed its export.

    A geometry operation is destructive to the current document canvas.  It
    must never be retried merely because Photoshop failed to return an export
    envelope after already applying the crop; retrying would crop the
    once-cropped canvas a second time.
    """

    if not isinstance(result, dict):
        return False
    geometry = result.get("geometry_evidence") or result.get("geometry")
    if isinstance(geometry, dict):
        bounds = geometry.get("crop_bounds")
        if isinstance(bounds, dict) and all(key in bounds for key in ("left", "top", "right", "bottom")):
            return True
        if geometry.get("type") == "crop":
            return True
    manifest = result.get("operation_manifest")
    if isinstance(manifest, dict):
        entry = manifest.get("entry")
        if isinstance(entry, dict) and entry.get("type") == "large-crop":
            return True
    return False


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("structuredContent"), (dict, list)):
        return value["structuredContent"]
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        texts = [item.get("text") for item in value["content"] if isinstance(item, dict) and isinstance(item.get("text"), str)]
        for text in reversed(texts):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        if texts:
            raise AdapterError("photoshop_tool_text_error:" + texts[-1][:800])
    return value


def _instance() -> tuple[str, str]:
    cli = os.environ.get("PHOTO_PHOTOSHOP_DCC_CLI", DEFAULT_CLI)
    completed = subprocess.run([cli, "--output", "json", "list"], capture_output=True, text=True, timeout=20, check=False)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AdapterError("photoshop_instance_list_invalid_json") from error
    for item in payload.get("instances", []) if isinstance(payload, dict) else []:
        if item.get("dcc_type") == "photoshop" and item.get("status") in {"available", "ready"}:
            instance_id = str(item.get("instance_id") or "")
            short = str(item.get("instance_short") or instance_id[:8])
            if short:
                return short, instance_id
    raise AdapterError("photoshop_live_instance_not_found")


def _call(tool: str, arguments: dict[str, Any], session_id: str) -> Any:
    cli = os.environ.get("PHOTO_PHOTOSHOP_DCC_CLI", DEFAULT_CLI)
    short, _instance_id = _instance()
    slug = f"photoshop.{short}.{tool}"
    command = [
        cli, "--output", "json", "--non-interactive", "--timeout-secs", os.environ.get("PHOTO_PHOTOSHOP_TIMEOUT", "300"),
        "call", slug, "--json", json.dumps(arguments, ensure_ascii=False),
        "--meta-json", json.dumps({"agent_context": {"session_id": session_id}}, ensure_ascii=False),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=max(30, int(os.environ.get("PHOTO_PHOTOSHOP_TIMEOUT", "300")) + 10), check=False)
    try:
        outer = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AdapterError(f"photoshop_call_invalid_json:{tool}:{completed.stderr[-500:]}") from error
    if isinstance(outer, dict) and outer.get("isError") is True:
        raise AdapterError(f"photoshop_tool_error:{tool}:{json.dumps(outer, ensure_ascii=False)[:800]}")
    result = _decode(outer.get("result", outer) if isinstance(outer, dict) else outer)
    # dcc-mcp wraps skill output in action_result(...).  Promote its typed
    # context so the orchestration layer can validate evidence directly.
    if isinstance(result, dict) and isinstance(result.get("context"), dict):
        context = result["context"]
        result = {**result, **context}
    if isinstance(result, dict) and result.get("isError") is True:
        raise AdapterError(f"photoshop_tool_error:{tool}:{json.dumps(result, ensure_ascii=False)[:800]}")
    if completed.returncode != 0 and not isinstance(result, dict):
        raise AdapterError(f"photoshop_call_failed:{tool}:{completed.stderr[-500:]}")
    return result


def _session_file(request: dict[str, Any]) -> Path:
    root = Path(str(request.get("output_dir") or Path.home() / "Library/Application Support/PhotoPostProduction")).expanduser().resolve()
    root.joinpath("adobe-runtime").mkdir(parents=True, exist_ok=True)
    return root / "adobe-runtime" / f"ps-{_safe(request.get('run_id'))}-{_safe(request.get('photo_id'))}.json"


def _document_is_live(state: dict[str, Any], working_path: str, session_id: str) -> bool:
    """Reject a persisted Photoshop document ID after the host closed it.

    Document IDs are session-scoped. A durable adapter state file may outlive
    the Photoshop document because the app was restarted, unlocked, or closed
    manually, so ``opened=true`` alone is never sufficient for recovery.
    """

    expected_id = str(state.get("document_id") or "").strip()
    if not expected_id:
        return False
    try:
        listing = _call("photoshop_document__list_documents", {}, session_id)
    except AdapterError:
        return False
    documents = listing.get("documents") if isinstance(listing, dict) else None
    if not isinstance(documents, list):
        context = listing.get("context") if isinstance(listing, dict) else None
        documents = context.get("documents") if isinstance(context, dict) else None
    if not isinstance(documents, list):
        return False
    expected_path = str(Path(working_path).expanduser().resolve())
    for document in documents:
        if not isinstance(document, dict):
            continue
        document_id = str(document.get("id") or document.get("document_id") or "").strip()
        document_path = document.get("path")
        if document_id != expected_id:
            continue
        if not isinstance(document_path, str) or not document_path.strip():
            return True
        return str(Path(document_path).expanduser().resolve()) == expected_path
    return False


def _path_for(request: dict[str, Any], operation_id: str, suffix: str) -> Path:
    root = Path(str(request.get("output_dir") or Path.cwd())).expanduser().resolve()
    return root / "variants" / _safe(request.get("photo_id")) / _safe(request.get("variant_name")) / "photoshop" / f"{_safe(operation_id)}-{suffix}"


def _session_artifact_path(request: dict[str, Any], suffix: str) -> Path:
    """Return one stable artifact path for the whole Photoshop document session."""
    stem = _safe(Path(str(request.get("source_path") or request.get("photo_id") or "photo")).stem)
    names = {"master.psd": f"{stem}.psd", "preview.jpg": f"{stem}.jpg"}
    return _photoshop_output_dir(request) / names.get(suffix, suffix)


def _history_snapshot_name(request: dict[str, Any], operation: dict[str, Any]) -> str:
    checkpoint = operation.get("checkpoint") or operation.get("operation_id") or "operation"
    return f"photo-post:{_safe(request.get('photo_id'))}:{_safe(checkpoint)}"


def _photoshop_output_dir(request: dict[str, Any]) -> Path:
    root = Path(str(request.get("output_dir") or Path.cwd())).expanduser().resolve()
    return root / "final" / _safe(request.get("photo_id"))


def _is_final_operation(request: dict[str, Any], operation: dict[str, Any]) -> bool:
    """Only the graph's final required operation may persist the PSD master."""

    if request.get("is_final_operation") is True:
        return True
    # Keep direct adapter calls safe when the orchestration envelope predates
    # the explicit flag. The normal graph always ends with apply_crop.
    return str(operation.get("adapter_operation") or "") == "apply_crop"


def _requested_tools(request: dict[str, Any]) -> list[str]:
    """Derive the read-only Photoshop preflight tool list from the plan."""

    plan = request.get("adapter_plan") if isinstance(request.get("adapter_plan"), dict) else {}
    photoshop = plan.get("photoshop") if isinstance(plan.get("photoshop"), dict) else {}
    operations = photoshop.get("operations") if isinstance(photoshop.get("operations"), list) else []
    aliases = {"sharpening": "sharpen", "eraser_mask": "eraser"}
    tools: list[str] = []
    for item in operations:
        if not isinstance(item, dict) or item.get("required") is not True:
            continue
        tool = aliases.get(str(item.get("tool") or ""), str(item.get("tool") or ""))
        if tool and tool not in tools:
            tools.append(tool)
    return tools or ["apply_region_operation", "apply_crop"]


def _cleanup_ephemeral_outputs(
    request: dict[str, Any],
    retain_psd: Path | None = None,
    retain_manifest: Path | None = None,
    remove_previous_preview: str | None = None,
) -> None:
    """Remove legacy per-operation files while keeping one session document.

    The Photoshop bridge may materialize both ``master.psd`` and
    ``preview.psd`` for every operation. Those are not needed when all
    operations share one document and Photoshop History handles in-session
    rollback. Keep only the stable session master supplied by the caller.
    """

    output_dir = _photoshop_output_dir(request)
    output_dir.mkdir(parents=True, exist_ok=True)
    retained = retain_psd.expanduser().resolve() if retain_psd else None
    retained_manifest = retain_manifest.expanduser().resolve() if retain_manifest else None
    transient_manifest_suffixes = (
        "-operation-manifest.json",
        "-layer-spec.json",
        "-tool-spec.json",
        "-mask-spec.json",
        "-crop-spec.json",
    )
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        resolved = path.resolve()
        remove = False
        if path.suffix.casefold() in {".psd", ".psb"}:
            remove = retained is None or resolved != retained
        elif path.name.casefold().endswith(transient_manifest_suffixes):
            remove = retained_manifest is None or resolved != retained_manifest
        if remove:
            try:
                path.unlink()
            except OSError:
                pass
    if remove_previous_preview:
        previous = Path(remove_previous_preview).expanduser()
        try:
            previous_resolved = previous.resolve()
            if previous_resolved.parent == output_dir.resolve() and previous_resolved.suffix.casefold() in {".jpg", ".jpeg"}:
                previous_resolved.unlink(missing_ok=True)
        except OSError:
            pass


def _operation_arguments(
    request: dict[str, Any],
    operation: dict[str, Any],
    before: str | None,
    document_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    operation_id = str(operation.get("operation_id") or request.get("idempotency_key") or "ps-operation")
    final_operation = _is_final_operation(request, operation)
    # The Photoshop skill persists idempotency records in the host process. A
    # graph operation ID is intentionally stable across retries, but it must
    # not replay an old output from a different run/output directory.
    tool = str(operation.get("adapter_operation") or "region_mask_operation")
    params = operation.get("parameters") if isinstance(operation.get("parameters"), dict) else {}
    # The Photoshop bridge caches operation results by operation_id for the
    # lifetime of the host process. Include both run and variant so parallel
    # style variants never replay one another's layer operation.
    effective_operation_id = (
        f"{operation_id}:run-{_safe(request.get('run_id'))}"
        f":variant-{_safe(request.get('variant_name'))}"
    )
    recovery_nonce = params.get("_adapter_recovery_nonce")
    if recovery_nonce:
        effective_operation_id += f":recovery-{_safe(recovery_nonce)}"
    base = {
        "operation_id": effective_operation_id,
        "document_id": document_id or request.get("document_id"),
        # Photoshop's native History is the checkpoint for intermediate
        # operations.  Only the final graph node may write the one PSD and
        # one JPG; storing a full PSD before every layer/tool operation is
        # both slow and catastrophically wasteful on RAW-sized documents.
        "before_path": before,
        "after_path": str(_session_artifact_path(request, "master.psd")) if final_operation else None,
        "export_path": str(_session_artifact_path(request, "preview.jpg")) if final_operation else None,
        "jpeg_quality": 12,
        "audit_dir": str(_photoshop_output_dir(request) / "audit"),
        # Keep this explicit even though the bridge defaults to false, so only
        # the final graph node can materialize the canonical PSD/JPEG pair.
        "persist_final": final_operation,
        "history_snapshot_name": _history_snapshot_name(request, operation),
    }
    execution_route = operation.get("execution_route") if isinstance(operation.get("execution_route"), dict) else {}
    descriptor_id = params.get("descriptor_id") or execution_route.get("descriptor_id")
    if execution_route.get("tier") == "descriptor-verified":
        registry_path = os.environ.get(
            "PHOTO_PHOTOSHOP_DESCRIPTOR_DB",
            str(Path.home() / "Library/Application Support/PhotoPostProduction/action-descriptors.sqlite3"),
        )
        descriptor = get_descriptor(registry_path, str(descriptor_id or ""))
        if descriptor is None:
            raise AdapterError("photoshop_verified_descriptor_not_found")
        adapter_tool = str(operation.get("adapter_operation") or "recorded_action")
        adapter_tool = {"sharpening": "sharpen", "eraser_mask": "eraser"}.get(adapter_tool, adapter_tool)
        return "photoshop_fine_edit__apply_tool_operation", {
            **base,
            "tool": adapter_tool,
            "action_descriptors": descriptor["descriptor"] if isinstance(descriptor["descriptor"], list) else [descriptor["descriptor"]],
            "parameters": params,
        }
    if tool == "layer_mask":
        return "photoshop_fine_edit__apply_layer_operation", {
            **base,
            "layer_operation": "mask",
            "layer_id": request.get("base_layer_id") or request.get("layer_id"),
            "name": "AI-layer-mask",
            "reveal_selection": True,
            "allow_destructive": False,
        }
    if tool in {"region_mask_operation", "selective_color", "curves_local"}:
        operation_type = "hue_saturation" if tool == "selective_color" else "levels" if tool == "curves_local" else "brightness_contrast"
        return "photoshop_fine_edit__apply_region_operation", {
            **base,
            "region": operation.get("affected_region", "subject-review"),
            "input_layer_id": request.get("layer_id"),
            "parameters": params,
            "risk": operation.get("risk", "medium"),
            "operation_type": params.get("operation_type", operation_type),
            "mask_kind": params.get("mask_kind", "all"),
            "mask_bounds": params.get("mask_bounds"),
            "mask_points": params.get("mask_points"),
            "mask_units": params.get("mask_units", "normalized"),
            "feather": params.get("feather", 0),
            "output_layer_name": params.get("output_layer_name"),
            "generative": False,
        }
    if tool == "apply_crop":
        crop_bounds = params.get("crop_bounds") if isinstance(params.get("crop_bounds"), dict) else {"left": 0, "top": 0, "right": 1, "bottom": 1}
        return "photoshop_fine_edit__apply_crop", {
            **base,
            "region": operation.get("affected_region", "composition"),
            "risk": operation.get("risk", "high"),
            "crop_bounds": crop_bounds,
            "crop_units": params.get("crop_units", "normalized"),
        }
    if tool in {"layer_operation", "smart_object"}:
        layer_operation = params.get("layer_operation") or ("create_group" if tool == "smart_object" else "duplicate")
        return "photoshop_fine_edit__apply_layer_operation", {
            **base,
            "layer_operation": layer_operation,
            "layer_id": request.get("layer_id"),
            "name": params.get("name") or f"AI-{tool}",
            "reveal_selection": True,
            "allow_destructive": False,
        }
    aliases = {
        "sharpening": "sharpen",
        "dodge_burn": "dodge_burn",
        "noise_reduction": "noise_reduction",
        "portrait_beauty": "portrait_beauty",
        "eraser_mask": "eraser",
        "content_aware_remove": "content_aware_remove",
    }
    normalized_tool = aliases.get(tool, tool)
    return "photoshop_fine_edit__apply_tool_operation", {
        **base,
        "tool": normalized_tool,
        "input_layer_id": request.get("layer_id"),
        "region": operation.get("affected_region", "subject-review"),
        "parameters": params,
        "risk": operation.get("risk", "high"),
        "mask_kind": params.get("mask_kind", "all"),
        "mask_bounds": params.get("mask_bounds"),
        "mask_points": params.get("mask_points"),
        "mask_units": params.get("mask_units", "normalized"),
        "feather": params.get("feather", 0),
        "output_layer_name": params.get("output_layer_name"),
        "generative": False,
    }


def _profile_exports(jpeg: Path, master: dict[str, Any] | None, request: dict[str, Any], operation_id: str) -> tuple[dict[str, str], dict[str, Any] | None]:
    """The canonical final JPEG is the sole delivery copy for one PSD run."""

    del master, request, operation_id
    return {"competition-quality": str(jpeg)}, None


def _validated_editable_master_evidence(value: Any) -> dict[str, Any]:
    """Normalize and verify the one retained PSD master for release gating.

    Photoshop's save response is authoritative for layer/mask identities, but
    some successful bridge responses omit the convenience ``valid`` flag.
    The quality gate must receive an explicit verification result rather than
    treating an omitted field as a failed master.
    """

    if not isinstance(value, dict):
        return {}
    path_value = value.get("path") or value.get("master_path")
    path = Path(str(path_value)).expanduser() if isinstance(path_value, str) and path_value.strip() else None
    layer_ids = [str(item) for item in value.get("layer_ids", [])] if isinstance(value.get("layer_ids"), list) else []
    mask_ids = [str(item) for item in value.get("mask_ids", [])] if isinstance(value.get("mask_ids"), list) else []
    editable = value.get("editable") is not False
    layered = value.get("layered") is not False
    format_name = str(value.get("format") or "PSD").upper()
    valid = bool(
        value.get("valid") is not False
        and path is not None
        and path.is_file()
        and format_name == "PSD"
        and editable
        and layered
        and layer_ids
        and mask_ids
    )
    return {
        **value,
        "path": str(path) if path is not None else None,
        "sha256": value.get("sha256") or value.get("master_sha256") or (_sha256(path) if path is not None and path.is_file() else None),
        "format": format_name,
        "editable": editable,
        "layered": layered,
        "layer_ids": layer_ids,
        "mask_ids": mask_ids,
        "valid": valid,
    }


def execute(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation") if isinstance(request.get("operation"), dict) else {}
    operation_id = str(operation.get("operation_id") or request.get("idempotency_key") or "ps-operation")
    session_file = _session_file(request)
    session_id = f"{request.get('run_id')}:{request.get('photo_id')}:{request.get('variant_name')}"
    state: dict[str, Any] = {}
    if session_file.is_file():
        try:
            state = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    try:
        final_operation = _is_final_operation(request, operation)
        previous_preview = request.get("previous_evidence", {}).get("after_path") if isinstance(request.get("previous_evidence"), dict) else None
        working_path = request.get("working_path") or request.get("source_path")
        document_identity_reconciled_from: str | None = None
        if not state.get("opened") and (not working_path or not Path(str(working_path)).is_file()):
            raise AdapterError("photoshop_working_input_missing")
        if state.get("opened") and working_path and not _document_is_live(state, str(working_path), session_id):
            # A persisted document ID is invalid after Photoshop closes or
            # recreates the document. Re-open the same handoff file so the
            # next graph node receives a live document ID.
            previous_document_id = str(state.get("document_id") or "").strip()
            if previous_document_id:
                document_identity_reconciled_from = previous_document_id
            state = {
                "working_path": str(working_path),
                "opened": False,
                "document_id": None,
                "base_layer_id": None,
                "pixel_layer_id": None,
                "layer_id": None,
                "last_master": {},
                "last_operation_manifest": None,
            }
        if not state.get("opened"):
            toolchain = _call(
                "photoshop_fine_edit__plan_toolchain",
                {
                    "subject_type": str(request.get("score", {}).get("primary_category", "general")) if isinstance(request.get("score"), dict) else "general",
                    "target": "真实裁切、明确色彩层次和局部人物精修；所有修改保留在一个可编辑 PSD 中",
                    "intent": str(request.get("variant_name") or "competition-standard"),
                    "creative_intensity": 65,
                    "source_fidelity": 80,
                    "requested_tools": _requested_tools(request),
                },
                session_id,
            )
            opened = _call("photoshop_fine_edit__open_document", {"path": str(Path(str(working_path)).expanduser().resolve())}, session_id)
            state.update({"opened": True, "document_id": opened.get("document_id") if isinstance(opened, dict) else None, "working_path": str(working_path), "toolchain_plan": toolchain})
            session_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        document_id = str(state.get("document_id") or request.get("document_id") or "") or None
        request = {
            **request,
            "document_id": document_id,
            # Keep organization/adjustment layers out of the raster-tool
            # chain.  A create_group operation may become Photoshop's active
            # layer, but portrait beauty, sharpening, noise reduction and
            # similar tools must continue from the latest pixel-producing
            # layer instead of targeting that group.
            "layer_id": state.get("pixel_layer_id") or state.get("base_layer_id") or state.get("layer_id"),
            "base_layer_id": state.get("base_layer_id"),
        }
        initial_working_path = str(state.get("working_path") or working_path)
        tool_name, arguments = _operation_arguments(
            request,
            operation,
            str(request.get("previous_evidence", {}).get("after_path") or initial_working_path) if isinstance(request.get("previous_evidence"), dict) else initial_working_path,
            document_id=document_id,
        )
        result = _call(tool_name, arguments, session_id)
        if not isinstance(result, dict):
            raise AdapterError("photoshop_operation_result_malformed")
        expected_bridge_operation_id = str(arguments.get("operation_id") or "")
        operation_manifest = result.get("operation_manifest")
        if not _result_manifest_matches(operation_manifest, expected_bridge_operation_id):
            recovered_manifest = None
            try:
                document_manifest = _call(
                    "photoshop_fine_edit__get_operation_manifest",
                    {"document_id": document_id},
                    session_id,
                )
            except AdapterError:
                document_manifest = None
            recovered_manifest = _matching_manifest_evidence(
                document_manifest,
                expected_bridge_operation_id,
                source="photoshop_fine_edit.get_operation_manifest",
            )
            if recovered_manifest is None:
                recovered_manifest = _replay_manifest_artifact(expected_bridge_operation_id)
            if recovered_manifest is not None:
                result = {**result, "operation_manifest": recovered_manifest}
        export_evidence = result.get("export_evidence") if isinstance(result.get("export_evidence"), dict) else {}
        master_evidence = result.get("master_evidence") if isinstance(result.get("master_evidence"), dict) else {}
        jpeg = Path(str(export_evidence.get("path") or arguments.get("export_path"))).expanduser()
        exported_now = export_evidence.get("status") == "completed" and jpeg.is_file()
        if not final_operation and not exported_now:
            # History-mode operations are allowed to return no rendered file.
            # The open document and its History state are the checkpoint; the
            # first persistent raster artifact is the final export.
            jpeg = Path(str(arguments.get("export_path"))).expanduser()
        elif final_operation and not exported_now:
            if str(operation.get("adapter_operation") or "") == "apply_crop" and _crop_geometry_applied(result):
                # Crop pixels already changed the current canvas.  Recover by
                # saving/exporting that exact document, never by re-running
                # geometry on the cropped result.
                saved = _call(
                    "photoshop_fine_edit__save_master",
                    {"path": str(arguments["after_path"]), "format": "psd"},
                    session_id,
                )
                exported = _call(
                    "photoshop_fine_edit__export_jpeg",
                    {
                        "path": str(arguments["export_path"]),
                        "quality": int(arguments.get("jpeg_quality", 12)),
                    },
                    session_id,
                )
                if not isinstance(saved, dict) or not isinstance(exported, dict):
                    raise AdapterError("photoshop_crop_export_recovery_malformed")
                jpeg = Path(str(exported.get("path") or arguments["export_path"])).expanduser()
                exported_now = exported.get("status") == "completed" and jpeg.is_file()
                master_evidence = {
                    "path": str(saved.get("master_path") or arguments["after_path"]),
                    "sha256": saved.get("master_sha256") or _sha256(Path(str(arguments["after_path"]))),
                    "format": str(saved.get("format") or "PSD").upper(),
                    "editable": saved.get("editable") is not False,
                    "layered": saved.get("layered") is not False,
                    "layer_ids": [str(item) for item in saved.get("layer_ids", [])],
                    "mask_ids": [str(item) for item in saved.get("mask_ids", [])],
                }
                export_evidence = {**exported, "status": "completed", "path": str(jpeg)}
                result = {
                    **result,
                    "master_evidence": master_evidence,
                    "export_evidence": export_evidence,
                    "export_recovery": "saved-and-exported-after-geometry-without-reapplying-crop",
                }
            else:
                # A host-side idempotency replay can point to an artifact from
                # an earlier attempt. Retry once with a recovery-scoped ID,
                # but only when no geometry has already been applied.
                retry_operation = dict(operation)
                retry_parameters = retry_operation.get("parameters") if isinstance(retry_operation.get("parameters"), dict) else {}
                retry_operation["parameters"] = {**retry_parameters, "_adapter_recovery_nonce": time.time_ns()}
                retry_tool_name, retry_arguments = _operation_arguments(
                    request,
                    retry_operation,
                    str(request.get("previous_evidence", {}).get("after_path") or initial_working_path) if isinstance(request.get("previous_evidence"), dict) else initial_working_path,
                    document_id=document_id,
                )
                result = _call(retry_tool_name, retry_arguments, session_id)
                if not isinstance(result, dict):
                    raise AdapterError("photoshop_operation_result_malformed")
                export_evidence = result.get("export_evidence") if isinstance(result.get("export_evidence"), dict) else {}
                master_evidence = result.get("master_evidence") if isinstance(result.get("master_evidence"), dict) else {}
                jpeg = Path(str(export_evidence.get("path") or retry_arguments.get("export_path"))).expanduser()
                exported_now = export_evidence.get("status") == "completed" and jpeg.is_file()
        if final_operation and not exported_now:
            raise AdapterError("photoshop_export_missing")
        if not master_evidence and not final_operation:
            persisted_master = state.get("last_master")
            if isinstance(persisted_master, dict) and persisted_master.get("path"):
                master_evidence = persisted_master
            else:
                fallback_master = Path(str(arguments.get("after_path", ""))).expanduser()
                if fallback_master.is_file():
                    master_evidence = {
                        "path": str(fallback_master),
                        "sha256": _sha256(fallback_master),
                        "format": "PSD",
                        "editable": True,
                        "layered": True,
                        "layer_ids": [],
                        "mask_ids": [],
                    }
        operation_manifest = result.get("operation_manifest")
        if not _result_manifest_matches(operation_manifest, expected_bridge_operation_id):
            persisted_manifest = state.get("last_operation_manifest")
            operation_manifest = (
                persisted_manifest
                if _result_manifest_matches(persisted_manifest, expected_bridge_operation_id)
                else None
            )
        if final_operation:
            expected_master = _session_artifact_path(request, "master.psd")
            current_master = Path(str(master_evidence.get("path") or "")).expanduser() if master_evidence else None
            # The crop host can save a valid PSD but omit layer/mask identity
            # metadata from its geometry response.  Re-query the canonical
            # single-document save contract whenever those IDs are missing so
            # the release gate can prove this is an editable layered master.
            master_metadata_incomplete = not (
                isinstance(master_evidence, dict)
                and isinstance(master_evidence.get("layer_ids"), list)
                and bool(master_evidence.get("layer_ids"))
                and isinstance(master_evidence.get("mask_ids"), list)
                and bool(master_evidence.get("mask_ids"))
            )
            if current_master is None or not current_master.is_file() or current_master != expected_master or master_metadata_incomplete:
                saved = _call(
                    "photoshop_fine_edit__save_master",
                    {"path": str(expected_master), "format": "psd"},
                    session_id,
                )
                if not isinstance(saved, dict) or saved.get("status") not in {None, "completed"}:
                    raise AdapterError("photoshop_final_master_save_failed")
                current_master = Path(str(saved.get("master_path") or expected_master)).expanduser()
                if not current_master.is_file():
                    raise AdapterError("photoshop_final_master_missing")
                master_evidence = {
                    "path": str(current_master), "sha256": saved.get("master_sha256") or _sha256(current_master),
                    "format": str(saved.get("format") or "PSD").upper(), "editable": saved.get("editable") is not False,
                    "layered": saved.get("layered") is not False, "layer_ids": [str(item) for item in saved.get("layer_ids", [])],
                    "mask_ids": [str(item) for item in saved.get("mask_ids", [])],
                }
        if final_operation:
            master_evidence = _validated_editable_master_evidence(master_evidence)
            if master_evidence.get("valid") is not True:
                raise AdapterError("photoshop_final_editable_master_invalid")
            exports, print_master = _profile_exports(jpeg, master_evidence, request, operation_id)
        else:
            # Intermediate operations need one preview so the next operation
            # can receive a valid before-path, but do not create delivery
            # profiles or retain a full PSD checkpoint.
            exports, print_master = {}, None
        retained_master = None
        if final_operation:
            candidate_master = master_evidence.get("path") if isinstance(master_evidence, dict) else None
            if isinstance(candidate_master, str) and Path(candidate_master).is_file():
                retained_master = Path(candidate_master)
            else:
                fallback_master = Path(str(arguments.get("after_path", ""))).expanduser()
                if fallback_master.is_file():
                    retained_master = fallback_master
        else:
            # A final PSD may already exist when a director adds one more
            # non-destructive refinement in the same live document. Keep that
            # one canonical master through the History-only operation; do not
            # mistake it for an intermediate artifact and delete it.
            previous_master = state.get("last_master")
            previous_path = previous_master.get("path") if isinstance(previous_master, dict) else None
            if isinstance(previous_path, str) and Path(previous_path).is_file():
                retained_master = Path(previous_path)
        session_master = _session_artifact_path(request, "master.psd")
        # The current preview has the same stable path on every operation.
        # Never delete it as the "previous" preview before returning evidence.
        current_preview = Path(str(jpeg)).expanduser().resolve()
        previous_to_remove = None
        if previous_preview:
            previous_path = Path(str(previous_preview)).expanduser().resolve()
            if previous_path != current_preview:
                previous_to_remove = str(previous_path)
        retained_manifest = None
        if final_operation and isinstance(operation_manifest, dict):
            manifest_path = operation_manifest.get("path")
            if isinstance(manifest_path, str) and Path(manifest_path).is_file():
                retained_manifest = Path(manifest_path)
        _cleanup_ephemeral_outputs(
            request,
            retain_psd=retained_master,
            retain_manifest=retained_manifest,
            remove_previous_preview=previous_to_remove,
        )
        if not final_operation and current_preview.is_file():
            try:
                current_preview.unlink()
            except OSError:
                pass
        if not final_operation:
            master_evidence = {}
        document_evidence = result.get("document_evidence") if isinstance(result.get("document_evidence"), dict) else {}
        output_layer = result.get("output_layer") if isinstance(result.get("output_layer"), dict) else {}
        output_document_id = str(
            document_evidence.get("output_document_id")
            or result.get("document_id")
            or document_id
            or ""
        ) or None
        output_layer_id = (
            document_evidence.get("output_layer_id")
            or output_layer.get("id")
            or result.get("output_layer_id")
            or document_evidence.get("input_layer_id")
            or result.get("input_layer_id")
        )
        history_evidence = result.get("history_evidence") if isinstance(result.get("history_evidence"), dict) else None
        history_status = str(history_evidence.get("status", "")).casefold() if history_evidence else ""
        if not final_operation and (
            history_status not in {"created", "reused", "verified", "completed", "snapshot-created", "snapshot_reused"}
            or history_evidence.get("native_history_api") is not True
        ):
            raise AdapterError("photoshop_native_history_checkpoint_unverified")
        if history_evidence is None:
            history_evidence = {
                "status": "not-required" if final_operation else "unreported",
                "mode": "history",
                "snapshot_name": _history_snapshot_name(request, operation),
                "persistent": False,
            }
        evidence_after_path = str(jpeg) if final_operation and jpeg.is_file() else initial_working_path
        evidence_export_path = str(jpeg) if final_operation and jpeg.is_file() else None
        evidence = {
            "before_path": initial_working_path,
            "after_path": evidence_after_path,
            "export_path": evidence_export_path,
            "exports": exports,
            "operation_manifest": operation_manifest,
            "source_sha256": _sha256(Path(initial_working_path)) if Path(initial_working_path).is_file() else None,
            "document_id": output_document_id,
            "document_path": str(Path(initial_working_path).expanduser().resolve()),
            "document_identity_reconciled": bool(document_identity_reconciled_from and output_document_id),
            "document_mode": "single-document",
            "rollback": history_evidence,
            "software": "Adobe Photoshop",
            "bridge_version": result.get("execution", {}).get("bridge_version") if isinstance(result.get("execution"), dict) else None,
            "actual_locality": "local",
            "artifact_retention": "single-final-master" if final_operation else "single-session-document",
        }
        if final_operation:
            evidence["editable_master"] = print_master or master_evidence
            evidence["master"] = master_evidence
        if document_identity_reconciled_from:
            evidence["document_identity_previous"] = document_identity_reconciled_from
            evidence["document_identity_reconciliation"] = "reopened_same_working_path_after_live_document_check"
        state.update({
            "document_id": output_document_id,
            "last_operation_id": operation_id,
            "last_export": str(jpeg) if final_operation else None,
            "last_master": master_evidence if final_operation else state.get("last_master", {}),
            "last_operation_manifest": operation_manifest or state.get("last_operation_manifest"),
            "history_policy": {"mode": history_evidence.get("mode", "history"), "persistent": False},
        })
        # The first duplicate establishes the stable pixel-layer source for
        # later masks. Groups/adjustment layers are valid edit outputs, but a
        # user mask must target the known pixel layer unless a later tool
        # explicitly returns a more suitable layer.
        adapter_operation = str(operation.get("adapter_operation") or "")
        layer_operation = str((operation.get("parameters") or {}).get("layer_operation") or "duplicate")
        pixel_producing_operation = adapter_operation not in {
            "apply_crop",
            "region_mask_operation",
            "selective_color",
            "curves_local",
            "layer_mask",
        }
        if adapter_operation == "layer_operation":
            pixel_producing_operation = layer_operation == "duplicate"
        if output_layer_id is not None and pixel_producing_operation:
            state["layer_id"] = str(output_layer_id)
            state["pixel_layer_id"] = str(output_layer_id)
        if (
            adapter_operation == "layer_operation"
            and layer_operation == "duplicate"
            and output_layer_id is not None
            and not state.get("base_layer_id")
        ):
            state["base_layer_id"] = str(output_layer_id)
        session_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        quality = (
            _final_quality_evidence(initial_working_path, str(jpeg))
            if final_operation and jpeg.is_file() and Path(initial_working_path).is_file()
            else {}
        )
        return {
            "status": "completed",
            "operation_id": operation_id,
            "backend": "photoshop-fine-edit",
            "actual_locality": "local",
            "document_id": output_document_id,
            "checkpoint_mode": history_evidence.get("mode", "history"),
            "is_final_operation": final_operation,
            "evidence": evidence,
            "quality": quality,
            "raw": result,
        }
    except AdapterError as error:
        return {"status": "paused", "operation_id": operation_id, "reason": str(error), "backend": "photoshop-fine-edit"}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {"status": "failed", "operation_id": operation_id, "reason": f"photoshop_adapter_error:{type(error).__name__}:{error}"}


def main() -> int:
    if "--photo-post-production-json" not in sys.argv:
        print("Use --photo-post-production-json and send one adapter request as JSON on stdin.", file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
        result = execute(request if isinstance(request, dict) else {})
    except Exception as error:
        result = {"status": "failed", "reason": f"photoshop_adapter_protocol_error:{type(error).__name__}:{error}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
