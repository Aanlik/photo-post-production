"""Create and seal metadata-only trust roots for local edit runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_INTENTS = {
    "documentary-truthful",
    "natural-enhancement",
    "editorial-expression",
    "competition-standard",
    "commercial/creative",
}
ALLOWED_AUTHORITIES = {"full"}
ALLOWED_LOCALITY_POLICIES = {"local-only", "mixed", "mixed-locality", "allow-cloud-generation", "remote"}

_INTENT_BUDGETS = {
    "documentary-truthful": (1.0, 0.15, 1200, 80, 5),
    "natural-enhancement": (1.5, 0.25, 2000, 100, 8),
    "editorial-expression": (2.5, 0.45, 3500, 140, 20),
    "competition-standard": (2.0, 0.35, 3000, 120, 15),
    "commercial/creative": (3.0, 0.60, 5000, 150, 30),
}
_REQUIRED_BUDGET_FIELDS = {
    "max_global_adjustments",
    "max_local_adjustments",
    "max_transformative_operations",
    "max_exposure_delta",
    "max_crop_fraction",
    "max_temperature_delta",
    "max_sharpening",
    "max_geometry_delta",
    "max_candidates",
}


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _snapshot_digest(assets: list[dict]) -> str:
    identities = [{"path": asset["path"], "sha256": asset["sha256"]} for asset in assets]
    return hashlib.sha256(_canonical_json(identities)).hexdigest()


def _source_assets(input_path: Path) -> list[dict]:
    """Snapshot regular files under the selected root without following symlinks."""
    if input_path.is_file() and not input_path.is_symlink():
        resolved = input_path.resolve()
        stat = resolved.stat()
        return [{
            "path": str(resolved),
            "sha256": _sha256(resolved),
            "size_bytes": stat.st_size,
            "read_only": True,
        }]
    if not input_path.is_dir():
        return []
    assets: list[dict] = []
    for directory, directory_names, file_names in os.walk(input_path, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names
            if not (directory_path / name).is_symlink()
        )
        for name in sorted(file_names):
            source = directory_path / name
            if source.is_symlink() or not source.is_file():
                continue
            resolved = source.resolve()
            try:
                resolved.relative_to(input_path)
            except ValueError:
                continue
            stat = resolved.stat()
            assets.append({
                "path": str(resolved),
                "sha256": _sha256(resolved),
                "size_bytes": stat.st_size,
                "read_only": True,
            })
    return sorted(assets, key=lambda asset: asset["path"])


def _default_budget(intent: str) -> dict:
    exposure, crop, temperature, sharpening, geometry = _INTENT_BUDGETS[intent]
    return {
        "max_global_adjustments": 12,
        "max_local_adjustments": 8,
        "max_transformative_operations": 0,
        "max_exposure_delta": exposure,
        "max_crop_fraction": crop,
        "max_temperature_delta": temperature,
        "max_sharpening": sharpening,
        "max_geometry_delta": geometry,
        "max_candidates": 4,
    }


def _valid_budget(budget: Any) -> bool:
    return (
        isinstance(budget, dict)
        and _REQUIRED_BUDGET_FIELDS.issubset(budget)
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and value >= 0
            for value in budget.values()
        )
        and 0 <= budget["max_crop_fraction"] <= 1
    )


def _trust_root_digest(context: dict) -> str:
    unsigned = deepcopy(context)
    unsigned.pop("trust_root_digest", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def create_run_manifest(run_id: str, input_dir: str, intent: str, authority: str) -> dict:
    """Return an unsealed manifest; configure policy, then call ``seal_run_manifest``."""
    run_id = _non_empty(run_id, "run_id")
    input_dir = _non_empty(input_dir, "input_dir")
    intent = _non_empty(intent, "intent")
    authority = _non_empty(authority, "authority")
    if intent not in ALLOWED_INTENTS:
        raise ValueError(f"unsupported intent: {intent}")
    if authority not in ALLOWED_AUTHORITIES:
        raise ValueError(f"unsupported authority: {authority}")
    source_root = Path(input_dir).expanduser().resolve()
    assets = _source_assets(source_root)
    source_snapshot_sha256 = _snapshot_digest(assets)
    primary_source = assets[0] if len(assets) == 1 else None
    intent_budget = _default_budget(intent)
    trusted_context = {
        "run_id": run_id,
        "input_dir": str(source_root),
        "intent": intent,
        "edit_authority": authority,
        "source_path": primary_source["path"] if primary_source else None,
        "source_sha256": primary_source["sha256"] if primary_source else None,
        "source_assets": deepcopy(assets),
        "source_snapshot_sha256": source_snapshot_sha256,
        "source_snapshot_immutable": True,
        "intent_budget": deepcopy(intent_budget),
        "locality_policy": "local-only",
    }
    return {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "sealed_at": None,
        "sealed": False,
        "trusted_context_digest": None,
        "input_dir": str(source_root),
        "intent": intent,
        "edit_authority": authority,
        "director_brief": {},
        "creative_controls": {},
        "processing_locality": "local-only",
        "locality_policy": "local-only",
        "resource_budget": {"max_iterations": 3},
        "source_assets": deepcopy(assets),
        "asset_group_ids": [],
        "source_path": trusted_context["source_path"],
        "source_sha256": trusted_context["source_sha256"],
        "source_snapshot_sha256": source_snapshot_sha256,
        "source_snapshot_immutable": True,
        "intent_budget": deepcopy(intent_budget),
        "trusted_context": trusted_context,
        "edit_graph": {"operations": [], "operation_records": []},
        "operation_ids": [],
        "provenance": [],
        "adapter_status": {},
        "iteration_records": [],
        "checkpoints": [],
        "before_path": None,
        "after_path": None,
        "score_deltas": [],
        "transformation_level": None,
        "stopping_reason": None,
        "final_label": None,
    }


def seal_run_manifest(manifest: dict) -> dict:
    """Finalize a manifest and attach a tamper-evident digest to its trust root."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    sealed = deepcopy(manifest)
    intent = sealed.get("intent")
    authority = sealed.get("edit_authority")
    if intent not in ALLOWED_INTENTS:
        raise ValueError("manifest intent is not supported")
    if authority not in ALLOWED_AUTHORITIES:
        raise ValueError("manifest authority is not supported")
    policy = sealed.get("locality_policy")
    if policy not in ALLOWED_LOCALITY_POLICIES:
        raise ValueError("manifest locality_policy is not supported")
    budget = sealed.get("intent_budget")
    if not _valid_budget(budget):
        raise ValueError("manifest intent_budget is incomplete or invalid")
    source_root = Path(str(sealed.get("input_dir", ""))).expanduser().resolve()
    current_assets = _source_assets(source_root)
    if current_assets != sealed.get("source_assets"):
        raise ValueError("source snapshot changed before sealing")
    snapshot_digest = _snapshot_digest(current_assets)
    if sealed.get("source_snapshot_sha256") != snapshot_digest:
        raise ValueError("source snapshot digest changed before sealing")
    primary_source = current_assets[0] if len(current_assets) == 1 else None
    context = {
        "run_id": sealed.get("run_id"),
        "input_dir": str(source_root),
        "intent": intent,
        "edit_authority": authority,
        "source_path": primary_source["path"] if primary_source else None,
        "source_sha256": primary_source["sha256"] if primary_source else None,
        "source_assets": deepcopy(current_assets),
        "source_snapshot_sha256": snapshot_digest,
        "source_snapshot_immutable": True,
        "intent_budget": deepcopy(budget),
        "locality_policy": policy,
    }
    digest = _trust_root_digest(context)
    context["trust_root_digest"] = digest
    sealed.update({
        "sealed": True,
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "trusted_context_digest": digest,
        "trusted_context": context,
        "source_path": context["source_path"],
        "source_sha256": context["source_sha256"],
    })
    return sealed


