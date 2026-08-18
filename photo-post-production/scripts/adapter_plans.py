"""Translate score/style evidence into bounded cross-application plans."""

from __future__ import annotations

from typing import Any

from color_style import build_color_style


def _context_boxes(score: dict[str, Any]) -> list[dict[str, float]]:
    evidence = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    raw_boxes = (
        evidence.get("secondary_subject_boxes_display")
        or evidence.get("person_boxes_display")
        or evidence.get("human_boxes_display")
        or []
    )
    boxes: list[dict[str, float]] = []
    for box in raw_boxes if isinstance(raw_boxes, list) else []:
        if not isinstance(box, dict):
            continue
        try:
            parsed = {key: float(box[key]) for key in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError):
            continue
        if parsed["width"] > 0 and parsed["height"] > 0:
            boxes.append(parsed)
    return boxes


def _crop_context_guard(score: dict[str, Any], crop: dict[str, Any]) -> dict[str, Any]:
    """Allow an aesthetic crop while protecting meaningful context.

    Environmental portraits often contain people that Vision can label but
    cannot localize reliably.  Treating that as "never crop" produces weak
    compositions with dead headroom.  A crop is still allowed without boxes
    when it is demonstrably headroom-dominant, trims only modestly from the
    sides, keeps the lower gesture/body, and does not pretend to remove a
    specific person.
    """
    tags = {str(item).casefold() for item in score.get("secondary_tags", []) if item}
    evidence = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    labels = {
        str(item[0]).casefold()
        for item in (evidence.get("labels") or [])
        if isinstance(item, (list, tuple)) and item
    }
    context_people = bool(tags & {"people", "crowd"}) or bool(labels & {"people", "crowd", "group"})
    boxes = _context_boxes(score)
    if not context_people and not boxes:
        return {"preserve_context": False, "reason": "single_subject_crop_allowed"}
    if not boxes:
        bounds = crop["crop_bounds"]
        removed_top = float(bounds["top"])
        removed_bottom = 1.0 - float(bounds["bottom"])
        horizontal_trim = float(bounds["left"]) + (1.0 - float(bounds["right"]))
        headroom_dominant = (
            removed_top >= 0.16
            and removed_top >= max(0.05, removed_bottom * 1.8)
            and float(bounds["bottom"]) >= 0.88
            and horizontal_trim <= 0.28
        )
        if headroom_dominant:
            return {
                "preserve_context": False,
                "reason": "headroom_dominant_crop_reduces_empty_space_without_targeting_context",
                "policy": "automatic-aesthetic-crop-headroom-only",
                "protection": "no_specific_secondary_subject_removal_claim",
            }
        return {
            "preserve_context": True,
            "reason": "secondary_people_detected_without_safe_boxes",
            "policy": "preserve-or-review",
        }

    bounds = crop["crop_bounds"]
    left, top = float(bounds["left"]), float(bounds["top"])
    right, bottom = float(bounds["right"]), float(bounds["bottom"])
    for box in boxes:
        box_left, box_top = box["x"], box["y"]
        box_right, box_bottom = box_left + box["width"], box_top + box["height"]
        intersection = max(0.0, min(right, box_right) - max(left, box_left)) * max(
            0.0, min(bottom, box_bottom) - max(top, box_top)
        )
        box_area = box["width"] * box["height"]
        if intersection > 0.0 and intersection < box_area * 0.90:
            return {
                "preserve_context": True,
                "reason": "crop_would_cut_secondary_subject",
                "policy": "preserve-or-review",
            }
    return {
        "preserve_context": False,
        "reason": "secondary_subjects_whole_or_excluded",
        "policy": "automatic-aesthetic-crop",
    }


