"""Build bounded, category-aware Lightroom color settings.

The planner produces only settings supported by the pinned Lightroom MCP.
It deliberately preserves camera white balance when no measured source value
is available; inventing an absolute Kelvin value is less safe than a neutral
as-shot render.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any


CHANNELS = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")
DIMENSIONS = ("hue", "saturation", "luminance")

_VARIANT_STRENGTH = {
    "natural": 0.60,
    "editorial": 0.82,
    "competition-standard": 1.0,
}

_CATEGORY_STYLES: dict[str, dict[str, Any]] = {
    "portrait-environmental": {
        "rationale": "Protect red/orange skin while reducing competing green and blue background color.",
        "skin_tone_protection": True,
        "hsl": {
            "red": {"hue": 0, "saturation": -3, "luminance": 2},
            "orange": {"hue": -2, "saturation": -2, "luminance": 6},
            "yellow": {"hue": -2, "saturation": -5, "luminance": 1},
            "green": {"hue": -2, "saturation": -9, "luminance": -1},
            "aqua": {"hue": -2, "saturation": -5, "luminance": -1},
            "blue": {"hue": -3, "saturation": -7, "luminance": -3},
        },
    },
    "landscape-nature": {
        "rationale": "Separate blue/aqua depth while keeping foliage restrained and believable.",
        "skin_tone_protection": False,
        "hsl": {
            "yellow": {"hue": -2, "saturation": -3, "luminance": 2},
            "green": {"hue": -5, "saturation": -6, "luminance": 3},
            "aqua": {"hue": -5, "saturation": 4, "luminance": -2},
            "blue": {"hue": -4, "saturation": 7, "luminance": -6},
            "purple": {"saturation": -3},
        },
    },
    "urban-landscape": {
        "rationale": "Control mixed urban color while retaining useful blue-hour and material separation.",
        "skin_tone_protection": False,
        "hsl": {
            "orange": {"saturation": -2, "luminance": 2},
            "yellow": {"hue": -2, "saturation": -7, "luminance": -1},
            "green": {"saturation": -8},
            "aqua": {"hue": -3, "saturation": 1, "luminance": -2},
            "blue": {"hue": -3, "saturation": 3, "luminance": -5},
            "purple": {"saturation": -7},
            "magenta": {"saturation": -7},
        },
    },
    "street-documentary": {
        "rationale": "Preserve observed light and suppress distracting yellow/green contamination.",
        "skin_tone_protection": True,
        "hsl": {
            "red": {"saturation": -2, "luminance": 1},
            "orange": {"saturation": -3, "luminance": 2},
            "yellow": {"hue": -2, "saturation": -9, "luminance": -2},
            "green": {"saturation": -11, "luminance": -2},
            "aqua": {"saturation": -4},
            "blue": {"saturation": -3, "luminance": -2},
        },
    },
    "architecture-urban-space": {
        "rationale": "Build a deliberate red/cyan night palette while suppressing dirty purple, magenta, green, and sodium-light pollution.",
        "skin_tone_protection": False,
        "hsl": {
            "red": {"hue": 2, "saturation": 8, "luminance": -2},
            "orange": {"hue": -2, "saturation": 3, "luminance": 3},
            "yellow": {"hue": -4, "saturation": -14, "luminance": -3},
            "green": {"hue": 6, "saturation": -20, "luminance": -3},
            "aqua": {"hue": -8, "saturation": -16, "luminance": -6},
            "blue": {"hue": -6, "saturation": -20, "luminance": -9},
            "purple": {"saturation": -30, "luminance": -6},
            "magenta": {"saturation": -32, "luminance": -6},
        },
    },
    "animal-wildlife": {
        "rationale": "Protect subject fur/feather color and reduce foliage competition behind the animal.",
        "skin_tone_protection": False,
        "hsl": {
            "red": {"saturation": -2, "luminance": 1},
            "orange": {"hue": -1, "saturation": 1, "luminance": 3},
            "yellow": {"saturation": -5, "luminance": 1},
            "green": {"hue": -3, "saturation": -9, "luminance": -2},
            "aqua": {"saturation": -4},
            "blue": {"saturation": -3, "luminance": -2},
        },
    },
    "plant-macro": {
        "rationale": "Separate yellow and green hues without producing fluorescent foliage.",
        "skin_tone_protection": False,
        "hsl": {
            "red": {"saturation": -2},
            "orange": {"saturation": -2, "luminance": 1},
            "yellow": {"hue": 5, "saturation": -9, "luminance": 2},
            "green": {"hue": -8, "saturation": -6, "luminance": 4},
            "aqua": {"hue": -3, "saturation": -4, "luminance": -1},
            "blue": {"saturation": -4},
        },
    },
    "other-unsupported": {
        "rationale": "Use a neutral color baseline because semantic classification is unsupported or uncertain.",
        "skin_tone_protection": False,
        "hsl": {},
    },
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clamp(value: Any, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, _finite(value)))


def _recipe_lightroom(style_recipe: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(style_recipe, dict):
        return {}
    recipe = style_recipe.get("recipe")
    if not isinstance(recipe, dict):
        return {}
    lightroom = recipe.get("lightroom")
    return lightroom if isinstance(lightroom, dict) else {}


def _measured_white_balance(score: dict[str, Any]) -> tuple[float | None, float | None]:
    candidates = [
        score.get("white_balance"),
        score.get("technical_metrics", {}).get("white_balance") if isinstance(score.get("technical_metrics"), dict) else None,
        score.get("metadata", {}).get("white_balance") if isinstance(score.get("metadata"), dict) else None,
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        temperature = _finite(candidate.get("temperature"), -1.0)
        tint = _finite(candidate.get("tint"), 0.0)
        if 2000.0 <= temperature <= 50000.0:
            return temperature, tint
    return None, None


def _build_hsl(base: dict[str, Any], learned: dict[str, Any], strength: float) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for channel in CHANNELS:
        base_channel = base.get(channel) if isinstance(base.get(channel), dict) else {}
        learned_channel = learned.get(channel) if isinstance(learned.get(channel), dict) else {}
        values: dict[str, float] = {}
        for dimension in DIMENSIONS:
            value = _finite(base_channel.get(dimension)) * strength + _finite(learned_channel.get(dimension))
            value = round(_clamp(value, -40.0, 40.0), 2)
            if abs(value) >= 0.01:
                values[dimension] = value
        if values:
            output[channel] = values
    return output


def build_color_style(
    score: dict[str, Any],
    style_recipe: dict[str, Any] | None = None,
    variant: str = "natural",
) -> dict[str, Any]:
    """Return executable color settings plus a transparent strategy record."""

    score = score if isinstance(score, dict) else {}
    category = str(score.get("primary_category", "other-unsupported"))
    category_style = deepcopy(_CATEGORY_STYLES.get(category, _CATEGORY_STYLES["other-unsupported"]))
    recipe = _recipe_lightroom(style_recipe)
    strength = _VARIANT_STRENGTH.get(variant, _VARIANT_STRENGTH["natural"])
    learned_hsl = recipe.get("hsl") if isinstance(recipe.get("hsl"), dict) else {}
    result: dict[str, Any] = {
        "hsl": _build_hsl(category_style["hsl"], learned_hsl, strength),
        "strategy": {
            "version": "category-color-v2",
            "category": category,
            "variant": variant,
            "strength": strength,
            "rationale": category_style["rationale"],
            "skin_tone_protection": bool(category_style["skin_tone_protection"]),
            "white_balance_basis": "preserve-camera-as-shot",
            "learned_style_applied": bool(recipe),
            "unsupported_color_features": ["three-way-color-grading", "camera-profile", "lut"],
        },
    }

    temperature, tint = _measured_white_balance(score)
    if temperature is not None:
        result["white_balance"] = "Custom"
        result["temperature"] = round(_clamp(temperature + _finite(recipe.get("temperature_bias")), 2000.0, 50000.0))
        result["tint"] = round(_clamp((tint or 0.0) + _finite(recipe.get("tint_bias")), -150.0, 150.0), 2)
        result["strategy"]["white_balance_basis"] = "measured-source-plus-style-bias"
    return result
