"""Versioned, normalized category-specific photo scoring weights."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


WEIGHTS_VERSION = "category-weights-2026.08.17-v1"

_RAW_PROFILES = {
    "landscape-nature": {"technical": 0.20, "composition": 0.30, "light_color": 0.25, "moment_story": 0.10, "coherence": 0.15},
    "urban-landscape": {"technical": 0.20, "composition": 0.30, "light_color": 0.25, "moment_story": 0.10, "coherence": 0.15},
    "architecture-urban-space": {"technical": 0.20, "composition": 0.35, "light_color": 0.25, "moment_story": 0.05, "coherence": 0.15},
    "street-documentary": {"technical": 0.18, "composition": 0.20, "light_color": 0.14, "moment_story": 0.34, "coherence": 0.14},
    "portrait-environmental": {"technical": 0.24, "composition": 0.19, "light_color": 0.19, "moment_story": 0.19, "coherence": 0.19},
    "animal-wildlife": {"technical": 0.24, "composition": 0.22, "light_color": 0.18, "moment_story": 0.22, "coherence": 0.14},
    "plant-macro": {"technical": 0.28, "composition": 0.24, "light_color": 0.22, "moment_story": 0.08, "coherence": 0.18},
    "other-unsupported": {"technical": 0.20, "composition": 0.25, "light_color": 0.20, "moment_story": 0.20, "coherence": 0.15},
}


def _normalize(weights: dict[str, Any]) -> dict[str, float]:
    parsed = {str(key): max(0.0, float(value)) for key, value in weights.items()}
    total = sum(parsed.values())
    if total <= 0:
        raise ValueError("category weights must contain positive values")
    return {key: value / total for key, value in parsed.items()}


def get_category_profile(category: str) -> dict[str, Any]:
    selected = category if category in _RAW_PROFILES else "other-unsupported"
    return {
        "category": selected,
        "requested_category": category,
        "version": WEIGHTS_VERSION,
        "weights": _normalize(deepcopy(_RAW_PROFILES[selected])),
    }


def category_weights(category: str) -> dict[str, float]:
    return get_category_profile(category)["weights"]