def verify_run_manifest(manifest: Any) -> bool:
    """Return whether a sealed manifest and its duplicated policy fields are intact."""
    if not isinstance(manifest, dict) or manifest.get("sealed") is not True:
        return False
    context = manifest.get("trusted_context")
    digest = manifest.get("trusted_context_digest")
    if not isinstance(context, dict) or not isinstance(digest, str) or not digest:
        return False
    if context.get("trust_root_digest") != digest or _trust_root_digest(context) != digest:
        return False
    mirrored = {
        "run_id": "run_id",
        "input_dir": "input_dir",
        "intent": "intent",
        "edit_authority": "edit_authority",
        "source_path": "source_path",
        "source_sha256": "source_sha256",
        "source_assets": "source_assets",
        "source_snapshot_sha256": "source_snapshot_sha256",
        "source_snapshot_immutable": "source_snapshot_immutable",
        "intent_budget": "intent_budget",
        "locality_policy": "locality_policy",
    }
    return all(manifest.get(top) == context.get(inner) for top, inner in mirrored.items())


def verify_trusted_context(context: Any) -> bool:
    """Validate a detached sealed context used by the quality gate."""
    if not isinstance(context, dict):
        return False
    digest = context.get("trust_root_digest")
    return isinstance(digest, str) and bool(digest) and _trust_root_digest(context) == digest
