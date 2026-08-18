"""Deterministic, local preview metrics built on Pillow."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageStat


def _luma(red: int, green: int, blue: int) -> float:
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _perceptual_hash(gray: Image.Image) -> str:
    """Return a stable 64-bit difference hash for a Pillow image."""
    resized = gray.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(resized.getdata())
    bits = "".join(
        "1" if pixels[row * 9 + column] >= pixels[row * 9 + column + 1] else "0"
        for row in range(8)
        for column in range(8)
    )
    return f"{int(bits, 2):016x}"


def _neighbour_metrics(luma_values: list[float], width: int, height: int) -> tuple[float, float]:
    if width < 2 or height < 2:
        return 0.0, 0.0
    differences: list[float] = []
    for row in range(height):
        for column in range(width):
            index = row * width + column
            if column + 1 < width:
                differences.append(abs(luma_values[index] - luma_values[index + 1]))
            if row + 1 < height:
                differences.append(abs(luma_values[index] - luma_values[index + width]))
    mean_difference = sum(differences) / len(differences)
    edge_energy = mean_difference / 255.0
    # Sparse local contrast means a soft or uniform preview; alternating sharp edges retain high energy.
    return max(0.0, min(1.0, 1.0 - edge_energy)), edge_energy


def _analysis_image(rgb: Image.Image) -> Image.Image:
    """Return a bounded working image while preserving source dimensions elsewhere."""

    max_edge = max(256, int(os.environ.get("PHOTO_POST_METRICS_MAX_EDGE", "1600")))
    width, height = rgb.size
    if max(width, height) <= max_edge:
        return rgb
    scale = max_edge / max(width, height)
    resized = rgb.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    rgb.close()
    return resized


def _stream_pixel_metrics(rgb: Image.Image, gray: Image.Image) -> tuple[float, float, float, float, list[int]]:
    """Compute preview statistics without materializing one Python object per pixel."""

    width, height = rgb.size
    pixel_count = width * height
    if pixel_count == 0:
        return 0.0, 0.0, 0.0, 0.0, [0] * 256

    rgb_pixels = rgb.load()
    gray_pixels = gray.load()
    histogram = [0] * 256
    luma_total = 0.0
    chroma_total = 0.0
    difference_total = 0.0
    difference_count = 0
    for row in range(height):
        previous_luma = None
        for column in range(width):
            red, green, blue = rgb_pixels[column, row]
            luma = _luma(red, green, blue)
            luma_total += luma
            chroma_total += (max(red, green, blue) - min(red, green, blue)) / 255.0
            gray_value = int(gray_pixels[column, row])
            histogram[gray_value] += 1
            if previous_luma is not None:
                difference_total += abs(previous_luma - luma)
                difference_count += 1
            if row > 0:
                difference_total += abs(int(gray_pixels[column, row - 1]) - gray_value)
                difference_count += 1
            previous_luma = luma

    mean_difference = difference_total / difference_count if difference_count else 0.0
    edge_energy = mean_difference / 255.0
    blur_proxy = max(0.0, min(1.0, 1.0 - edge_energy))
    return luma_total, chroma_total, blur_proxy, edge_energy, histogram


def _edge_percentiles(gray: Image.Image) -> tuple[float, float]:
    """Return local-detail percentiles without punishing photographic bokeh.

    Mean edge energy is dominated by intentionally smooth backgrounds. High
    percentiles retain the sharp subject/eye/clothing signal while a genuinely
    defocused frame still has weak values throughout the image.
    """

    edges = gray.filter(ImageFilter.FIND_EDGES)
    if edges.width > 4 and edges.height > 4:
        edges = edges.crop((2, 2, edges.width - 2, edges.height - 2))
    histogram = edges.histogram()
    total = max(1, sum(histogram))

    def percentile(fraction: float) -> float:
        threshold = total * fraction
        cumulative = 0
        for value, count in enumerate(histogram):
            cumulative += count
            if cumulative >= threshold:
                return value / 255.0
        return 1.0

    return percentile(0.90), percentile(0.95)


def analyze_preview(path: str) -> dict:
    """Analyze a locally readable image preview without modifying its source."""
    source = Path(path)
    with Image.open(source) as image:
        image.load()
        original_width, original_height = image.size
        rgb = _analysis_image(image.convert("RGB"))
        analysis_width, analysis_height = rgb.size
        gray = rgb.convert("L")
        luma_total, chroma_total, blur_proxy, noise_proxy, histogram = _stream_pixel_metrics(rgb, gray)
        edge_p90, edge_p95 = _edge_percentiles(gray)

    pixel_count = analysis_width * analysis_height
    highlight_count = sum(histogram[250:])
    shadow_count = sum(histogram[:6])

    return {
        "path": str(source),
        "width": original_width,
        "height": original_height,
        "dimensions": [original_width, original_height],
        "analysis_dimensions": [analysis_width, analysis_height],
        "pixel_count": original_width * original_height,
        "analysis_pixel_count": pixel_count,
        "luma_histogram": histogram,
        "mean_luma": round(luma_total / pixel_count / 255.0, 8),
        "mean_chroma": round(chroma_total / pixel_count, 8),
        "highlight_clipping": round(highlight_count / pixel_count, 8),
        "shadow_crush": round(shadow_count / pixel_count, 8),
        "blur_proxy": round(blur_proxy, 8),
        "noise_proxy": round(noise_proxy, 8),
        "edge_p90": round(edge_p90, 8),
        "edge_p95": round(edge_p95, 8),
        "perceptual_hash": _perceptual_hash(gray),
    }


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, float(value)))


def _rendered_component_scores(metrics: dict[str, Any], target_mean_luma: float = 0.48) -> dict[str, float]:
    """Turn measured pixels into conservative post-render quality components.

    This is deliberately independent of the AI pre-score.  It is not intended
    to replace artistic judgment; it is a deterministic release signal that
    catches the most common failure mode where a visually worse render keeps
    the original candidate score.
    """

    mean_luma = float(metrics.get("mean_luma", 0.5))
    highlight_clipping = float(metrics.get("highlight_clipping", 0.0))
    shadow_crush = float(metrics.get("shadow_crush", 0.0))
    blur_proxy = float(metrics.get("blur_proxy", 0.5))
    edge_energy = float(metrics.get("noise_proxy", 0.0))
    edge_p90 = float(metrics.get("edge_p90", edge_energy))
    edge_p95 = float(metrics.get("edge_p95", edge_energy))
    chroma = float(metrics.get("mean_chroma", 0.0))
    width, height = metrics.get("dimensions", [0, 0])
    long_edge = max(int(width or 0), int(height or 0))

    target_mean_luma = _clamp(float(target_mean_luma), 0.08, 0.75)
    exposure_balance = _clamp(100.0 - abs(mean_luma - target_mean_luma) * 145.0)
    clipping_score = _clamp(100.0 - (highlight_clipping * 900.0 + max(0.0, shadow_crush - 0.03) * 300.0))
    focus_score = _clamp((edge_p95 - 0.018) * 1500.0)
    detail_score = _clamp((edge_p90 - 0.012) * 2200.0)
    resolution_score = _clamp((long_edge / 3600.0) * 100.0)
    color_balance = _clamp(100.0 - abs(chroma - 0.18) * 120.0)

    technical = (
        focus_score * 0.30
        + clipping_score * 0.35
        + exposure_balance * 0.20
        + resolution_score * 0.15
    )
    aesthetic = (
        exposure_balance * 0.30
        + clipping_score * 0.20
        + detail_score * 0.25
        + color_balance * 0.25
    )
    return {
        "technical_score": round(_clamp(technical), 2),
        "aesthetic_score": round(_clamp(aesthetic), 2),
        "exposure_balance": round(_clamp(exposure_balance), 2),
        "clipping_score": round(_clamp(clipping_score), 2),
        "focus_score": round(_clamp(focus_score), 2),
        "detail_score": round(_clamp(detail_score), 2),
        "color_balance": round(_clamp(color_balance), 2),
    }


def evaluate_rendered_candidate(
    before_path: str | None,
    after_path: str,
    target_mean_luma: float = 0.48,
) -> dict[str, Any]:
    """Score a rendered image from actual pixels and compare it with its input.

    RAW files are not assumed to be Pillow-readable.  When the adapter supplies
    a rendered before checkpoint, it is used for the improvement component;
    otherwise improvement is neutral rather than inferred from the pre-score.
    """

    after = Path(after_path).expanduser()
    if not after.is_file():
        return {"status": "unavailable", "reason": "rendered_output_not_found"}
    try:
        after_metrics = analyze_preview(str(after))
    except (OSError, ValueError) as error:
        return {"status": "unavailable", "reason": f"rendered_output_unreadable:{type(error).__name__}"}

    after_scores = _rendered_component_scores(after_metrics, target_mean_luma)
    before_metrics: dict[str, Any] | None = None
    before_scores: dict[str, float] | None = None
    if before_path:
        before = Path(before_path).expanduser()
        if before.is_file():
            try:
                before_metrics = analyze_preview(str(before))
                before_scores = _rendered_component_scores(before_metrics, target_mean_luma)
            except (OSError, ValueError):
                before_metrics = None

    before_baseline = (
        before_scores["aesthetic_score"] if before_scores is not None else after_scores["aesthetic_score"]
    )
    improvement = _clamp(50.0 + (after_scores["aesthetic_score"] - before_baseline) * 2.0)
    final_score = _clamp(
        after_scores["technical_score"] * 0.35
        + after_scores["aesthetic_score"] * 0.45
        + improvement * 0.20
    )
    return {
        "status": "evaluated",
        "source": "post-render-pixels",
        "after_metrics": after_metrics,
        "before_metrics": before_metrics,
        "technical_score": after_scores["technical_score"],
        "aesthetic_score": after_scores["aesthetic_score"],
        "improvement_score": round(improvement, 2),
        "final_score": round(final_score, 2),
        "components": after_scores,
        "target_mean_luma": round(float(target_mean_luma), 3),
    }


def compare_rendered_pixels(before_path: str | None, after_path: str) -> dict[str, Any]:
    """Measure whether a completed edit changed visible pixels or geometry.

    This is intentionally separate from quality scoring: a technically poor
    photo can still have been genuinely edited, while a successful adapter call
    that produces an identity render must never be reported as a retouch.
    """
    if not before_path:
        return {"status": "unavailable", "reason": "before_render_missing"}
    before, after = Path(before_path).expanduser(), Path(after_path).expanduser()
    if not before.is_file() or not after.is_file():
        return {"status": "unavailable", "reason": "render_path_missing"}
    try:
        with Image.open(before) as before_image, Image.open(after) as after_image:
            before_rgb = before_image.convert("RGB")
            after_rgb = after_image.convert("RGB")
            before_dimensions = list(before_rgb.size)
            after_dimensions = list(after_rgb.size)
            resized_before = before_rgb.resize(after_rgb.size, Image.Resampling.LANCZOS)
            difference = ImageChops.difference(resized_before, after_rgb)
            mean_abs_delta = sum(ImageStat.Stat(difference).mean) / 3
            histogram = difference.convert("L").histogram()
            pixel_count = after_rgb.width * after_rgb.height
            changed_fraction = sum(histogram[6:]) / max(1, pixel_count)
    except (OSError, ValueError) as error:
        return {"status": "unavailable", "reason": f"render_compare_failed:{type(error).__name__}"}
    geometry_changed = before_dimensions != after_dimensions
    material = geometry_changed or mean_abs_delta >= 2.0 or changed_fraction >= 0.01
    return {
        "status": "evaluated",
        "before_dimensions": before_dimensions,
        "after_dimensions": after_dimensions,
        "geometry_changed": geometry_changed,
        "mean_abs_delta": round(mean_abs_delta, 4),
        "changed_fraction": round(changed_fraction, 6),
        "material_change": material,
    }
