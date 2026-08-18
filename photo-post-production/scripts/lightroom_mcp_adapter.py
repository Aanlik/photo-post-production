#!/usr/bin/env python3
"""Real Lightroom Classic MCP adapter for the photo-post-production runtime.

The execution engine speaks a small JSON protocol.  This adapter translates
that protocol to the pinned ``@mskalski/lightroom-mcp`` stdio server and keeps
all Lightroom writes scoped to a checkpointed, restored Develop transaction.
It never writes the source RAW or requests a managed copy of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_NPX = shutil.which("npx") or "npx"
DEFAULT_PACKAGE = "@mskalski/lightroom-mcp@0.9.0"
MCP_PROTOCOL = "2025-03-26"


class AdapterError(RuntimeError):
    """A recoverable adapter/host failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(value: Any) -> str:
    text = str(value or "photo").strip()
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in text)[:100] or "photo"


def _command() -> list[str]:
    configured = os.environ.get("PHOTO_LIGHTROOM_MCP_COMMAND")
    if configured:
        return shlex.split(configured)
    return [os.environ.get("PHOTO_LIGHTROOM_NPX", DEFAULT_NPX), "-y", os.environ.get("PHOTO_LIGHTROOM_MCP_PACKAGE", DEFAULT_PACKAGE)]


