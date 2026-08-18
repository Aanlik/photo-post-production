"""Read-only health and result validation for a Photoshop adapter bridge.

Application discovery is deliberately weaker than bridge health.  This module
never opens or edits an image; it only inspects local prerequisites and
normalizes payloads already returned by a bridge.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Tuple


_BOOLEAN_CAPABILITIES = (
    "supports_masks",
    "supports_mask_validation",
    "supports_layers",
    "supports_non_destructive_layers",
    "supports_history",
    "supports_export",
    "supports_generative_control",
    "supports_generative_reporting",
    "supports_operation_id",
    "supports_operation_manifest",
)

_REQUIRED_FINE_EDIT_CAPABILITIES = _BOOLEAN_CAPABILITIES
_DEFAULT_BROKER_URL = "http://127.0.0.1:47391"
_DEVELOPER_MODE_PROBE_MARKER = "independent-read-only-v1"
_INDEPENDENT_DEVELOPER_MODE_SOURCES = frozenset({"photoshop-system-settings-readback"})
_PHOTOSHOP_DEVELOPER_MODE_SETTING = "UXPDeveloperMode"


def _structured_reason(
    code: str,
    prerequisite: str,
    message: str,
    details: Optional[List[str]] = None,
) -> dict:
    reason = {
        "code": code,
        "prerequisite": prerequisite,
        "message": message,
    }
    if details:
        reason["details"] = details
    return reason


def _base_health_result() -> dict:
    result = {
        "available": False,
        "bridge_version": None,
        "supports_masks": False,
        "supports_mask_validation": False,
        "supports_layers": False,
        "supports_non_destructive_layers": False,
        "supports_history": False,
        "supports_export": False,
        "supports_generative_control": False,
        "supports_generative_reporting": False,
        "supports_operation_id": False,
        "supports_operation_manifest": False,
        "backend_locality": None,
        "generative_backend_healthy": False,
        "generative_backend_locality": None,
        "fine_edit_mode": False,
        "generative_operations_enabled": False,
        "generative_downgrade_reason": None,
        "mode": "global-only",
        "downgrade_reason": None,
        "discovery": {},
        "smoke_test": {
            "eligible": False,
            "status": "not_run",
            "reason": "Photoshop bridge health prerequisites did not pass.",
        },
    }
    return result


def _photoshop_installation() -> Tuple[Optional[str], Optional[str]]:
    candidates = sorted(
        glob.glob("/Applications/Adobe Photoshop*/Adobe Photoshop*.app"),
        reverse=True,
    )
    if not candidates:
        return None, None

    app_path = candidates[0]
    info_path = Path(app_path) / "Contents" / "Info.plist"
    version = None
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        raw_version = info.get("CFBundleShortVersionString")
        if isinstance(raw_version, str) and raw_version.strip():
            version = raw_version.strip()
    except (OSError, plistlib.InvalidFileException):
        pass
    return app_path, version


def _uxp_developer_tool_path() -> Optional[str]:
    candidates = (
        "/Applications/Adobe UXP Developer Tool.app",
        "/Applications/Adobe UXP Developer Tools.app",
        "/Applications/Adobe UXP Developer Tools/Adobe UXP Developer Tool.app",
        "/Applications/Adobe UXP Developer Tools/Adobe UXP Developer Tools.app",
        "/Applications/UXP Developer Tool.app",
        "/Applications/UXP Developer Tools.app",
        str(Path.home() / "Applications" / "Adobe UXP Developer Tool.app"),
        str(Path.home() / "Applications" / "Adobe UXP Developer Tools.app"),
        str(Path.home() / "Applications" / "UXP Developer Tool.app"),
        str(Path.home() / "Applications" / "UXP Developer Tools.app"),
    )
    return next((candidate for candidate in candidates if Path(candidate).exists()), None)


def _staged_bridge_manifest() -> Optional[str]:
    candidates = (
        Path.home() / ".local" / "share" / "adobepy" / "bridges" / "photoshop" / "manifest.json",
        Path.home() / "Library" / "Application Support" / "adobepy" / "bridges" / "photoshop" / "manifest.json",
    )
    return next((str(candidate) for candidate in candidates if candidate.is_file()), None)


def _read_json_url(url: str, timeout: float = 0.75) -> Tuple[Optional[Any], Optional[str]]:
    endpoint = url.rstrip("/") + "/v1/capabilities"
    token = os.environ.get("ADOBEPY_TOKEN", "dev-token")
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "x-adobepy-token": token,
            "Authorization": "Bearer {}".format(token),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return None, str(exc)
    if not isinstance(payload, (dict, list)):
        return None, "capability endpoint returned unsupported JSON"
    return payload, None


def _run_read_only_cli(args: List[str]) -> Tuple[Optional[dict], Optional[str]]:
    executable = shutil.which(args[0])
    if executable is None:
        return None, "executable not found"
    try:
        completed = subprocess.run(
            [executable] + args[1:],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "non-zero exit status {}".format(completed.returncode)
        return None, detail
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        return None, "CLI returned malformed JSON: {}".format(exc)
    if not isinstance(payload, dict):
        return None, "CLI returned non-object JSON"
    return payload, None


def _process_running(pattern: str) -> bool:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return False
    try:
        completed = subprocess.run(
            [pgrep, "-f", pattern],
            capture_output=True,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _probe_developer_mode() -> dict:
    """Read an exact Photoshop system preference independently of the bridge."""

    preference_path = Path.home() / "Library" / "Preferences" / "com.adobe.Photoshop.plist"
    result = {
        "enabled": None,
        "source_type": None,
        "probe_marker": _DEVELOPER_MODE_PROBE_MARKER,
        "setting_key": _PHOTOSHOP_DEVELOPER_MODE_SETTING,
        "path": str(preference_path),
    }
    try:
        with preference_path.open("rb") as handle:
            preferences = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return result
    value = preferences.get(_PHOTOSHOP_DEVELOPER_MODE_SETTING)
    if not isinstance(value, bool):
        return result
    result["enabled"] = value
    result["source_type"] = "photoshop-system-settings-readback"
    return result


def _validated_developer_mode_evidence(payload: Any) -> Tuple[Optional[bool], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None
    source_type = payload.get("source_type")
    if source_type not in _INDEPENDENT_DEVELOPER_MODE_SOURCES:
        return None, None
    if payload.get("probe_marker") != _DEVELOPER_MODE_PROBE_MARKER:
        return None, None
    if payload.get("setting_key") != _PHOTOSHOP_DEVELOPER_MODE_SETTING:
        return None, None
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return None, None
    return enabled, source_type


def _broker_records(payload: Optional[Any]) -> Tuple[dict, dict, dict]:
    if isinstance(payload, list):
        for record in payload:
            if not isinstance(record, dict):
                continue
            capabilities = record.get("capabilities")
            if not isinstance(capabilities, dict) or capabilities.get("host") != "photoshop":
                continue
            features = capabilities.get("features")
            features = set(features) if isinstance(features, list) else set()
            normalized = dict(capabilities)
            feature_flags = {
                "supports_masks": "masks",
                "supports_mask_validation": "maskValidation",
                "supports_layers": "nonDestructiveLayers",
                "supports_non_destructive_layers": "nonDestructiveLayers",
                "supports_history": (
                    "history" in features
                    or "historySnapshots" in features
                    or "historySnapshots" in str(capabilities.get("methods", {}))
                ),
                "supports_export": "export" in features or "exportWithPreset" in str(capabilities.get("methods", {})),
                "supports_generative_control": "generativeControl",
                "supports_generative_reporting": "generativeReporting",
                "supports_operation_id": "operationIds",
                "supports_operation_manifest": "operationManifest",
            }
            for field, feature in feature_flags.items():
                if not isinstance(normalized.get(field), bool):
                    normalized[field] = feature if isinstance(feature, bool) else feature in features
            normalized.setdefault("backend_locality", "local")
            # The bridge intentionally reports the local generative backend as
            # unhealthy until a separately verified model is configured.  The
            # current broker capability record omits extension fields, so keep
            # this explicit and structured in the normalized record.
            if not isinstance(normalized.get("generative_backend_healthy"), bool):
                normalized["generative_backend_healthy"] = False
            if not isinstance(normalized.get("generative_backend_locality"), str):
                normalized["generative_backend_locality"] = "local"
            bridge_version = capabilities.get("bridgeVersion")
            bridge = {
                "connected": isinstance(record.get("connectedAtEpochMs"), (int, float)),
                "host": "photoshop",
                "plugin_id": "com.adobepy.bridge.photoshop",
                "uxp_loaded": True,
                "version": bridge_version,
                "source": "broker-session-capabilities",
            }
            generative_backend = {
                "healthy": normalized.get("generative_backend_healthy"),
                "locality": normalized.get("generative_backend_locality"),
                "name": "bridge-declared-local-generative-backend",
                "version": bridge_version,
            }
            return bridge, normalized, generative_backend
        return {}, {}, {}
    if not isinstance(payload, dict):
        return {}, {}, {}
    bridge = payload.get("bridge")
    capabilities = payload.get("capabilities")
    generative_backend = payload.get("generative_backend")
    return (
        bridge if isinstance(bridge, dict) else {},
        capabilities if isinstance(capabilities, dict) else {},
        generative_backend if isinstance(generative_backend, dict) else {},
    )


def _photoshop_cli_session(payload: Optional[dict]) -> Tuple[Optional[dict], List[str]]:
    if not isinstance(payload, dict):
        return None, []
    sessions = payload.get("sessions")
    if isinstance(sessions, list):
        for session in sessions:
            if not isinstance(session, dict):
                continue
            if session.get("dcc_type") != "photoshop" or session.get("connected") is not True:
                continue
            tools = session.get("tools")
            if not isinstance(tools, list):
                return session, []
            tool_names = []
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name")
                if isinstance(name, str) and name.strip():
                    tool_names.append(name.strip())
            return session, tool_names
    instances = payload.get("instances")
    if isinstance(instances, list):
        for instance in instances:
            if not isinstance(instance, dict) or instance.get("dcc_type") != "photoshop":
                continue
            if instance.get("status") not in {"available", "ready"}:
                continue
            direct_control = instance.get("direct_control")
            connected = isinstance(direct_control, dict) and direct_control.get("ready") is True
            if not connected:
                continue
            return {**instance, "connected": True, "tools": []}, []
    return None, []


def _find_local_executable(name: str) -> Optional[str]:
    path = shutil.which(name)
    if path:
        return path
    fallbacks = {
        "dcc-mcp-photoshop": Path.home() / ".local" / "share" / "dcc-mcp-photoshop-venv" / "bin" / name,
        "dcc-mcp-cli": Path.home() / ".local" / "bin" / name,
        "adobepy": Path.home() / ".local" / "bin" / name,
    }
    candidate = fallbacks.get(name)
    return str(candidate) if candidate and candidate.is_file() else None


def _discover_adapter_health() -> dict:
    """Inspect the selected dcc-mcp-photoshop path without editing an image."""

    photoshop_path, photoshop_version = _photoshop_installation()
    rustc_path = _find_local_executable("rustc")
    cargo_path = _find_local_executable("cargo")
    adobepy_path = _find_local_executable("adobepy")
    adapter_path = _find_local_executable("dcc-mcp-photoshop")
    dcc_cli_path = _find_local_executable("dcc-mcp-cli")
    uxp_tool_path = _uxp_developer_tool_path()
    bridge_manifest = _staged_bridge_manifest()
    broker_url = os.environ.get("ADOBEPY_BROKER_URL", _DEFAULT_BROKER_URL)
    broker_payload, broker_error = _read_json_url(broker_url)

    dcc_list_payload = None
    dcc_list_error = "dcc-mcp-cli is not installed"
    if dcc_cli_path:
        dcc_list_payload, dcc_list_error = _run_read_only_cli([
            "dcc-mcp-cli", "--output", "json", "--no-auto-gateway", "list"
        ])

    bridge_record, capability_record, generative_backend_record = _broker_records(broker_payload)
    bridge_connected = (
        bridge_record.get("connected") is True
        and bridge_record.get("host") == "photoshop"
    )
    bridge_loaded = (
        bridge_connected
        and bridge_record.get("plugin_id") == "com.adobepy.bridge.photoshop"
        and bridge_record.get("uxp_loaded") is True
    )
    developer_mode_probe = _probe_developer_mode()
    developer_mode, developer_mode_source = _validated_developer_mode_evidence(
        developer_mode_probe
    )
    photoshop_session, tool_names = _photoshop_cli_session(dcc_list_payload)
    if photoshop_session is not None and not tool_names and dcc_cli_path:
        instance_id = photoshop_session.get("instance_id")
        if isinstance(instance_id, str) and instance_id.strip():
            search_payload, search_error = _run_read_only_cli([
                "dcc-mcp-cli", "--output", "json", "--no-auto-gateway", "search",
                "--query", "export layer mask operation", "--dcc-type", "photoshop",
                "--instance-id", instance_id, "--limit", "50",
            ])
            if isinstance(search_payload, dict) and isinstance(search_payload.get("hits"), list):
                tool_names = [
                    hit["slug"] for hit in search_payload["hits"]
                    if isinstance(hit, dict)
                    and hit.get("kind") == "tool"
                    and isinstance(hit.get("slug"), str)
                ]
            if search_error and not dcc_list_error:
                dcc_list_error = search_error
    photoshop_session_discovered = photoshop_session is not None
    tool_discovery_succeeded = photoshop_session_discovered and bool(tool_names)

    bridge_version = bridge_record.get("version")
    if not isinstance(bridge_version, str) or not bridge_version.strip():
        bridge_version = None
    structured_capabilities = all(
        isinstance(capability_record.get(field), bool)
        for field in _BOOLEAN_CAPABILITIES
    ) and isinstance(capability_record.get("backend_locality"), str)
    generative_backend_healthy = generative_backend_record.get("healthy")
    generative_backend_locality = generative_backend_record.get("locality")

    discovery = {
        "selected_bridge": "dcc-mcp-photoshop",
        "photoshop_installed": photoshop_path is not None,
        "photoshop_path": photoshop_path,
        "photoshop_version": photoshop_version,
        "photoshop_running": _process_running(r"Adobe Photoshop.*\.app/Contents/MacOS"),
        "rustc_available": rustc_path is not None,
        "rustc_path": rustc_path,
        "cargo_available": cargo_path is not None,
        "cargo_path": cargo_path,
        "adobepy_cli_available": adobepy_path is not None,
        "adobepy_path": adobepy_path,
        "dcc_mcp_photoshop_available": adapter_path is not None,
        "dcc_mcp_photoshop_path": adapter_path,
        "dcc_mcp_cli_available": dcc_cli_path is not None,
        "dcc_mcp_cli_path": dcc_cli_path,
        "broker_url": broker_url,
        "broker_reachable": broker_payload is not None,
        "broker_error": broker_error,
        "uxp_developer_tool_installed": uxp_tool_path is not None,
        "uxp_developer_tool_path": uxp_tool_path,
        "uxp_bridge_staged": bridge_manifest is not None,
        "uxp_bridge_manifest": bridge_manifest,
        "broker_bridge_connected": bridge_connected,
        "uxp_bridge_loaded": bridge_loaded,
        "photoshop_developer_mode_enabled": developer_mode,
        "photoshop_developer_mode_evidence_source": developer_mode_source,
        "photoshop_developer_mode_probe_marker": (
            developer_mode_probe.get("probe_marker")
            if isinstance(developer_mode_probe, dict)
            else None
        ),
        "mcp_photoshop_session_discovered": photoshop_session_discovered,
        "mcp_tool_discovery_succeeded": tool_discovery_succeeded,
        "mcp_discovered_tools": tool_names,
        "mcp_list_error": dcc_list_error,
        "structured_capability_payload": structured_capabilities,
        "structured_generative_backend_record": (
            isinstance(generative_backend_healthy, bool)
            and isinstance(generative_backend_locality, str)
            and bool(generative_backend_locality.strip())
        ),
    }

    blockers = []
    if photoshop_path is None:
        blockers.append("Adobe Photoshop application installation")
    if rustc_path is None:
        blockers.append("rustc executable")
    if cargo_path is None:
        blockers.append("cargo executable")
    if adobepy_path is None:
        blockers.append("adobepy CLI (`cargo install adobepy-cli`)")
    if adapter_path is None:
        blockers.append("dcc-mcp-photoshop adapter executable")
    if uxp_tool_path is None:
        blockers.append("Adobe UXP Developer Tool application")
    if bridge_manifest is None:
        blockers.append("staged adobepy Photoshop UXP bridge manifest")
    if broker_payload is None:
        blockers.append("healthy `adobepy broker` capability endpoint at {}".format(broker_url))
    if not bridge_connected:
        blockers.append("explicit connected Photoshop bridge record")
    if not bridge_loaded:
        blockers.append("loaded `com.adobepy.bridge.photoshop` UXP bridge")
    if developer_mode is not True:
        blockers.append("verified Photoshop Developer Mode")
    if dcc_cli_path is None:
        blockers.append("dcc-mcp-cli executable")
    if not photoshop_session_discovered:
        blockers.append("explicit connected Photoshop MCP session record")
    if not tool_discovery_succeeded:
        blockers.append("structured non-empty Photoshop MCP tool list")
    if not structured_capabilities:
        blockers.append("structured Photoshop capability payload")
    if not discovery["structured_generative_backend_record"]:
        blockers.append("structured generative-backend health/locality record")

    local_prerequisites_pass = not blockers
    if local_prerequisites_pass:
        return {
            "available": True,
            "bridge_version": bridge_version,
            **{field: capability_record[field] for field in _BOOLEAN_CAPABILITIES},
            "backend_locality": capability_record["backend_locality"],
            "generative_backend_healthy": generative_backend_healthy,
            "generative_backend_locality": generative_backend_locality,
            "discovery": discovery,
        }

    if photoshop_path is None:
        reason = _structured_reason(
            "photoshop_not_installed",
            "Adobe Photoshop application installation",
            "Adobe Photoshop is not installed; bridge health cannot be established.",
            blockers,
        )
    else:
        reason = _structured_reason(
            "photoshop_bridge_prerequisites_missing",
            "dcc-mcp-photoshop health chain",
            "Photoshop is installed, but the selected bridge health chain is incomplete.",
            blockers,
        )

    return {
        "available": False,
        "bridge_version": bridge_version,
        "downgrade_reason": reason,
        "discovery": discovery,
    }


def _normalize_health_payload(payload: Any) -> dict:
    result = _base_health_result()
    if not isinstance(payload, dict):
        result["downgrade_reason"] = _structured_reason(
            "malformed_health_payload",
            "adapter health contract",
            "Adapter health response must be a JSON object.",
            ["payload_not_object"],
        )
        return result

    discovery = payload.get("discovery")
    if isinstance(discovery, dict):
        result["discovery"] = discovery

    if payload.get("available") is False:
        reason = payload.get("downgrade_reason")
        if not isinstance(reason, dict) or not all(
            isinstance(reason.get(field), str) and reason[field].strip()
            for field in ("code", "prerequisite", "message")
        ):
            reason = _structured_reason(
                "adapter_unavailable",
                "classified bridge health response",
                "Adapter reported unavailable without a complete structured downgrade reason.",
            )
        result["downgrade_reason"] = reason
        if isinstance(payload.get("bridge_version"), str) and payload["bridge_version"].strip():
            result["bridge_version"] = payload["bridge_version"].strip()
        return result

    malformed = []
    if payload.get("available") is not True:
        malformed.append("invalid_boolean:available")

    bridge_version = payload.get("bridge_version")
    if not isinstance(bridge_version, str) or not bridge_version.strip():
        malformed.append("invalid_string:bridge_version")

    for field in _BOOLEAN_CAPABILITIES:
        if field not in payload:
            malformed.append("missing:{}".format(field))
        elif not isinstance(payload[field], bool):
            malformed.append("invalid_boolean:{}".format(field))

    locality = payload.get("backend_locality")
    if not isinstance(locality, str) or not locality.strip():
        malformed.append("invalid_string:backend_locality")

    generative_backend_healthy = payload.get("generative_backend_healthy")
    if not isinstance(generative_backend_healthy, bool):
        malformed.append("invalid_boolean:generative_backend_healthy")
    generative_backend_locality = payload.get("generative_backend_locality")
    if not isinstance(generative_backend_locality, str) or not generative_backend_locality.strip():
        malformed.append("invalid_string:generative_backend_locality")

    if malformed:
        result["downgrade_reason"] = _structured_reason(
            "malformed_health_payload",
            "adapter health contract",
            "Adapter health response is missing required fields or has invalid field types.",
            malformed,
        )
        return result

    result["bridge_version"] = bridge_version.strip()
    result["backend_locality"] = locality.strip()
    result["generative_backend_healthy"] = generative_backend_healthy
    result["generative_backend_locality"] = generative_backend_locality.strip()
    for field in _BOOLEAN_CAPABILITIES:
        result[field] = payload[field]

    missing_capabilities = [field for field in _REQUIRED_FINE_EDIT_CAPABILITIES if not result[field]]
    if result["backend_locality"] != "local":
        missing_capabilities.append("backend_locality:local")
    if missing_capabilities:
        result["downgrade_reason"] = _structured_reason(
            "missing_required_capability",
            "fine-edit health gate",
            "The bridge is reachable but cannot prove every required fine-edit capability.",
            missing_capabilities,
        )
        return result

    generative_enabled = (
        result["supports_generative_control"]
        and result["supports_generative_reporting"]
        and result["generative_backend_healthy"]
        and result["generative_backend_locality"] == "local"
        and result["backend_locality"] == "local"
    )
    generative_reason = None
    if not generative_enabled:
        code = (
            "generative_backend_unhealthy"
            if not result["generative_backend_healthy"]
            else "generative_backend_locality_not_allowed"
        )
        generative_reason = _structured_reason(
            code,
            "separate generative-backend health gate",
            "Fine editing is available, but generative operations are disabled.",
        )

    result.update({
        "available": True,
        "fine_edit_mode": True,
        "generative_operations_enabled": generative_enabled,
        "generative_downgrade_reason": generative_reason,
        "mode": "fine-edit",
        "downgrade_reason": None,
        "smoke_test": {
            "eligible": True,
            "status": "not_run",
            "reason": "All bridge health prerequisites passed; an explicit smoke-test invocation is still required.",
        },
    })
    return result


def check_health() -> dict:
    """Return a strict, classified bridge-health result; never edit an image."""

    try:
        return _normalize_health_payload(_discover_adapter_health())
    except Exception as exc:  # defensive API boundary: callers always receive a downgrade
        result = _base_health_result()
        result["downgrade_reason"] = _structured_reason(
            "health_check_error",
            "Photoshop adapter health checker",
            "Health discovery failed before bridge availability could be proven.",
            ["{}: {}".format(type(exc).__name__, exc)],
        )
        return result


def _canonical_manifest_hash(entry: dict) -> str:
    payload = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_local_hash(path_value: Any, claimed_hash: Any, label: str, errors: list[str]) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip() or "://" in path_value:
        errors.append(f"{label}_path_not_local")
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute() or not path.is_file():
        errors.append(f"{label}_path_not_local")
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        errors.append(f"{label}_path_not_local")
        return None
    actual = digest.hexdigest()
    if not isinstance(claimed_hash, str) or claimed_hash != actual:
        errors.append(f"{label}_sha256_mismatch")
    return actual


def normalize_operation_result(payload: dict) -> dict:
    """Validate a masked operation result without trusting adapter assertions."""

    errors = []
    source = payload if isinstance(payload, dict) else {}

    requested_operation_id = source.get("requested_operation_id")
    if not isinstance(requested_operation_id, str) or not requested_operation_id.strip():
        errors.append("missing_requested_operation_id")
        requested_operation_id = None
    else:
        requested_operation_id = requested_operation_id.strip()

    operation_id = source.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id.strip():
        errors.append("invalid_operation_id")
        operation_id = None
    else:
        operation_id = operation_id.strip()

    if (
        requested_operation_id is not None
        and operation_id is not None
        and requested_operation_id != operation_id
    ):
        errors.append("operation_id_mismatch")

    status = source.get("status")
    if not isinstance(status, str) or not status.strip():
        errors.append("invalid_status")
        status = None
    else:
        status = status.strip()

    idempotent_replay = source.get("idempotent_replay")
    if not isinstance(idempotent_replay, bool):
        errors.append("invalid_idempotent_replay")
        idempotent_replay = None

    adapter_allowed_localities = source.get("allowed_localities")
    if adapter_allowed_localities is not None:
        if (
            not isinstance(adapter_allowed_localities, list)
            or not adapter_allowed_localities
            or any(
                not isinstance(item, str) or not item.strip()
                for item in adapter_allowed_localities
            )
        ):
            errors.append("invalid_allowed_localities")
        elif adapter_allowed_localities != ["local"]:
            errors.append("untrusted_allowed_localities_override")

    execution = source.get("execution")
    if not isinstance(execution, dict):
        errors.append("missing_execution_evidence")
        normalized_execution = None
    else:
        normalized_execution = dict(execution)
        for field in ("backend", "software", "software_version", "bridge_version"):
            value = execution.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append("invalid_execution_{}".format(field))
        execution_locality = execution.get("locality")
        if not isinstance(execution_locality, str) or not execution_locality.strip():
            errors.append("invalid_execution_locality")
        elif execution_locality != "local":
            errors.append("execution_locality_not_local")

    document_evidence = source.get("document_evidence")
    if not isinstance(document_evidence, dict):
        errors.append("missing_document_evidence")
        normalized_document = None
    else:
        normalized_document = dict(document_evidence)
        for field in (
            "input_document_id",
            "output_document_id",
            "input_layer_id",
            "output_layer_id",
        ):
            value = document_evidence.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append("invalid_document_{}".format(field))
        if document_evidence.get("editable") is not True:
            errors.append("document_not_editable")
        if document_evidence.get("layered") is not True:
            errors.append("document_not_layered")
        if document_evidence.get("non_destructive") is not True:
            errors.append("document_not_non_destructive")
        if document_evidence.get("flattened") is not False:
            errors.append("document_flattened")

    final_operation = source.get("is_final_operation") is True or source.get("checkpoint_mode") == "final"
    export_evidence = source.get("export_evidence")
    if not final_operation and export_evidence is None:
        # History-mode operations are allowed to defer raster export until the
        # final operation. The document and History checkpoint remain the
        # rollback boundary for the current Photoshop session.
        normalized_export = {"status": "not-run", "reason": "history_mode_final_export_only"}
    elif not isinstance(export_evidence, dict):
        errors.append("missing_export_evidence")
        normalized_export = None
    else:
        normalized_export = dict(export_evidence)
        if not final_operation and export_evidence.get("status") in {"not-run", "omitted", "deferred"}:
            pass
        elif export_evidence.get("status") != "completed":
            errors.append("export_not_completed")
        if final_operation or export_evidence.get("status") == "completed":
            for field in ("path", "sha256", "source_master_sha256"):
                value = export_evidence.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append("invalid_export_{}".format(field))
            if export_evidence.get("format") != "JPEG":
                errors.append("invalid_export_format")
            _verify_local_hash(
                export_evidence.get("path"), export_evidence.get("sha256"), "export", errors
            )

    mask_validation = source.get("mask_validation")
    if not isinstance(mask_validation, dict):
        errors.append("missing_mask_validation")
        normalized_mask = None
    else:
        normalized_mask = dict(mask_validation)
        if mask_validation.get("status") != "valid":
            errors.append("mask_validation_failed")
        if mask_validation.get("dimensions_match") is not True:
            errors.append("mask_dimensions_unverified")
        if mask_validation.get("edges_checked") is not True:
            errors.append("mask_edges_unverified")
        if not isinstance(mask_validation.get("artifact_warnings"), list):
            errors.append("invalid_mask_artifact_warnings")

    generative = source.get("generative")
    if not isinstance(generative, dict):
        errors.append("missing_generative_status")
        normalized_generative = None
    else:
        normalized_generative = dict(generative)
        used = generative.get("used")
        reported = generative.get("reported")
        controlled = generative.get("controlled")
        if not isinstance(used, bool):
            errors.append("invalid_generative_used")
        if reported is not True:
            errors.append("generative_operation_not_reported")
        if controlled is not True:
            errors.append("generative_operation_not_controlled")
        if used is True:
            if generative.get("backend_healthy") is not True:
                errors.append("generative_backend_unhealthy")
            backend_locality = generative.get("backend_locality")
            if not isinstance(backend_locality, str) or not backend_locality.strip():
                errors.append("invalid_generative_backend_locality")
            elif backend_locality != "local":
                errors.append("generative_backend_locality_not_local")
            for field in ("model", "model_version", "prompt"):
                value = generative.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append("missing_generative_{}".format(field))

    operation_manifest = source.get("operation_manifest")
    history_mode = not final_operation and source.get("checkpoint_mode") == "history"
    if not isinstance(operation_manifest, dict):
        errors.append("missing_operation_manifest")
        normalized_manifest = None
    else:
        normalized_manifest = dict(operation_manifest)
        if operation_manifest.get("status") != "valid":
            errors.append("operation_manifest_not_valid")
        manifest_operation_id = operation_manifest.get("operation_id")
        if not isinstance(manifest_operation_id, str) or not manifest_operation_id.strip():
            errors.append("invalid_manifest_operation_id")
        elif operation_id is not None and manifest_operation_id != operation_id:
            errors.append("manifest_operation_id_mismatch")
        manifest_hash = operation_manifest.get("manifest_hash")
        if not isinstance(manifest_hash, str) or not manifest_hash.strip():
            errors.append("invalid_manifest_hash")
        manifest_entry = operation_manifest.get("entry")
        if not isinstance(manifest_entry, dict):
            errors.append("malformed_manifest_entry")
        else:
            entry_operation_id = manifest_entry.get("operation_id")
            if not isinstance(entry_operation_id, str) or not entry_operation_id.strip():
                errors.append("invalid_manifest_entry_operation_id")
            elif operation_id is not None and entry_operation_id != operation_id:
                errors.append("manifest_entry_operation_id_mismatch")
            mask_sha256 = manifest_entry.get("mask_sha256")
            if not isinstance(mask_sha256, str) or not mask_sha256.strip():
                errors.append("invalid_manifest_mask_sha256")
            depends_on = manifest_entry.get("depends_on")
            if (
                not isinstance(depends_on, list)
                or any(not isinstance(item, str) or not item.strip() for item in depends_on)
            ):
                errors.append("invalid_manifest_entry_depends_on")
            manifest_fields = [
                "type",
                "region",
                "mask_reference",
                "input_layer_id",
                "output_layer_id",
                "before_path",
                "before_sha256",
            ]
            if not history_mode:
                manifest_fields.extend(["after_path", "after_sha256"])
            for field in manifest_fields:
                value = manifest_entry.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append("invalid_manifest_entry_{}".format(field))
            if history_mode:
                history_snapshot_name = manifest_entry.get("history_snapshot_name") or source.get("history_snapshot_name")
                if not isinstance(history_snapshot_name, str) or not history_snapshot_name.strip():
                    errors.append("missing_history_snapshot_name")
            if not isinstance(manifest_entry.get("parameters"), dict):
                errors.append("invalid_manifest_entry_parameters")
            if manifest_entry.get("risk") not in ("low", "medium", "high"):
                errors.append("invalid_manifest_entry_risk")
            manifest_generative = manifest_entry.get("generative")
            if not isinstance(manifest_generative, bool):
                errors.append("invalid_manifest_entry_generative")
            elif isinstance(generative, dict) and manifest_generative is not generative.get("used"):
                errors.append("manifest_generative_status_mismatch")
            if manifest_generative is True and isinstance(generative, dict):
                for field in ("model", "model_version", "prompt"):
                    if manifest_entry.get(field) != generative.get(field):
                        errors.append("manifest_generative_{}_mismatch".format(field))
            if isinstance(document_evidence, dict):
                for field in ("input_layer_id", "output_layer_id"):
                    if manifest_entry.get(field) != document_evidence.get(field):
                        errors.append("manifest_{}_mismatch".format(field))
            actual_manifest_hash = _canonical_manifest_hash(manifest_entry)
            if manifest_hash != actual_manifest_hash:
                errors.append("manifest_hash_mismatch")
            _verify_local_hash(
                manifest_entry.get("mask_reference"), manifest_entry.get("mask_sha256"),
                "mask", errors,
            )
            _verify_local_hash(
                manifest_entry.get("before_path"), manifest_entry.get("before_sha256"),
                "before", errors,
            )
            after_actual = None
            if manifest_entry.get("after_path") or manifest_entry.get("after_sha256"):
                after_actual = _verify_local_hash(
                    manifest_entry.get("after_path"), manifest_entry.get("after_sha256"),
                    "after", errors,
                )
            if isinstance(export_evidence, dict) and export_evidence.get("status") == "completed":
                source_master_hash = export_evidence.get("source_master_sha256")
                if after_actual is None or source_master_hash != after_actual:
                    errors.append("export_source_master_sha256_mismatch")

    warnings = source.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        errors.append("invalid_warnings")
        warnings = []

    valid = not errors
    return {
        "valid": valid,
        "classification": "valid_operation_result" if valid else "invalid_operation_result",
        "requested_operation_id": requested_operation_id,
        "operation_id": operation_id,
        "status": status,
        "idempotent_replay": idempotent_replay,
        "execution": normalized_execution,
        "document_evidence": normalized_document,
        "export_evidence": normalized_export,
        "mask_validation": normalized_mask,
        "generative": normalized_generative,
        "operation_manifest": normalized_manifest,
        "warnings": list(warnings),
        "errors": errors,
    }


def main() -> int:
    result = check_health()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["available"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
