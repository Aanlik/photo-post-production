"""Capability probes and explicit downgrade labels for optional backends."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from chat_window_image_backend import probe_chat_window_image_backend


def probe_generative_backend(
    timeout: int = 10,
    processing_locality: str | None = None,
    network: bool = True,
) -> dict[str, Any]:
    """Probe the selected backend without uploading pixels.

    ``local-only`` keeps the historical command-based local probe. In Hybrid
    mode, generation is routed to the host-managed built-in image_gen tool.
    The OpenAI API route is intentionally disabled for this project.
    """
    locality_policy = processing_locality or os.environ.get("PHOTO_PROCESSING_LOCALITY", "local-only").strip()
    requested_backend = os.environ.get("PHOTO_GENERATIVE_BACKEND", "chat-window").strip().casefold()
    if locality_policy in {"mixed", "mixed-locality", "allow-cloud-generation"}:
        if requested_backend in {"openai", "openai-image", "chatgpt-image", "api"}:
            return {
                "backend": "openai-image-api",
                "api_mode": True,
                "available": False,
                "planning_eligible": False,
                "ready_for_execution": False,
                "healthy": False,
                "verified": False,
                "locality": "cloud-api",
                "requires_host_tool": False,
                "reason": "api_backend_disabled_by_user_chat_window_mode",
            }
        return probe_chat_window_image_backend()
    command = os.environ.get("PHOTO_GENERATIVE_HEALTH_COMMAND", "").strip()
    if not command:
        return {
            "available": False,
            "healthy": False,
            "backend": None,
            "locality": "unknown",
            "reason": "no_independently_verified_local_generative_backend",
        }
    try:
        completed = subprocess.run(command.split(), check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "healthy": False, "backend": command, "locality": "unknown", "reason": f"health_probe_failed:{type(error).__name__}"}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"available": False, "healthy": False, "backend": command, "locality": "unknown", "reason": "health_probe_invalid_json"}
    healthy = payload.get("healthy") is True
    locality = str(payload.get("locality", "unknown"))
    eligible = completed.returncode == 0 and healthy and locality == "local"
    return {
        "available": eligible,
        "healthy": healthy,
        "backend": payload.get("backend", command),
        "version": payload.get("version"),
        "locality": locality,
        "reason": None if eligible else "backend_health_or_locality_gate_failed",
    }


def downgrade_for_capabilities(requested: list[str], fine_edit: dict[str, Any] | None = None, generative: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a structured plan status; never silently call unsupported tools."""
    requested = [str(item) for item in requested]
    fine_edit = fine_edit or {"available": False, "reason": "photoshop_fine_edit_not_probed"}
    generative = generative or probe_generative_backend()
    unsupported = []
    if any(item in {"liquify", "healing", "clone_stamp", "dodge_burn", "transform_warp", "perspective_warp"} for item in requested) and not fine_edit.get("available"):
        unsupported.extend(item for item in requested if item in {"liquify", "healing", "clone_stamp", "dodge_burn", "transform_warp", "perspective_warp"})
    if any(item.startswith("generative") or item in {"remove-element", "add-element", "replace-sky-or-background"} for item in requested) and not generative.get("available"):
        unsupported.extend(item for item in requested if item.startswith("generative") or item in {"remove-element", "add-element", "replace-sky-or-background"})
    return {
        "requested": requested,
        "unsupported": sorted(set(unsupported)),
        "label": "global-only" if unsupported else "fine-edit-eligible",
        "generative": generative,
        "fine_edit": fine_edit,
        "reasons": [f"unsupported:{item}" for item in sorted(set(unsupported))],
    }