def _face_geometry(score: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Turn the strongest local-Vision face box into PS mask/crop geometry.

    Vision boxes use a lower-left origin; Photoshop's normalized selections use
    a top-left origin.  No face evidence means no face-specific operation.
    """
    evidence = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    display_boxes = evidence.get("face_boxes_display") if isinstance(evidence.get("face_boxes_display"), list) else []
    boxes = display_boxes or (evidence.get("face_boxes") if isinstance(evidence.get("face_boxes"), list) else [])
    boxes_are_display_top_left = bool(display_boxes)
    parsed: list[dict[str, float]] = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        try:
            x, y = float(box["x"]), float(box["y"])
            width, height = float(box["width"]), float(box["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        parsed.append({"x": x, "y": y, "width": width, "height": height})
    if not parsed:
        return {}, None, {"preserve_context": False, "reason": "no_face_geometry"}
    face = max(parsed, key=lambda item: item["width"] * item["height"])
    center_x = min(1.0, max(0.0, face["x"] + face["width"] / 2.0))
    center_y = min(1.0, max(0.0, face["y"] + face["height"] / 2.0)) if boxes_are_display_top_left else min(1.0, max(0.0, 1.0 - face["y"] - face["height"] / 2.0))
    # Keep the face mask genuinely local.  The previous expansion covered a
    # large fraction of the frame, so "face" adjustments visibly leaked into
    # the street and weakened subject/background separation.
    pad_x = max(0.025, face["width"] * 0.55)
    pad_y = max(0.040, face["height"] * 0.65)
    mask = {
        "mask_kind": "ellipse",
        "mask_bounds": {
            "left": round(max(0.0, center_x - pad_x), 5),
            "top": round(max(0.0, center_y - pad_y), 5),
            "right": round(min(1.0, center_x + pad_x), 5),
            "bottom": round(min(1.0, center_y + pad_y), 5),
        },
        "mask_units": "normalized",
        "feather": 18.0,
    }
    # Small faces usually indicate an environmental/full-body portrait. Use
    # a genuine 4:5 delivery crop, put the face near the upper fifth, and keep
    # the lower gesture/body.  This removes empty headroom without shoving the
    # subject to the frame edge. Close portraits keep a slightly tighter crop.
    display_dimensions = evidence.get("display_dimensions") if isinstance(evidence.get("display_dimensions"), list) else []
    try:
        display_aspect = float(display_dimensions[0]) / float(display_dimensions[1])
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        display_aspect = 2.0 / 3.0
    target_aspect = 4.0 / 5.0
    # Environmental portraits benefit from a decisive 4:5-ish crop: keep the
    # gesture, bag and street cues, but remove dead sky/headroom and edge
    # fragments.  Close portraits remain tighter.
    crop_width = 0.76 if face["height"] < 0.16 else 0.74
    crop_height = min(0.92, crop_width * display_aspect / target_aspect)
    tags = {str(item).casefold() for item in score.get("secondary_tags", []) if item}
    labels = {
        str(item[0]).casefold()
        for item in (evidence.get("labels") or [])
        if isinstance(item, (list, tuple)) and item
    }
    context_people = bool(tags & {"people", "crowd"}) or bool(labels & {"people", "crowd", "group"})
    has_safe_context_boxes = bool(_context_boxes(score))
    desired_face_y = (
        0.22 if context_people and not has_safe_context_boxes
        else 0.20 if face["height"] < 0.16
        else 0.32
    )
    left = min(max(0.0, center_x - crop_width * 0.50), 1.0 - crop_width)
    # When the detector knows there is a crowd but cannot safely box the
    # secondary people, bias the crop away from the right edge.  This avoids
    # leaving an accidental half-person at the boundary while retaining the
    # primary face, gesture and environmental story.
    if context_people and not has_safe_context_boxes and left + crop_width > 0.90:
        left = max(0.0, 0.90 - crop_width)
    top = min(max(0.0, center_y - crop_height * desired_face_y), 1.0 - crop_height)
    crop = {
        "crop_bounds": {
            "left": round(left, 5),
            "top": round(top, 5),
            "right": round(left + crop_width, 5),
            "bottom": round(top + crop_height, 5),
        },
        "crop_units": "normalized",
    }
    guard = _crop_context_guard(score, crop)
    if guard["preserve_context"]:
        return mask, None, guard
    return mask, crop, guard


def _portrait_subject_mask(face_mask: dict[str, Any]) -> dict[str, Any]:
    """Expand verified face geometry into a conservative full-subject rectangle."""
    bounds = face_mask.get("mask_bounds") if isinstance(face_mask.get("mask_bounds"), dict) else {}
    try:
        left, top = float(bounds["left"]), float(bounds["top"])
        right, bottom = float(bounds["right"]), float(bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return {}
    face_width, face_height = right - left, bottom - top
    if face_width <= 0 or face_height <= 0:
        return {}
    center_x = (left + right) / 2.0
    # The face mask is already padded for a natural feather.  Expand it to a
    # bounded torso/gesture region, not across most of the background.
    subject_width = min(0.54, max(0.38, face_width * 2.25))
    subject_left = max(0.0, min(1.0 - subject_width, center_x - subject_width / 2.0))
    subject_top = max(0.0, top - max(0.05, face_height * 0.85))
    return {
        "mask_kind": "rectangle",
        "mask_bounds": {
            "left": round(subject_left, 5),
            "top": round(subject_top, 5),
            "right": round(subject_left + subject_width, 5),
            "bottom": 1.0,
        },
        "mask_units": "normalized",
        "feather": 60.0,
    }


def _architecture_night_geometry(
    score: dict[str, Any],
    variant: str,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any]]:
    """Enable a night-architecture finish without forcing a panorama crop.

    A wide city frame often uses water, skyline spacing, and foreground
    reflections as structural elements. Without reliable geometry evidence,
    the planner must preserve that context and leave crop decisions to an
    explicit, evidence-backed operation.
    """

    if str(score.get("primary_category")) != "architecture-urban-space" or variant != "competition-standard":
        return False, None, {"preserve_context": True, "reason": "architecture_night_finish_not_applicable"}
    tags = {str(item).casefold() for item in score.get("secondary_tags", []) if item}
    evidence = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    labels = {
        str(item[0]).casefold(): float(item[1])
        for item in (evidence.get("labels") or [])
        if isinstance(item, (list, tuple)) and len(item) >= 2
    }
    night = "night" in tags or labels.get("night sky", 0.0) >= 0.30
    open_sky = "minimal" in tags and max(labels.get("sky", 0.0), labels.get("cloudy", 0.0)) >= 0.65
    dimensions = evidence.get("display_dimensions") if isinstance(evidence.get("display_dimensions"), list) else []
    landscape = len(dimensions) == 2 and float(dimensions[0]) > float(dimensions[1]) > 0
    if not (night and open_sky and landscape):
        return False, None, {"preserve_context": True, "reason": "insufficient_night_sky_geometry_evidence"}
    return True, None, {
        "preserve_context": True,
        "reason": "night-architecture-finish-preserves-water-skyline-and-foreground-context",
        "policy": "preserve-context-unless-geometry-evidence-requires-crop",
    }


def build_lightroom_plan(score: dict[str, Any], style_recipe: dict[str, Any] | None = None, variant: str = "natural") -> dict[str, Any]:
    technical = float(score.get("technical", 60))
    gain = float(score.get("expected_gain", 30))
    recipe = (style_recipe or {}).get("recipe", {}).get("lightroom", {}) if isinstance(style_recipe, dict) else {}
    architecture_night, _, _ = _architecture_night_geometry(score, variant)
    defaults = {
        "natural": {
            "exposure": 0.12, "highlights": -18.0, "shadows": 12.0,
            "whites": 4.0, "blacks": -6.0, "contrast": 6.0,
            "clarity": 3.0, "vibrance": 8.0, "saturation": -1.0,
            "texture": 3.0, "dehaze": 1.0,
        },
        "editorial": {
            "exposure": 0.22, "highlights": -26.0, "shadows": 18.0,
            "whites": 6.0, "blacks": -8.0, "contrast": 10.0,
            "clarity": 4.0, "vibrance": 14.0, "saturation": -2.0,
            "texture": 4.0, "dehaze": 2.0,
        },
        "competition-standard": {
            "exposure": 0.28, "highlights": -32.0, "shadows": 22.0,
            "whites": 9.0, "blacks": -12.0, "contrast": 12.0,
            "clarity": 6.0, "vibrance": 18.0, "saturation": -2.0,
            "texture": 5.0, "dehaze": 3.0,
        },
    }.get(variant, {})
    if architecture_night:
        # High-ISO city files need highlight control and black separation, not
        # the generic competition recipe's exposure/shadow lift. Photoshop
        # handles the local building lift after this quieter RAW foundation.
        defaults = {
            "exposure": 0.72, "highlights": -44.0, "shadows": 35.0,
            "whites": -3.0, "blacks": -7.0, "contrast": 8.0,
            "clarity": 3.0, "vibrance": 30.0, "saturation": 8.0,
            "texture": 2.0, "dehaze": 3.0,
        }
    exposure = float(defaults.get("exposure", 0.0)) + float(recipe.get("exposure_bias", 0.0))
    if technical < 70:
        exposure += 0.10 if float(score.get("mean_luma", 0.5)) < 0.35 else 0.0
    if variant == "editorial":
        exposure += 0.08
    if variant == "competition-standard" and not architecture_night:
        exposure += 0.04
    color = build_color_style(score, style_recipe, variant)
    if architecture_night:
        color["strategy"]["scene_mode"] = "night-architecture"
        color["strategy"]["rationale"] = (
            "Create visible red/cyan separation, hold neon highlights, and keep the ISO 6400 sky quiet."
        )
    color_settings = {key: value for key, value in color.items() if key != "strategy"}
    return {
        "backend": "lightroom-mcp",
        "operation_id": f"lr-{score.get('photo_id', 'photo')}-{variant}",
        "checkpoint_required": True,
        "settings": {
            "exposure": round(max(-1.5, min(1.5, exposure)), 3),
            "highlights": round(max(-70.0, min(0.0, float(defaults.get("highlights", 0.0)) - float(recipe.get("highlight_protection", 0.0)))), 2),
            "shadows": round(max(0.0, min(35.0, float(defaults.get("shadows", 0.0)) + float(recipe.get("shadow_lift", 0.0)))), 2),
            "whites": round(max(-25.0, min(25.0, float(defaults.get("whites", 0.0)))), 2),
            "blacks": round(max(-30.0, min(30.0, float(defaults.get("blacks", 0.0)))), 2),
            "contrast": round(max(-20.0, min(30.0, float(defaults.get("contrast", 0.0)) + float(recipe.get("contrast_bias", 0.0)))), 2),
            "clarity": round(max(-10.0, min(20.0, float(defaults.get("clarity", 0.0)))), 2),
            "vibrance": round(max(-20.0, min(30.0, float(defaults.get("vibrance", 0.0)))), 2),
            "saturation": round(max(-12.0, min(12.0, float(defaults.get("saturation", 0.0)) + float(recipe.get("saturation_bias", 0.0)))), 2),
            "texture": round(max(-10.0, min(20.0, float(defaults.get("texture", 0.0)))), 2),
            "dehaze": round(max(-10.0, min(25.0, float(defaults.get("dehaze", 0.0)))), 2),
            "sharpening": 18.0 if architecture_night else round(max(0.0, min(100.0, 35.0 + gain * 0.25)), 2),
            **({
                "luminance_smoothing": 55.0,
                "luminance_noise_reduction_detail": 45.0,
                "luminance_noise_reduction_contrast": 20.0,
                "color_noise_reduction": 35.0,
                "color_noise_reduction_detail": 50.0,
                "color_noise_reduction_smoothness": 50.0,
            } if architecture_night else {}),
            **color_settings,
        },
        "color_strategy": color["strategy"],
        "export": {
            "profile": "competition-quality" if variant == "competition-standard" else "web-share",
            # Photoshop's current local bridge exports verified high-quality
            # JPEGs, while its layered TIFF save path is host-version
            # dependent.  Keep print-master opt-in instead of making the
            # default share/competition JPG flow fail on a TIFF host bug.
            "profiles": ["competition-quality"],
            "quality": 100,
            "preserve_metadata": True,
            "require_exif": True,
            "require_embedded_xmp": False,
        },
        "restore_policy": "restore-develop-after-export-unless-user-approves-persistent-catalog-edit",
    }


def build_photoshop_plan(
    score: dict[str, Any],
    variant: str = "natural",
    generative_available: bool = False,
    generative_capability: dict[str, Any] | None = None,
    processing_locality: str = "local-only",
) -> dict[str, Any]:
    category = str(score.get("primary_category", "other-unsupported"))
    face_mask, crop, crop_guard = _face_geometry(score)
    architecture_night, architecture_crop, architecture_crop_guard = _architecture_night_geometry(score, variant)
    if architecture_night:
        crop = architecture_crop
        crop_guard = architecture_crop_guard
    subject_mask = _portrait_subject_mask(face_mask)
    portrait_refine = category == "portrait-environmental" and bool(face_mask)
    face_bounds = face_mask.get("mask_bounds") if isinstance(face_mask.get("mask_bounds"), dict) else {}
    crowd_distractor_mask: dict[str, Any] = {}
    if portrait_refine and (
        "crowd" in {str(item).casefold() for item in score.get("secondary_tags", []) if item}
        or "people" in {str(item).casefold() for item in score.get("secondary_tags", []) if item}
    ):
        try:
            # A bright crowd member immediately beside the face competes more
            # strongly than distant street texture.  This bounded strip leaves
            # the face untouched and only reins in the competing background.
            face_left = float(face_bounds["left"])
            face_top = float(face_bounds["top"])
            face_bottom = float(face_bounds["bottom"])
            crowd_distractor_mask = {
                "mask_kind": "rectangle",
                "mask_bounds": {
                    "left": round(max(0.0, face_left - 0.18), 5),
                    "top": round(max(0.0, face_top - 0.04), 5),
                    "right": round(max(0.0, face_left - 0.008), 5),
                    "bottom": round(min(1.0, face_bottom + 0.36), 5),
                },
                "mask_units": "normalized",
                "feather": 55.0,
            }
        except (KeyError, TypeError, ValueError):
            crowd_distractor_mask = {}
    operations = [
        # Document structure is created before pixel edits so every later
        # operation can be rolled back to a named layer/checkpoint.
        {"tool": "layer_operation", "operation": "duplicate", "risk": "low", "required": True, "parameters": {"name": "01 · 原始像素保护"}},
        {"tool": "layer_operation", "operation": "create_group", "risk": "low", "required": True, "parameters": {"name": "02 · AI 精修"}},
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": architecture_night,
            "applicable": architecture_night,
            "parameters": {
                "mask_kind": "all",
                "mask_units": "normalized",
                "feather": 0.0,
                "saturation": 35.0,
                "lightness": 3.0,
                "output_layer_name": "02C · 全局夜景色彩基底",
            },
        },
        {
            "tool": "sharpening",
            "risk": "medium",
            "required": architecture_night,
            "applicable": architecture_night,
            "requires_100_percent_check": True,
            "parameters": {
                "mask_kind": "all",
                "opacity": 24.0,
                "output_layer_name": "02A · 建筑细节锐化",
            },
        },
        {
            "tool": "noise_reduction",
            "risk": "low",
            "required": architecture_night,
            "applicable": architecture_night,
            "parameters": {
                "mask_kind": "rectangle",
                "mask_bounds": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 0.31},
                "mask_units": "normalized",
                "feather": 165.0,
                "radius": 1.8,
                "opacity": 88.0,
                "output_layer_name": "02B · 夜空细腻降噪",
            },
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": architecture_night,
            "applicable": architecture_night,
            "parameters": {
                "mask_kind": "rectangle",
                "mask_bounds": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 0.29},
                "mask_units": "normalized",
                "feather": 150.0,
                "saturation": -12.0,
                "lightness": -3.0,
                "output_layer_name": "03 · 夜空去脏灰",
            },
        },
        {
            "tool": "region_mask_operation",
            "risk": "medium",
            "required": architecture_night,
            "applicable": architecture_night,
            "parameters": {
                "mask_kind": "rectangle",
                "mask_bounds": {"left": 0.12, "top": 0.22, "right": 0.99, "bottom": 0.68},
                "mask_units": "normalized",
                "feather": 135.0,
                "brightness": 32.0,
                "contrast": 8.0,
                "output_layer_name": "04 · 建筑主体塑形",
            },
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": architecture_night,
            "applicable": architecture_night,
            "parameters": {
                "mask_kind": "rectangle",
                "mask_bounds": {"left": 0.12, "top": 0.22, "right": 0.99, "bottom": 0.68},
                "mask_units": "normalized",
                "feather": 135.0,
                "saturation": 18.0,
                "lightness": 3.0,
                "output_layer_name": "04B · 建筑冷暖分离",
            },
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": architecture_night,
            "applicable": architecture_night,
            "parameters": {
                "mask_kind": "rectangle",
                "mask_bounds": {"left": 0.0, "top": 0.64, "right": 1.0, "bottom": 0.96},
                "mask_units": "normalized",
                "feather": 115.0,
                "saturation": -20.0,
                "lightness": -6.0,
                "output_layer_name": "05 · 前景蓝紫抑制",
            },
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": portrait_refine,
            "applicable": portrait_refine,
            "parameters": {
                "mask_kind": "all",
                "saturation": -10.0,
                "lightness": -3.0,
                "output_layer_name": "03 · 环境色彩克制",
            },
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": portrait_refine and bool(crowd_distractor_mask),
            "applicable": portrait_refine and bool(crowd_distractor_mask),
            "parameters": {
                **crowd_distractor_mask,
                "saturation": -34.0,
                "lightness": -12.0,
                "output_layer_name": "03A · 邻近人群降噪",
            },
        },
        {
            "tool": "region_mask_operation",
            "risk": "medium",
            "required": portrait_refine and bool(subject_mask),
            "applicable": portrait_refine and bool(subject_mask),
            "parameters": {
                **subject_mask,
                "brightness": 8.0,
                "contrast": 5.0,
                "output_layer_name": "04 · 主体明暗分离",
            },
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": portrait_refine and bool(subject_mask),
            "applicable": portrait_refine and bool(subject_mask),
            "parameters": {
                **subject_mask,
                "saturation": 5.0,
                "lightness": 3.0,
                "output_layer_name": "05 · 主体色彩回弹",
            },
        },
        {
            "tool": "region_mask_operation",
            "risk": "medium",
            "required": portrait_refine,
            "applicable": portrait_refine,
            "parameters": {
                **face_mask,
                "brightness": 14.0,
                "contrast": -2.0,
                "output_layer_name": "06 · 帽檐下自然补光",
            },
        },
        {
            "tool": "portrait_beauty",
            "risk": "medium",
            "required": portrait_refine,
            "applicable": portrait_refine,
            "requires_face_mask": True,
            "parameters": {**face_mask, "smoothing_radius": 2.4, "face_brightness": 20.0, "face_contrast": 3.0, "output_layer_name": "07 · 肤质与面部明暗"},
        },
        {
            "tool": "selective_color",
            "risk": "medium",
            "required": portrait_refine,
            "applicable": portrait_refine,
            "parameters": {**face_mask, "saturation": 4.0, "lightness": 4.0, "output_layer_name": "08 · 肤色与局部色彩"},
        },
        # These actions are available, but Photoshop correctly requires a
        # user-recorded Action Descriptor for brush/curve geometry.  Keep them
        # in the plan and review queue without making the default run fail
        # closed when no descriptor has been registered yet.
        {"tool": "dodge_burn", "risk": "medium", "required": False, "applicable": False, "review": True, "requires_descriptor": True},
        {"tool": "selective_color", "risk": "medium", "required": False, "applicable": False, "review": True, "requires_descriptor": True},
        {"tool": "curves_local", "risk": "medium", "required": False, "applicable": False, "review": True, "requires_descriptor": True},
        {"tool": "noise_reduction", "risk": "low", "required": False, "applicable": False, "review": True, "requires_descriptor": True},
        {"tool": "sharpening", "risk": "medium", "required": False, "applicable": False, "review": True, "requires_100_percent_check": True, "requires_descriptor": True},
        {"tool": "perspective_warp", "risk": "high", "required": False, "applicable": False, "review": True, "requires_descriptor": True},
        {"tool": "liquify", "risk": "high", "required": False, "applicable": False, "requires_face_mask": True, "review": True, "requires_descriptor": True},
        {"tool": "content_aware_remove", "risk": "high", "required": False, "applicable": False, "review": True},
        {"tool": "apply_crop", "risk": "high", "required": crop is not None, "applicable": crop is not None, "review": True, "parameters": crop or {}},
    ]
    capability = generative_capability if isinstance(generative_capability, dict) else {}
    planning_eligible = capability.get("planning_eligible") is True
    if generative_available or capability.get("available") is True or planning_eligible:
        host_pending = capability.get("requires_host_tool") is True and capability.get("ready_for_execution") is not True
        operations.append({
            "tool": "generative-fill",
            "risk": "high",
            "required": False,
            "status": "awaiting-host-imagegen" if host_pending else "planned",
            "backend": capability.get("backend", "local-generative-backend"),
            "locality": capability.get("locality", "local"),
            "requires_mask": True,
            "requires_cloud_approval": False,
            "requires_conversation_confirmation": False,
            "automatic_host_call": capability.get("locality") == "chat-window",
            "requires_visible_conversation_image": capability.get("requires_visible_conversation_image", False),
        })
    else:
        operations.append({
            "tool": "generative-fill",
            "risk": "high",
            "required": False,
            "status": "downgraded-generative-backend-unavailable",
            "reason": capability.get("reason", "no_eligible_generative_backend"),
        })
    # Promote only problem-driven operations that have an executable route.
    # This makes the visual diagnosis control the real plan while preserving
    # fail-closed behavior for missing descriptors or unavailable UI control.
    problem_plan = score.get("problem_driven_plan") if isinstance(score.get("problem_driven_plan"), dict) else {}
    problem_operations = problem_plan.get("operations") if isinstance(problem_plan.get("operations"), list) else []
    problem_tool_map = {
        "subject-relight": "region_mask_operation",
        "background-restraint": "selective_color",
        "crop-and-straighten": "apply_crop",
        "skin-tone-correct": "portrait_beauty",
        "selective-sharpen": "sharpening",
    }
    for problem in problem_operations:
        if not isinstance(problem, dict):
            continue
        route = problem.get("execution_route") if isinstance(problem.get("execution_route"), dict) else {}
        if route.get("tier") not in {"stable-auto", "descriptor-verified"}:
            continue
        desired_tool = problem_tool_map.get(str(problem.get("type")))
        if not desired_tool:
            continue
        target = next((item for item in operations if item.get("tool") == desired_tool), None)
        if target is None or (desired_tool == "apply_crop" and crop is None):
            continue
        target["required"] = True
        target["applicable"] = True
        target["problem_reason"] = problem.get("reason")
        target["success_criteria"] = problem.get("success_criteria")
        target["execution_route"] = route
        if route.get("descriptor_id"):
            target.setdefault("parameters", {})["descriptor_id"] = route["descriptor_id"]
    return {
        "backend": "photoshop-fine-edit",
        "operation_id": f"ps-{score.get('photo_id', 'photo')}-{variant}",
        "operations": operations,
        "master": {"format": "psd", "layered": True, "masks": True},
        "document_policy": {
            "mode": "single-document",
            "rollback": "photoshop-history",
            "snapshot_policy": "phase-only",
            "persist_intermediate_psd": False,
            "persist_intermediate_exports": False,
            "final_save": "one-psd",
        },
        "crop_guard": crop_guard,
        "tool_coverage": {
            "document": ["duplicate", "create_group"],
            "cleanup": ["healing", "clone_stamp", "eraser_mask", "content_aware_remove"],
            "portrait": ["portrait_beauty", "liquify"],
            "light_and_color": ["dodge_burn", "selective_color", "curves_local"],
            "detail": ["noise_reduction", "sharpening"],
            "geometry": ["perspective_warp", "apply_crop"],
            "generative": ["generative-fill"],
        },
        "processing_locality": processing_locality,
        "generative_backend": capability,
        "disclosure": "仅在执行返回验证过的操作清单后标记为精细后期",
    }