def _read_until(process: subprocess.Popen[str], message_id: int, timeout: float) -> dict[str, Any]:
    if process.stdout is None:
        raise AdapterError("lightroom_mcp_stdout_unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            events = selector.select(max(0.05, min(0.5, deadline - time.monotonic())))
            if not events:
                continue
            line = process.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == message_id:
                return payload
    finally:
        selector.close()
    stderr = ""
    if process.stderr is not None:
        try:
            stderr = process.stderr.read()[-800:]
        except OSError:
            pass
    raise AdapterError(f"lightroom_mcp_timeout_or_exit:id={message_id}:stderr={stderr}")


def _send(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise AdapterError("lightroom_mcp_stdin_unavailable")
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _decode_content(value: Any) -> Any:
    """Unwrap MCP text content, including the server's JSON-in-text envelope."""

    if isinstance(value, dict) and value.get("isError") is True:
        raise AdapterError("lightroom_tool_error:" + json.dumps(value, ensure_ascii=False)[:800])
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
            raise AdapterError("lightroom_tool_text_error:" + texts[-1][:800])
    return value


def call_tool(name: str, arguments: dict[str, Any], timeout: float | None = None) -> Any:
    """Start one isolated stdio MCP session and call one Lightroom tool."""

    timeout_value = float(timeout or os.environ.get("PHOTO_LIGHTROOM_MCP_TIMEOUT", "45"))
    try:
        process = subprocess.Popen(
            _command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as error:
        raise AdapterError(f"lightroom_mcp_start_failed:{type(error).__name__}") from error
    try:
        _send(process, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "photo-post-production-skill", "version": "1.0"},
            },
        })
        initialize = _read_until(process, 1, timeout_value)
        if initialize.get("error"):
            raise AdapterError("lightroom_initialize_error:" + json.dumps(initialize["error"], ensure_ascii=False)[:800])
        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_response = _read_until(process, 2, timeout_value)
        if tools_response.get("error"):
            raise AdapterError("lightroom_tools_list_error:" + json.dumps(tools_response["error"], ensure_ascii=False)[:800])
        # The Lightroom plug-in binds the response socket immediately after
        # the MCP process connects.  Give that local socket handshake a short
        # grace period before the first catalog request.
        time.sleep(float(os.environ.get("PHOTO_LIGHTROOM_PLUGIN_GRACE", "5")))
        _send(process, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        response = _read_until(process, 3, timeout_value)
        if response.get("error"):
            raise AdapterError("lightroom_call_error:" + json.dumps(response["error"], ensure_ascii=False)[:800])
        return _decode_content(response.get("result", {}))
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def _operation_settings(settings: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "white_balance": "WhiteBalance",
        "temperature": "Temperature",
        "tint": "Tint",
        "exposure": "Exposure2012",
        "contrast": "Contrast2012",
        "highlights": "Highlights2012",
        "shadows": "Shadows2012",
        "whites": "Whites2012",
        "blacks": "Blacks2012",
        "clarity": "Clarity2012",
        "vibrance": "Vibrance",
        "saturation": "Saturation",
        "sharpening": "Sharpness",
        "sharpness": "Sharpness",
        "luminance_smoothing": "LuminanceSmoothing",
        "luminance_noise_reduction_detail": "LuminanceNoiseReductionDetail",
        "luminance_noise_reduction_contrast": "LuminanceNoiseReductionContrast",
        "color_noise_reduction": "ColorNoiseReduction",
        "color_noise_reduction_detail": "ColorNoiseReductionDetail",
        "color_noise_reduction_smoothness": "ColorNoiseReductionSmoothness",
        "texture": "Texture",
        "dehaze": "Dehaze",
    }
    hsl_dimensions = {"hue": "HueAdjustment", "saturation": "SaturationAdjustment", "luminance": "LuminanceAdjustment"}
    hsl_channels = {item.casefold(): item for item in ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta")}
    direct_keys = set(mapping.values()) | {
        f"{prefix}{channel}"
        for prefix in hsl_dimensions.values()
        for channel in hsl_channels.values()
    }
    direct_keys.update({
        "LuminanceSmoothing", "LuminanceNoiseReductionDetail",
        "LuminanceNoiseReductionContrast", "ColorNoiseReduction",
        "ColorNoiseReductionDetail", "ColorNoiseReductionSmoothness",
    })
    result: dict[str, Any] = {}
    for key, value in settings.items():
        normalized = str(key).casefold()
        if normalized == "hsl" and isinstance(value, dict):
            for channel_name, channel_values in value.items():
                channel = hsl_channels.get(str(channel_name).casefold())
                if not channel or not isinstance(channel_values, dict):
                    continue
                for dimension_name, adjustment in channel_values.items():
                    prefix = hsl_dimensions.get(str(dimension_name).casefold())
                    if prefix and isinstance(adjustment, (int, float)) and not isinstance(adjustment, bool):
                        result[f"{prefix}{channel}"] = adjustment
            continue
        sdk_key = mapping.get(normalized, key if key in direct_keys else None)
        if isinstance(sdk_key, str) and sdk_key and not isinstance(value, (dict, list)):
            result[sdk_key] = value
    return result


def _metadata_settings(metadata: Any, keys: list[str]) -> dict[str, Any]:
    """Find allowlisted settings in common Lightroom metadata response shapes."""

    found: dict[str, Any] = {}
    if isinstance(metadata, dict):
        normalized = _operation_settings(metadata)
        for key in keys:
            if key in metadata:
                found[key] = metadata[key]
            elif key in normalized:
                found[key] = normalized[key]
        for child in metadata.values():
            nested = _metadata_settings(child, keys)
            found.update(nested)
    elif isinstance(metadata, list):
        for child in metadata:
            found.update(_metadata_settings(child, keys))
    return found


def _restorable_settings(
    metadata: Any,
    requested: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    """Keep temporary Develop writes inside the readable restore projection."""

    original = _metadata_settings(metadata, list(requested))
    executable = {key: value for key, value in requested.items() if key in original}
    restore = {key: original[key] for key in executable}
    skipped = [
        {"key": key, "reason": "lightroom_setting_not_readable_for_restore"}
        for key in requested
        if key not in original
    ]
    return executable, restore, skipped


_INTEGER_SLIDER_KEYS = frozenset({
    "Sharpness",
    "SharpenDetail",
    "SharpenEdgeMasking",
    "LuminanceSmoothing",
    "LuminanceNoiseReductionDetail",
    "LuminanceNoiseReductionContrast",
    "ColorNoiseReduction",
    "ColorNoiseReductionDetail",
    "ColorNoiseReductionSmoothness",
})


def _settings_tolerance(key: str, expected: Any) -> float:
    """Return the known Lightroom slider quantization tolerance."""

    if key in _INTEGER_SLIDER_KEYS and isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return 0.51
    return 0.01


def _settings_equal(key: str, expected: Any, actual: Any) -> bool:
    """Compare readback values while preserving real write failures."""

    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= _settings_tolerance(key, expected)
    return expected == actual


def _verify_settings_readback(requested: dict[str, Any], metadata: Any) -> dict[str, Any]:
    actual = _metadata_settings(metadata, list(requested))
    mismatches = {
        key: {"requested": value, "actual": actual.get(key)}
        for key, value in requested.items()
        if key not in actual or not _settings_equal(key, value, actual[key])
    }
    if mismatches:
        raise AdapterError("lightroom_write_verification_failed:" + json.dumps(mismatches, ensure_ascii=False, sort_keys=True))
    quantized = {
        key: {"requested": value, "actual": actual[key], "tolerance": _settings_tolerance(key, value)}
        for key, value in requested.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isinstance(actual.get(key), (int, float))
        and abs(float(value) - float(actual[key])) > 0.01
    }
    return {
        "status": "verified",
        "requested": requested,
        "actual": {key: actual[key] for key in requested},
        "quantized_readback": quantized,
    }


def _resolve_photo_id(request: dict[str, Any]) -> str:
    operation = request.get("operation") if isinstance(request.get("operation"), dict) else {}
    parameters = operation.get("parameters") if isinstance(operation.get("parameters"), dict) else {}
    # The score record uses a stable filename key (for example sample-photo),
    # while Lightroom accepts the catalog photo path.  Prefer an explicit
    # adapter photo_id, then the immutable source path, and only then the
    # display key.
    explicit = parameters.get("photo_id")
    if explicit:
        return str(explicit).strip()
    source = request.get("source_path")
    if source:
        source_path = Path(str(source)).expanduser()
        if source_path.exists():
            return str(source_path.resolve())
        return str(source_path)
    return str(request.get("photo_id") or "").strip()


def _find_catalog_photo_id(source_path: str | None) -> str | None:
    """Resolve an imported catalog photo when the working folder is new.

    Lightroom edits are applied to the catalog item, while the pipeline may
    stage a byte-identical RAW in a temporary or user-selected input folder.
    Prefer an exact catalog path; fall back to a unique filename match.
    """

    if not source_path:
        return None
    filename = Path(source_path).name
    if not filename:
        return None
    matches = call_tool("search_photos", {"filename": filename, "limit": 20, "offset": 0})
    if isinstance(matches, dict):
        matches = matches.get("photos", matches.get("results", []))
    if not isinstance(matches, list):
        return None
    source_resolved = str(Path(source_path).expanduser().resolve())
    exact = [
        item for item in matches
        if isinstance(item, dict)
        and item.get("path")
        and str(Path(str(item["path"])).expanduser().resolve()) == source_resolved
    ]
    candidates = exact or [item for item in matches if isinstance(item, dict) and item.get("id") is not None]
    if len(candidates) != 1:
        return None
    return str(candidates[0]["id"])


def _catalog_records(value: Any) -> list[dict[str, Any]]:
    """Collect record-shaped values from the import tool's JSON envelope."""

    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("id", "photo_id", "photoId")):
            records.append(value)
        for child in value.values():
            records.extend(_catalog_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_catalog_records(child))
    return records


def _catalog_photo_id_from_import(value: Any, source_path: str | None) -> str | None:
    """Resolve the newly imported catalog id across known MCP response shapes."""

    if not source_path:
        return None
    source = Path(source_path).expanduser().resolve()
    filename = source.name.casefold()
    records = _catalog_records(value)

    def record_id(record: dict[str, Any]) -> str | None:
        candidate = record.get("id", record.get("photo_id", record.get("photoId")))
        return str(candidate) if candidate is not None and str(candidate).strip() else None

    def record_path(record: dict[str, Any]) -> str | None:
        for key in ("path", "source_path", "sourcePath", "file_path", "filePath", "filename", "name"):
            candidate = record.get(key)
            if candidate:
                return str(candidate)
        return None

    exact = [
        record_id(record)
        for record in records
        if record_id(record)
        and record_path(record)
        and str(Path(record_path(record)).expanduser().resolve()) == str(source)
    ]
    if len(exact) == 1:
        return exact[0]

    matching = [
        record_id(record)
        for record in records
        if record_id(record)
        and record_path(record)
        and Path(record_path(record)).name.casefold() == filename
    ]
    if len(set(matching)) == 1:
        return matching[0]

    # Some versions return the imported id in a small envelope without the
    # source path. Accept it only when the response contains one unique id.
    ids = [record_id(record) for record in records if record_id(record)]
    if len(set(ids)) == 1:
        return ids[0]
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    return None


def _import_catalog_photo_id(source_path: str | None) -> tuple[str | None, Any]:
    """Import an unresolved source at its original location and resolve its id."""

    if not source_path:
        return None, None
    source = Path(source_path).expanduser().resolve()
    imported = call_tool("import_photos", {"source_path": str(source)})
    photo_id = _catalog_photo_id_from_import(imported, str(source))
    if not photo_id:
        # The import response in 0.9.0 may report only a count. Re-query the
        # bounded filename search after Lightroom has committed the import.
        photo_id = _find_catalog_photo_id(str(source))
    return photo_id, imported


def _new_export(destination: Path, source_path: str | None, before: set[str]) -> Path | None:
    if not destination.is_dir():
        return None
    candidates = [item for item in destination.rglob("*") if item.is_file() and item.suffix.casefold() in {".jpg", ".jpeg", ".tif", ".tiff", ".png"}]
    source_stem = Path(source_path).stem.casefold() if source_path else ""
    fresh = [item for item in candidates if str(item) not in before]
    matching = [item for item in fresh if source_stem and source_stem in item.stem.casefold()]
    candidates = matching or fresh or candidates
    return max(candidates, key=lambda item: item.stat().st_mtime_ns) if candidates else None


def _working_export_name(request: dict[str, Any], suffix: str = ".tif") -> str:
    extension = suffix if str(suffix).startswith(".") else f".{suffix}"
    return "--".join((
        _safe(request.get("run_id") or "run"),
        _safe(request.get("photo_id") or "photo"),
        _safe(request.get("variant_name") or "variant"),
    )) + extension.casefold()


def _scope_working_export(exported: Path, destination: Path, request: dict[str, Any]) -> Path:
    """Give the Photoshop handoff a run-unique basename.

    Photoshop may reuse an already open document when another TIFF has the
    same basename, even when the files live in different run directories.
    A run-scoped handoff name prevents that cross-run identity collision.
    """

    target = destination / _working_export_name(request, exported.suffix)
    if exported.resolve() == target.resolve():
        return exported
    if target.exists():
        if _sha256(target) != _sha256(exported):
            raise AdapterError("lightroom_working_handoff_collision")
        exported.unlink()
    else:
        exported.replace(target)
    parent = exported.parent
    if parent != destination and parent.name.startswith("attempt-"):
        try:
            parent.rmdir()
        except OSError:
            pass
    return target


def _export(
    photo_id: str,
    source_path: str | None,
    destination: Path,
    profile: str,
    fmt: str,
    quality: int,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    # Lightroom Classic may show an overwrite dialog for an existing export,
    # which is invisible to a headless MCP call. On retries, isolate the new
    # render in a deterministic-at-runtime attempt directory and retain the
    # prior artifact for rollback/evidence.
    export_destination = destination
    if any(item.is_file() for item in destination.rglob("*")):
        export_destination = destination / f"attempt-{time.time_ns()}"
        export_destination.mkdir(parents=True, exist_ok=True)
    before = {str(item) for item in export_destination.rglob("*") if item.is_file()}
    call_tool("export_photos", {
        "photo_ids": [photo_id],
        "destination": str(export_destination),
        "format": fmt,
        "quality": quality,
    }, timeout=float(os.environ.get("PHOTO_LIGHTROOM_EXPORT_TIMEOUT", "300")))
    exported = _new_export(export_destination, source_path, before)
    if exported is None or not exported.is_file():
        raise AdapterError(f"lightroom_export_file_not_found:{profile}")
    # A retry directory is only needed while this export is being discovered.
    # Once the current artifact is verified, discard older retry copies so a
    # large working TIFF cannot accumulate across iterations.
    for child in destination.iterdir():
        if child.is_dir() and child.name.startswith("attempt-") and child.resolve() != exported.parent.resolve():
            shutil.rmtree(child, ignore_errors=True)
    return exported


def _state_path(request: dict[str, Any]) -> Path:
    root = Path(str(request.get("output_dir") or Path.home() / "Library/Application Support/PhotoPostProduction")).expanduser().resolve()
    root.joinpath("adobe-runtime").mkdir(parents=True, exist_ok=True)
    return root / "adobe-runtime" / f"lr-{_safe(request.get('run_id'))}-{_safe(request.get('photo_id'))}-{_safe(request.get('variant_name'))}.json"


def _completed(request: dict[str, Any], operation_id: str, evidence: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "operation_id": operation_id,
        "backend": "lightroom-mcp",
        "actual_locality": "local",
        "software": "Adobe Lightroom Classic",
        "bridge_version": "lightroom-mcp-server 0.9.0",
        "evidence": evidence,
        **extra,
    }


def execute(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation") if isinstance(request.get("operation"), dict) else {}
    operation_id = str(operation.get("operation_id") or request.get("idempotency_key") or "lr-operation")
    source_path = str(request.get("source_path") or "") or None
    photo_id = _resolve_photo_id(request)
    if not photo_id:
        return {"status": "failed", "operation_id": operation_id, "reason": "lightroom_photo_id_missing"}
    output_dir = Path(str(request.get("output_dir") or Path.cwd())).expanduser().resolve()
    state_file = _state_path(request)
    state: dict[str, Any] = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    catalog_import: dict[str, Any] | None = None

    def completed(evidence: dict[str, Any], **extra: Any) -> dict[str, Any]:
        if catalog_import is not None:
            evidence["catalog_import"] = catalog_import
        return _completed(request, operation_id, evidence, **extra)

    try:
        try:
            before_metadata = call_tool("get_photo_metadata", {"photo_id": photo_id})
        except AdapterError as first_error:
            try:
                catalog_photo_id = _find_catalog_photo_id(source_path)
            except AdapterError:
                catalog_photo_id = None
            imported_response: Any = None
            if not catalog_photo_id:
                try:
                    catalog_photo_id, imported_response = _import_catalog_photo_id(source_path)
                    catalog_import = {
                        "status": "imported" if catalog_photo_id else "unresolved",
                        "source_path": source_path,
                        "photo_id": catalog_photo_id,
                        "response": imported_response,
                    }
                except AdapterError as import_error:
                    raise AdapterError(f"{first_error}; catalog_import_failed:{import_error}") from import_error
            if not catalog_photo_id or catalog_photo_id == photo_id:
                raise first_error
            photo_id = catalog_photo_id
            before_metadata = call_tool("get_photo_metadata", {"photo_id": photo_id})
        if not isinstance(before_metadata, (dict, list)):
            raise AdapterError("lightroom_metadata_malformed")
        if operation.get("parameters", {}).get("export_only"):
            export_plan = request.get("adapter_plan", {}).get("lightroom", {}).get("export", {}) if isinstance(request.get("adapter_plan"), dict) else {}
            if operation.get("parameters", {}).get("working_tiff"):
                working_dir = output_dir / "working" / _safe(request.get("photo_id")) / _safe(request.get("variant_name"))
                source_checksum = _sha256(Path(source_path)) if source_path and Path(source_path).is_file() else None
                cached_working = Path(str(state.get("working_path"))).expanduser() if state.get("working_path") else None
                if cached_working and cached_working.is_file() and state.get("source_sha256") == source_checksum:
                    return completed({
                        "before_path": source_path,
                        "after_path": str(cached_working),
                        "working_path": str(cached_working),
                        "export_path": str(cached_working),
                        "metadata": before_metadata,
                        "source_sha256": source_checksum,
                        "render_boundary": "raw-to-16-bit-tiff-before-photoshop",
                        "cache": "verified-existing-working-render",
                    }, checkpoint={"status": "verified", "metadata": before_metadata, "cache": "hit"})
                working = _scope_working_export(
                    _export(photo_id, source_path, working_dir, "working-render", "TIFF", 100),
                    working_dir,
                    request,
                )
                state.update({
                    "photo_id": photo_id,
                    "source_path": source_path,
                    "source_sha256": source_checksum,
                    "before_metadata": before_metadata,
                    "working_path": str(working),
                    "restore_status": "not-required",
                    "updated_at": time.time(),
                })
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                return completed({
                    "before_path": source_path,
                    "after_path": str(working),
                    "working_path": str(working),
                    "export_path": str(working),
                    "metadata": before_metadata,
                    "source_sha256": source_checksum,
                    "render_boundary": "raw-to-16-bit-tiff-before-photoshop",
                }, checkpoint={"status": "verified", "metadata": before_metadata})
            profiles = export_plan.get("profiles") if isinstance(export_plan, dict) else None
            profiles = profiles if isinstance(profiles, list) else ["web-share", "competition-quality"]
            exports: dict[str, str] = {}
            for profile in profiles:
                profile_name = str(profile)
                if profile_name == "print-master":
                    path = _export(photo_id, source_path, output_dir / "exports" / profile_name, profile_name, "TIFF", 100)
                else:
                    path = _export(photo_id, source_path, output_dir / "exports" / profile_name, profile_name, "JPEG", int(export_plan.get("quality", 100) if isinstance(export_plan, dict) else 100))
                exports[profile_name] = str(path)
            preferred = exports.get("competition-quality") or next(iter(exports.values()))
            return completed({
                "before_path": source_path,
                "after_path": preferred,
                "export_path": preferred,
                "exports": exports,
                "metadata": before_metadata,
                "source_sha256": _sha256(Path(source_path)) if source_path and Path(source_path).is_file() else None,
            }, checkpoint={"status": "verified", "metadata": before_metadata})

        planned_settings = _operation_settings(operation.get("parameters") if isinstance(operation.get("parameters"), dict) else {})
        planned_settings = {key: value for key, value in planned_settings.items() if key not in {"mask_confidence", "export_only"}}
        settings, original, skipped_settings = _restorable_settings(before_metadata, planned_settings)
        # A paused Photoshop stage must resume from the exact Lightroom TIFF
        # already opened in the live document.  Re-running the Develop/export
        # node would create a second RAW-sized TIFF and could silently diverge
        # from Photoshop's current pixels.  The state file is scoped by
        # run/photo/variant, so a verified restored catalog state plus matching
        # source hash is a safe operation-level resume checkpoint.
        source_checksum = _sha256(Path(source_path)) if source_path and Path(source_path).is_file() else None
        cached_working = Path(str(state.get("working_path"))).expanduser() if state.get("working_path") else None
        cached_settings = state.get("requested_settings")
        settings_match = cached_settings == settings if isinstance(cached_settings, dict) else state.get("restore_status") in {"verified", "not-required"}
        if (
            cached_working is not None
            and cached_working.is_file()
            and state.get("source_sha256") == source_checksum
            and state.get("restore_status") in {"verified", "not-required"}
            and settings_match
        ):
            return completed({
                "before_path": source_path,
                "after_path": str(cached_working),
                "export_path": str(cached_working),
                "working_path": str(cached_working),
                "metadata": before_metadata,
                "restore": {"status": state.get("restore_status"), "verified": True, "metadata": before_metadata},
                "source_sha256": source_checksum,
                "cache": "verified-existing-working-render",
                "planned_settings": state.get("planned_settings", settings),
                "applied_settings": settings,
                "skipped_settings": state.get("skipped_settings", []),
                "write_readback": state.get("write_readback", {"status": "verified-from-checkpoint"}),
            }, checkpoint={"status": "verified", "path": str(state_file), "cache": "hit"})
        if settings:
            call_tool("set_develop_settings", {"photo_id": photo_id, "settings": settings})
            applied_metadata = call_tool("get_photo_metadata", {"photo_id": photo_id})
            write_readback = _verify_settings_readback(settings, applied_metadata)
        else:
            applied_metadata = before_metadata
            write_readback = {"status": "not-required", "requested": {}, "actual": {}}
        working_dir = output_dir / "working" / _safe(request.get("photo_id")) / _safe(request.get("variant_name"))
        working = _scope_working_export(
            _export(photo_id, source_path, working_dir, "working-render", "TIFF", 100),
            working_dir,
            request,
        )
        restore = {key: original[key] for key in settings if key in original}
        restore_status = "not-required"
        restored_metadata = before_metadata
        if settings:
            call_tool("set_develop_settings", {"photo_id": photo_id, "settings": restore})
            restored_metadata = call_tool("get_photo_metadata", {"photo_id": photo_id})
            restored = _metadata_settings(restored_metadata, list(restore))
            if restored != restore:
                raise AdapterError("lightroom_restore_verification_failed")
            restore_status = "verified"
        state.update({
            "photo_id": photo_id,
            "source_path": source_path,
            "source_sha256": source_checksum,
            "operation_id": operation_id,
            "requested_settings": settings,
            "planned_settings": planned_settings,
            "skipped_settings": skipped_settings,
            "before_metadata": before_metadata,
            "restore_settings": restore,
            "restore_status": restore_status,
            "write_readback": write_readback,
            "working_path": str(working),
            "updated_at": time.time(),
        })
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return completed({
            "before_path": source_path,
            "after_path": str(working),
            "export_path": str(working),
            "working_path": str(working),
            "metadata": before_metadata,
            "restore": {"status": restore_status, "verified": restore_status in {"verified", "not-required"}, "metadata": restored_metadata},
            "source_sha256": state.get("source_sha256"),
            "planned_settings": planned_settings,
            "applied_settings": settings,
            "skipped_settings": skipped_settings,
            "write_readback": write_readback,
        }, checkpoint={"status": restore_status, "path": str(state_file)})
    except AdapterError as error:
        return {"status": "paused", "operation_id": operation_id, "reason": str(error), "backend": "lightroom-mcp"}
    except (OSError, ValueError, TypeError) as error:
        return {"status": "failed", "operation_id": operation_id, "reason": f"lightroom_adapter_error:{type(error).__name__}:{error}"}


def main() -> int:
    if "--photo-post-production-json" not in sys.argv:
        print("Use --photo-post-production-json and send one adapter request as JSON on stdin.", file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
        result = execute(request if isinstance(request, dict) else {})
    except Exception as error:  # Keep the JSON protocol intact for the caller.
        result = {"status": "failed", "reason": f"lightroom_adapter_protocol_error:{type(error).__name__}:{error}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
