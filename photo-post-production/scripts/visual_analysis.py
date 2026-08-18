"""Local visual evidence, category classification, and three-score analysis.

The preferred backend is Apple's on-device Vision framework. It contributes
subject labels, face/animal/text evidence, and confidence. Deterministic image
metrics remain authoritative for technical gates. When Vision is unavailable,
the module returns a clearly marked bounded fallback and routes the record to
review instead of pretending that a filename or a histogram is semantic truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from category_weighting import WEIGHTS_VERSION, category_weights
from model_signals import normalize_model_signals
from score_explainer import build_score_explanation


SCRIPT_DIR = Path(__file__).resolve().parent
VISION_SOURCE = SCRIPT_DIR / "apple_vision_analyzer.swift"
VISION_BINARY = Path.home() / "Library/Application Support/PhotoPostProduction/cache/apple-vision-analyzer"

CATEGORIES = (
    "landscape-nature", "urban-landscape", "architecture-urban-space",
    "street-documentary", "portrait-environmental", "animal-wildlife",
    "plant-macro", "other-unsupported",
)

_ANIMAL_WORDS = {"animal", "bird", "cat", "dog", "horse", "deer", "bear", "fish", "insect", "mammal", "reptile", "wildlife"}
_PERSON_WORDS = {"person", "people", "man", "woman", "face", "portrait", "human", "child", "crowd"}
_ARCHITECTURE_WORDS = {"building", "architecture", "bridge", "tower", "skyscraper", "church", "house", "facade", "interior", "stadium"}
_URBAN_WORDS = {"city", "street", "road", "urban", "downtown", "traffic", "intersection", "nightlife", "car", "vehicle", "train", "subway"}
_NATURE_WORDS = {"mountain", "landscape", "nature", "forest", "tree", "plant", "flower", "beach", "coast", "ocean", "sea", "lake", "river", "waterfall", "sky", "sunset", "sunrise", "cloud"}
_PLANT_MACRO_WORDS = {"flower", "blossom", "petal", "leaf", "leaves", "plant", "botanical", "macro", "macro photography", "close-up", "close up", "bud", "stamen"}
_TAG_RULES = {
    "night": {"night", "dark", "nighttime", "city_at_night"},
    "backlight": {"sunset", "sunrise", "silhouette", "backlight"},
    "people": _PERSON_WORDS,
    "crowd": {"crowd", "people", "group"},
    "reflection": {"reflection", "water", "lake", "river", "mirror"},
    "minimal": {"minimal", "empty", "sky"},
}

# The component meanings stay stable, while the category changes the relative
# importance of composition, light, moment and integrity.  These weights are
# the development-document table, expressed as fractions.
CATEGORY_WEIGHTS = {category: category_weights(category) for category in CATEGORIES}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _normalise_labels(payload: dict[str, Any]) -> list[tuple[str, float]]:
    output: list[tuple[str, float]] = []
    for item in payload.get("classifications", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("identifier", "")).casefold().replace("_", " ").strip()
        if label:
            output.append((label, max(0.0, min(1.0, _finite(item.get("confidence"))))))
    for item in payload.get("animals", []):
        if isinstance(item, dict):
            label = str(item.get("identifier", "animal")).casefold().strip() or "animal"
            output.append((label, max(0.0, min(1.0, _finite(item.get("confidence"))))))
    return output


def _compile_vision() -> Path | None:
    swiftc = shutil.which("swiftc")
    if not swiftc or not VISION_SOURCE.is_file():
        return None
    VISION_BINARY.parent.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(VISION_SOURCE.read_bytes()).hexdigest()[:16]
    stamp = VISION_BINARY.with_suffix(".sha256")
    if VISION_BINARY.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == source_hash:
        return VISION_BINARY
    temporary = VISION_BINARY.with_name(f"{VISION_BINARY.name}.tmp-{os.getpid()}")
    completed = subprocess.run(
        [swiftc, "-O", str(VISION_SOURCE), "-o", str(temporary)],
        check=False, capture_output=True, text=True, timeout=120,
    )
    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        return None
    temporary.replace(VISION_BINARY)
    stamp.write_text(source_hash, encoding="utf-8")
    return VISION_BINARY


def run_local_vision(path: str, timeout: int = 60) -> dict[str, Any]:
    """Run the on-device classifier once; return structured unavailable data."""
    binary = _compile_vision()
    if binary is None:
        return {"available": False, "backend": None, "reason": "apple_vision_unavailable"}
    completed = subprocess.run([str(binary), str(Path(path).resolve())], check=False, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        return {"available": False, "backend": "apple-vision", "reason": (completed.stderr or "vision_failed").strip()[-500:]}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"available": False, "backend": "apple-vision", "reason": "vision_invalid_json"}
    try:
        with Image.open(path) as image:
            orientation = int(image.getexif().get(274, 1) or 1)
            raw_width, raw_height = image.size
        display_width, display_height = (
            (raw_height, raw_width) if orientation in {5, 6, 7, 8}
            else (raw_width, raw_height)
        )
        payload.update({
            "exif_orientation": orientation,
            "raw_dimensions": [raw_width, raw_height],
            "display_dimensions": [display_width, display_height],
        })
    except (OSError, ValueError, TypeError):
        payload.update({"exif_orientation": 1})
    payload["available"] = True
    return payload


def _vision_box_to_display(box: dict[str, Any], orientation: int) -> dict[str, float] | None:
    """Map a Vision lower-left box to displayed top-left coordinates.

    ``VNImageRequestHandler(cgImage:)`` does not apply JPEG EXIF orientation.
    Lightroom/Photoshop do, so masks and crops must transform all four box
    corners before they are used on the rendered portrait document.
    """

    try:
        x = float(box["x"])
        y = float(box["y"])
        width = float(box["width"])
        height = float(box["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None

    def orient(u: float, v: float) -> tuple[float, float]:
        # u/v are top-left normalized coordinates in the stored pixel array.
        transforms = {
            1: lambda a, b: (a, b),
            2: lambda a, b: (1.0 - a, b),
            3: lambda a, b: (1.0 - a, 1.0 - b),
            4: lambda a, b: (a, 1.0 - b),
            5: lambda a, b: (b, a),
            6: lambda a, b: (1.0 - b, a),
            7: lambda a, b: (1.0 - b, 1.0 - a),
            8: lambda a, b: (b, 1.0 - a),
        }
        return transforms.get(orientation, transforms[1])(u, v)

    raw_left, raw_right = x, x + width
    raw_top, raw_bottom = 1.0 - (y + height), 1.0 - y
    points = [
        orient(raw_left, raw_top),
        orient(raw_right, raw_top),
        orient(raw_left, raw_bottom),
        orient(raw_right, raw_bottom),
    ]
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    left, right = max(0.0, min(xs)), min(1.0, max(xs))
    top, bottom = max(0.0, min(ys)), min(1.0, max(ys))
    return {
        "x": round(left, 8),
        "y": round(top, 8),
        "width": round(max(0.0, right - left), 8),
        "height": round(max(0.0, bottom - top), 8),
    }


def _contains(labels: Iterable[str], words: set[str]) -> bool:
    return any(label == word or word in label for label in labels for word in words)


def _category(labels: list[tuple[str, float]], face_count: int, animal_count: int) -> tuple[str, float]:
    semantic_labels = [(label, confidence) for label, confidence in labels if confidence >= 0.18]
    names = [label for label, _ in semantic_labels]
    confidence = max((confidence for _, confidence in semantic_labels), default=0.0)
    if animal_count or _contains(names, _ANIMAL_WORDS):
        return "animal-wildlife", max(0.7, confidence)
    if face_count or _contains(names, _PERSON_WORDS):
        if _contains(names, _URBAN_WORDS) or _contains(names, _ARCHITECTURE_WORDS):
            return "street-documentary", max(0.65, confidence)
        return "portrait-environmental", max(0.7, confidence)
    if _contains(names, _ARCHITECTURE_WORDS):
        return "architecture-urban-space", max(0.68, confidence)
    if _contains(names, _URBAN_WORDS):
        return "urban-landscape", max(0.68, confidence)
    if _contains(names, _PLANT_MACRO_WORDS):
        return "plant-macro", max(0.70, confidence)
    if _contains(names, _NATURE_WORDS):
        return "landscape-nature", max(0.68, confidence)
    return "other-unsupported", min(0.55, confidence)


def _technical_scores(technical: dict[str, Any]) -> tuple[float, float, list[str], dict[str, str]]:
    clipping = _finite(technical.get("highlight_clipping"))
    crush = _finite(technical.get("shadow_crush"))
    blur = _finite(technical.get("blur_proxy"))
    noise = _finite(technical.get("noise_proxy"))
    mean_luma = _finite(technical.get("mean_luma"), 0.5)
    technical_score = 100.0
    risks: list[str] = []
    if clipping > 0.08:
        technical_score -= min(30.0, clipping * 200)
        risks.append("高光溢出比例偏高")
    if crush > 0.10:
        technical_score -= min(24.0, crush * 120)
        risks.append("暗部压黑风险")
    # ``blur_proxy`` is a low-frequency neighbour proxy. On a full 1800px
    # preview, normal photographs commonly sit around 0.97–0.99; treating
    # 0.72 as a failure would reject every real RAW preview. Only the extreme
    # tail is a warning, and the final 100% review remains authoritative.
    if blur > 0.996:
        technical_score -= min(28.0, (blur - 0.996) * 7000)
        risks.append("清晰度代理偏弱")
    if noise > 0.34:
        technical_score -= min(15.0, (noise - 0.34) * 80)
        risks.append("局部纹理/噪点需复核")
    if mean_luma < 0.08 or mean_luma > 0.94:
        technical_score -= 12
        risks.append("整体曝光偏极端")
    technical_score = max(0.0, min(100.0, technical_score))
    editability = 62.0
    if clipping < 0.04:
        editability += 15
    if crush < 0.08:
        editability += 10
    if blur < 0.993:
        editability += 8
    if 0.12 <= mean_luma <= 0.86:
        editability += 5
    editability = max(0.0, min(100.0, editability))
    gates = {
        "source_readable": "pass",
        "irrecoverable_defect": "fail" if technical_score < 38 else ("warn" if technical_score < 62 else "pass"),
        "preview_quality": "warn" if risks else "pass",
    }
    return technical_score, editability, risks, gates


def _score_for_category(category: str, technical_score: float, editability: float, labels: list[tuple[str, float]], vision_available: bool, tags: list[str]) -> dict[str, float]:
    semantic_strength = max((confidence for _, confidence in labels), default=0.0) * 100
    subject_score = min(96.0, 48.0 + semantic_strength * 0.42) if vision_available else 52.0
    if category == "street-documentary":
        composition = min(92.0, 58.0 + (15.0 if "people" in tags else 0) + technical_score * 0.15)
        light_color = min(94.0, 52.0 + technical_score * 0.28)
        moment_story = min(94.0, 55.0 + semantic_strength * 0.35)
        coherence = min(94.0, 52.0 + technical_score * 0.22)
    elif category == "portrait-environmental":
        composition = min(94.0, 59.0 + technical_score * 0.18)
        light_color = min(94.0, 55.0 + technical_score * 0.25)
        moment_story = min(92.0, 53.0 + semantic_strength * 0.32)
        coherence = min(94.0, 56.0 + technical_score * 0.20)
    elif category == "architecture-urban-space":
        composition = min(95.0, 56.0 + technical_score * 0.28)
        light_color = min(94.0, 54.0 + technical_score * 0.24)
        moment_story = min(88.0, 46.0 + semantic_strength * 0.30)
        coherence = min(95.0, 55.0 + technical_score * 0.25)
    elif category == "plant-macro":
        composition = min(94.0, 57.0 + technical_score * 0.24)
        light_color = min(95.0, 58.0 + technical_score * 0.25)
        moment_story = min(86.0, 46.0 + semantic_strength * 0.25)
        coherence = min(95.0, 58.0 + technical_score * 0.24)
    else:
        composition = min(94.0, 55.0 + technical_score * 0.22)
        light_color = min(94.0, 56.0 + technical_score * 0.27)
        moment_story = min(90.0, 50.0 + semantic_strength * 0.32)
        coherence = min(94.0, 55.0 + technical_score * 0.22)
    weights = category_weights(category)
    photographic_value = (
        technical_score * weights["technical"]
        + composition * weights["composition"]
        + light_color * weights["light_color"]
        + moment_story * weights["moment_story"]
        + coherence * weights["coherence"]
    )
    keep_value = min(100.0, photographic_value * 0.72 + subject_score * 0.28)
    expected_gain = max(8.0, min(92.0, (100.0 - technical_score) * 0.42 + editability * 0.30 + (100.0 - keep_value) * 0.18 + 10.0))
    candidate = keep_value * 0.65 + editability * 0.20 + expected_gain * 0.15
    return {"technical": technical_score, "composition": composition, "light_color": light_color, "moment_story": moment_story, "coherence": coherence, "photographic_value": photographic_value, "keep_value": keep_value, "expected_gain": expected_gain, "candidate_potential": candidate}


def analyze_visual_record(record: dict[str, Any], vision: dict[str, Any] | None = None, style_fit: float = 50.0) -> dict[str, Any]:
    """Return a schema-shaped score record with explicit evidence provenance."""
    technical = record.get("technical_analysis") if isinstance(record.get("technical_analysis"), dict) else record
    source_path = str(record.get("source_path") or record.get("path") or "")
    vision = vision if isinstance(vision, dict) else run_local_vision(str(record.get("preview_path") or record.get("path") or source_path))
    labels = _normalise_labels(vision)
    face_count = len(vision.get("faces", [])) if isinstance(vision.get("faces"), list) else 0
    animal_count = len(vision.get("animals", [])) if isinstance(vision.get("animals"), list) else 0
    category, classification_confidence = _category(labels, face_count, animal_count)
    label_names = [label for label, confidence in labels if confidence >= 0.18]
    tags = [tag for tag, words in _TAG_RULES.items() if _contains(label_names, words)]
    if technical.get("mean_luma", 0.5) < 0.24:
        tags.append("low-light")
    if technical.get("highlight_clipping", 0) > 0.05:
        tags.append("high-contrast")
    if technical.get("noise_proxy", 0) > 0.30:
        tags.append("high-iso-risk")
    technical_score, editability, risks, gates = _technical_scores(technical)
    scores = _score_for_category(category, technical_score, editability, labels, bool(vision.get("available")), tags)
    vision_confidence = max((confidence for _, confidence in labels), default=0.0)
    score_confidence = min(0.94, 0.52 + vision_confidence * 0.28 + (0.16 if vision.get("available") else 0.0) - (0.10 if risks else 0.0))
    if category == "other-unsupported":
        score_confidence = min(score_confidence, 0.62)
        risks.append("主体类别未得到足够视觉证据")
    if not vision.get("available"):
        risks.append("本机视觉模型不可用，分类和摄影价值需人工复核")
    if gates.get("irrecoverable_defect") == "fail":
        decision = "rejected"
    elif scores["candidate_potential"] >= 75 and score_confidence >= 0.75 and all(value == "pass" for value in gates.values()):
        decision = "selected"
    else:
        decision = "review"
    evidence = ["histogram", "blur-noise-proxy", "source-readability"]
    if vision.get("available"):
        evidence.extend(["apple-vision-classification", "face-animal-text-evidence"])
    else:
        evidence.append("visual-model-unavailable")
    strengths = []
    if technical_score >= 70:
        strengths.append("技术基础相对稳定")
    if composition := scores["composition"] >= 75:
        strengths.append("主体/空间结构具备进一步判断价值")
    if scores["expected_gain"] >= 55:
        strengths.append("曝光、层次或色彩有明显改善空间")
    if not strengths:
        strengths.append("需要先通过人工确认明确表达意图")
    recommended = {
        "landscape-nature": ["平衡天空高光与地面暗部", "增强前中后景层次和局部纹理"],
        "urban-landscape": ["控制灯牌和天空高光", "恢复建筑暗部并保持夜色层次"],
        "architecture-urban-space": ["校正垂直线和透视", "清理边缘干扰并保留建筑纹理"],
        "street-documentary": ["突出关键人物/动作", "保留现场关系并控制复杂背景"],
        "portrait-environmental": ["调整人物脸部曝光和肤色", "检查皮肤、头发和身体边缘"],
        "animal-wildlife": ["突出眼睛、头部或关键动作", "恢复毛发/羽毛纹理并抑制噪点"],
        "plant-macro": ["确认焦平面并强化花瓣、叶片或细节纹理", "降低背景色彩和高光竞争"],
        "other-unsupported": ["先人工确认主体和表达意图"],
    }[category]
    normalized_signals = normalize_model_signals(record.get("model_signals"))
    edit_outlook = {
        "largest_problem": risks[0] if risks else "需要验证主体层级、构图和色彩关系",
        "likely_result": "通过少量有目的的操作改善主体可读性和画面完整性",
        "recommended_operations": recommended,
        "unrecoverable_limits": [risk for risk in risks if "不可恢复" in risk or "失败" in risk],
    }
    orientation = int(vision.get("exif_orientation", 1) or 1)
    raw_face_boxes = [item.get("bounding_box") for item in vision.get("faces", []) if isinstance(item, dict) and isinstance(item.get("bounding_box"), dict)][:8]
    raw_animal_boxes = [item.get("bounding_box") for item in vision.get("animals", []) if isinstance(item, dict) and isinstance(item.get("bounding_box"), dict)][:8]
    display_face_boxes = [mapped for box in raw_face_boxes if (mapped := _vision_box_to_display(box, orientation)) is not None]
    display_animal_boxes = [mapped for box in raw_animal_boxes if (mapped := _vision_box_to_display(box, orientation)) is not None]
    output = {
        "photo_id": record.get("photo_id") or record.get("asset_group_id") or Path(source_path).stem,
        "source_path": source_path,
        "preview_path": record.get("preview_path") or record.get("path"),
        "primary_category": category,
        "secondary_tags": sorted(set(tags)),
        "classification_confidence": round(classification_confidence, 4),
        "score_version": "2.1-local-vision-signals",
        "category_weights_version": WEIGHTS_VERSION,
        "model_signals": normalized_signals,
        "evidence": evidence,
        "score_confidence": round(max(0.0, min(1.0, score_confidence)), 4),
        "style_fit": round(max(0.0, min(100.0, style_fit)), 2),
        "technical_gates": gates,
        "technical": round(scores["technical"], 2),
        "composition": round(scores["composition"], 2),
        "light_color": round(scores["light_color"], 2),
        "moment_story": round(scores["moment_story"], 2),
        "coherence": round(scores["coherence"], 2),
        "photographic_value": round(scores["photographic_value"], 2),
        "editability": round(editability, 2),
        "expected_gain": round(scores["expected_gain"], 2),
        "keep_value": round(scores["keep_value"], 2),
        "candidate_potential": round(scores["candidate_potential"], 2),
        "final_score": None,
        "decision": decision,
        "strengths": strengths,
        "risks": risks,
        "recommended_treatment": recommended,
        "edit_outlook": edit_outlook,
        # Keep bounded geometry alongside the counts.  The geometry is local
        # Vision evidence, not an edit instruction by itself; the edit planner
        # may use it to create an explicit crop or a face-sized mask instead
        # of silently applying an edge-to-edge "portrait" adjustment.
        "visual_evidence": {
            "backend": vision.get("backend"),
            "labels": labels[:10],
            "faces": face_count,
            "face_boxes": raw_face_boxes,
            "face_boxes_display": display_face_boxes,
            "animals": animal_count,
            "animal_boxes": raw_animal_boxes,
            "animal_boxes_display": display_animal_boxes,
            "box_coordinate_space": "display-top-left-normalized",
            "exif_orientation": orientation,
            "display_dimensions": vision.get("display_dimensions"),
            "text_count": len(vision.get("text", [])) if isinstance(vision.get("text"), list) else 0,
        },
    }
    output["score_explanation"] = build_score_explanation({key: output[key] for key in ("primary_category", "score_confidence", "technical_gates", "keep_value", "editability", "expected_gain", "candidate_potential", "strengths", "risks")})
    output["score_record"] = as_score_record(output)
    return output


def as_score_record(record: dict[str, Any]) -> dict[str, Any]:
    """Strip runtime metadata so the strict score contract can validate it."""
    fields = (
        "primary_category", "secondary_tags", "classification_confidence", "score_version",
        "category_weights_version", "model_signals",
        "evidence", "score_confidence", "style_fit", "technical_gates", "technical",
        "composition", "light_color", "moment_story", "coherence", "photographic_value",
        "editability", "expected_gain", "keep_value", "candidate_potential", "final_score",
        "decision", "strengths", "risks", "recommended_treatment",
    )
    return {field: record.get(field) for field in fields}


def analyze_visual_paths(records: list[dict[str, Any]], use_vision: bool = True) -> list[dict[str, Any]]:
    results = []
    for record in records:
        item = dict(record)
        try:
            vision = run_local_vision(str(item.get("preview_path") or item.get("path"))) if use_vision else {"available": False, "reason": "disabled"}
            item.update(analyze_visual_record(item, vision=vision))
            item["state"] = "scored"
        except Exception as error:
            item.update({"state": "failed", "error": f"{type(error).__name__}: {error}"})
        results.append(item)
    return results
