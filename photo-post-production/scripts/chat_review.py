"""Render numbered review-lane contact sheets for direct chat inspection."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from score_explainer import compact_explanation


def _font(size: int) -> ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_chat_sheet(
    records: list[dict[str, Any]],
    preview_dir: str,
    output_path: str,
    lane: str,
    columns: int = 3,
    cell_width: int = 480,
    cell_height: int = 360,
) -> dict[str, Any]:
    """Render one review lane as a chat-friendly labelled image sheet.

    Records should contain ``review_key`` and ``filename``. A record may also
    provide ``preview_path``; otherwise ``preview_dir/<filename stem>.jpg`` is
    used. Missing previews are rendered as explicit placeholders, never hidden.
    """
    if not isinstance(lane, str) or not lane.strip():
        raise ValueError("lane must be a non-empty string")
    if columns < 1 or cell_width < 160 or cell_height < 120:
        raise ValueError("invalid contact-sheet dimensions")
    items = [item for item in records if isinstance(item, dict)]
    rows = max(1, math.ceil(len(items) / columns))
    canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "#f2f2f2")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(22)
    label_font = _font(18)
    small_font = _font(15)
    preview_root = Path(preview_dir).expanduser().resolve()
    missing: list[str] = []
    rendered = 0

    for index, item in enumerate(items):
        left = (index % columns) * cell_width
        top = (index // columns) * cell_height
        box = (left + 6, top + 6, left + cell_width - 6, top + cell_height - 6)
        draw.rectangle(box, fill="white", outline="#bdbdbd", width=2)
        filename = str(item.get("filename", item.get("photo_id", "unknown")))
        preview_value = item.get("preview_path")
        preview = Path(preview_value).expanduser() if isinstance(preview_value, str) and preview_value.strip() else preview_root / f"{Path(filename).stem}.jpg"
        try:
            with Image.open(preview) as source:
                image = ImageOps.contain(source.convert("RGB"), (cell_width - 20, cell_height - 142))
            image_left = left + (cell_width - image.width) // 2
            image_top = top + 12
            canvas.paste(image, (image_left, image_top))
            rendered += 1
        except (FileNotFoundError, OSError):
            missing.append(filename)
            draw.text((left + 18, top + 70), "预览缺失", font=title_font, fill="#a33")

        key = str(item.get("review_key", f"{lane}{index + 1:02d}"))
        category = str(item.get("primary_category", item.get("category", "待确认")))
        label = f"{key}  {filename}"
        draw.text((left + 12, top + cell_height - 132), label[:46], font=label_font, fill="#111")
        lines = compact_explanation(item)
        for line_index, line in enumerate(lines[:3]):
            wrapped = textwrap.wrap(line, width=50)[:2]
            for wrapped_index, wrapped_line in enumerate(wrapped):
                y = top + cell_height - 103 + (line_index * 21) + (wrapped_index * 16)
                draw.text((left + 12, y), wrapped_line, font=small_font, fill="#555")

    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="JPEG", quality=92, optimize=True)
    return {"lane": lane, "output_path": str(output), "count": len(items), "rendered": rendered, "missing_previews": missing}
