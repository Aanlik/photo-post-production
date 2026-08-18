"""Content-addressed identities for local analysis cache entries."""

from __future__ import annotations

import hashlib
import json


def cache_key(
    source_hash: str,
    config_hash: str,
    skill_version: str,
    tool_versions: dict,
    model_version: str | None,
) -> str:
    """Return a SHA-256 key that changes for every analysis dependency version."""
    payload = {
        "config_hash": config_hash,
        "model_version": model_version,
        "skill_version": skill_version,
        "source_hash": source_hash,
        "tool_versions": tool_versions,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
