"""Prepare a local RAW/JPEG folder for chat-based classification and review.

This command is intentionally analysis-only. It creates previews and a sealed
technical inventory; semantic classification and the three photographic scores
remain an AI/user decision so a filename heuristic can never masquerade as
visual evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_groups import group_related_assets
from batch_analyzer import analyze_paths


RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".dng", ".nef", ".orf", ".raf", ".rw2"}
VISUAL_EXTENSIONS = RAW_EXTENSIONS | {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".heic", ".webp"}
SUPPORTED_CATEGORIES = [
    "landscape-nature", "urban-landscape", "architecture-urban-space",
    "street-documentary", "portrait-environmental", "animal-wildlife",
    "other-unsupported",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def enumerate_sources(input_root: Path) -> list[Path]:
    if input_root.is_file():
        return [input_root] if input_root.suffix.casefold() in VISUAL_EXTENSIONS else []
    return sorted(
        (path for path in input_root.rglob("*") if path.is_file() and path.suffix.casefold() in VISUAL_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )


def _preview_path(source: Path, preview_root: Path) -> Path:
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:12]
    return preview_root / f"{source.stem}-{digest}.jpg"


def make_preview(source: Path, output: Path, timeout: int = 60) -> tuple[bool, str | None]:
    output.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.casefold() in {".jpg", ".jpeg"}:
        shutil.copy2(source, output)
        return True, None
    sips = shutil.which("sips")
    if sips:
        completed = subprocess.run(
            [sips, "-s", "format", "jpeg", "-Z", "1800", str(source), "--out", str(output)],
            check=False, capture_output=True, text=True, timeout=max(1, int(timeout)),
        )
        if completed.returncode == 0 and output.is_file():
            return True, None
        detail = (completed.stderr or completed.stdout or "sips failed").strip()[-500:]
    else:
        detail = "sips is unavailable"
    return False, detail


def prepare_batch(
    input_dir: str,
    output_dir: str,
    run_id: str | None = None,
    file_timeout: int = 60,
    batch_timeout: int = 0,
) -> dict[str, Any]:
    source_root = Path(input_dir).expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    if not source_root.is_file() and not source_root.is_dir():
        raise ValueError(f"input photo or directory does not exist: {source_root}")
    if source_root.is_file() and source_root.suffix.casefold() not in VISUAL_EXTENSIONS:
        raise ValueError(f"unsupported input photo type: {source_root.suffix}")
    if source_root.is_dir() and (source_root == output_root or source_root in output_root.parents):
        raise ValueError("output directory must be outside the input directory")
    output_root.mkdir(parents=True, exist_ok=True)
    preview_root = output_root / "previews"
    sources = enumerate_sources(source_root)
    source_hashes: dict[str, str] = {}
    source_hash_errors: dict[str, str] = {}
    for path in sources:
        try:
            source_hashes[str(path)] = sha256_file(path)
        except OSError as error:
            source_hash_errors[str(path)] = f"{type(error).__name__}: {error}"
    groups = group_related_assets([str(path) for path in sources], source_hashes)
    preview_records: list[dict[str, Any]] = []
    progress_path = output_root / "preparation-progress.jsonl"
    progress_lines: list[str] = []
    started_at = time.monotonic()
    for group in groups:
        primary = Path(group["primary_path"])
        preview = _preview_path(primary, preview_root)
        elapsed = time.monotonic() - started_at
        if batch_timeout and elapsed >= batch_timeout:
            ok, error, state = False, "batch_timeout", "preview-deferred"
        elif str(primary) in source_hash_errors:
            ok, error, state = False, source_hash_errors[str(primary)], "preview-failed"
        else:
            try:
                ok, error = make_preview(primary, preview, timeout=file_timeout)
                state = "preview-ready" if ok else "preview-failed"
            except Exception as exc:  # isolate one corrupt or hostile source file
                ok, error, state = False, f"{type(exc).__name__}: {exc}", "preview-failed"
        record = {
            "asset_group_id": group["asset_group_id"],
            "source_path": str(primary),
            "preview_path": str(preview) if ok else None,
            "state": state,
            "error": error,
        }
        preview_records.append(record)
        progress_lines.append(json.dumps({
            "source_path": str(primary), "state": state, "error": error,
            "completed": len(preview_records), "total": len(groups),
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }, ensure_ascii=False))
        progress_path.write_text("\n".join(progress_lines) + "\n", encoding="utf-8")
    technical = analyze_paths([record["preview_path"] for record in preview_records if record["preview_path"]])
    technical_by_path = {record.get("path"): record for record in technical}
    for record in preview_records:
        if record["preview_path"] in technical_by_path:
            record["technical_analysis"] = technical_by_path[record["preview_path"]]
    run_name = run_id or datetime.now(timezone.utc).strftime("batch-%Y%m%dT%H%M%SZ")
    manifest = {
        "run_id": run_name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(source_root),
        "input_kind": "file" if source_root.is_file() else "directory",
        "output_root": str(output_root),
        "source_snapshot": {"assets": [{"path": str(path), "sha256": source_hashes[str(path)]} for path in sources]},
        "source_policy": {"originals_untouched": True, "output_outside_source": True},
        "preparation": {
            "file_timeout_seconds": int(file_timeout),
            "batch_timeout_seconds": int(batch_timeout),
            "progress_path": str(progress_path),
            "completed_sources": len([item for item in preview_records if item["state"] == "preview-ready"]),
            "deferred_sources": len([item for item in preview_records if item["state"] == "preview-deferred"]),
            "failed_sources": len([item for item in preview_records if item["state"] == "preview-failed"]),
        },
        "asset_groups": groups,
        "preview_records": preview_records,
        "classification": {
            "status": "ai_review_required",
            "primary_categories": SUPPORTED_CATEGORIES,
            "animal_category_enabled": True,
            "reason": "Technical metrics cannot establish subject or photographic meaning; classify from the preview in the chat review step.",
        },
        "scoring": {
            "status": "ai_review_required",
            "formula": "keep_value × 65% + editability × 20% + expected_gain × 15%",
            "automatic_selection": "candidate_potential >= 75 and score_confidence >= 0.75 and all technical gates pass",
        },
        "next_step": "Render the preview_records in chat_review, then attach AI classification, score explanation, triage keys, and user decisions before any edit/export.",
    }
    manifest_path = output_root / "batch-manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="为照片技能准备本地 RAW/JPEG 批处理清单和预览")
    parser.add_argument("--input", required=True, help="输入照片文件夹")
    parser.add_argument("--output", required=True, help="输出运行文件夹，必须在输入文件夹之外")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--file-timeout", type=int, default=60, help="单个预览文件的最大处理秒数")
    parser.add_argument("--batch-timeout", type=int, default=0, help="整个预览阶段的最大秒数，0 表示不限制")
    args = parser.parse_args()
    result = prepare_batch(args.input, args.output, args.run_id, args.file_timeout, args.batch_timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
