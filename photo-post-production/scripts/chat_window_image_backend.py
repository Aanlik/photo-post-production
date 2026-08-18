"""Host-managed batch orchestration for Codex's built-in image_gen tool.

The built-in image tool is exposed by the current ChatGPT/Codex host, not by a
local HTTP endpoint.  This module therefore creates durable jobs and records
host-generated results; it intentionally never calls an API or pretends that a
filesystem path can be passed directly to the built-in tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


BACKEND_NAME = "chatgpt-built-in-imagegen"
SUPPORTED_INPUT_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
RAW_SUFFIXES = {".arw", ".cr2", ".cr3", ".dng", ".nef", ".nrw", ".orf", ".raf", ".rw2", ".pef", ".srw"}
_CATEGORY_LABELS = {
    "landscape-nature": "风光/植物",
    "urban-landscape": "城市风景",
    "architecture-urban-space": "建筑/城市空间",
    "street-documentary": "街头/纪实",
    "portrait-environmental": "人物/环境肖像",
    "animal-wildlife": "动物/野生动物",
    "other-unsupported": "其他/待确认",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_flag() -> bool:
    value = os.environ.get("PHOTO_CHAT_WINDOW_IMAGEGEN_AVAILABLE")
    return bool(value and value.strip().casefold() in {"1", "true", "yes", "on"})


def probe_chat_window_image_backend(tool_available: bool | None = None) -> dict[str, Any]:
    """Return host capability without uploading or opening a local image."""

    ready = _tool_flag() if tool_available is None else bool(tool_available)
    return {
        "backend": BACKEND_NAME,
        "api_mode": False,
        "locality": "chat-window",
        "available": ready,
        "planning_eligible": True,
        "ready_for_execution": ready,
        "healthy": True if ready else None,
        "verified": ready,
        "requires_host_tool": True,
        "requires_visible_conversation_image": True,
        "reason": None if ready else "host_built_in_imagegen_tool_must_be_available_in_current_session",
    }


def _validate_source(source: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.casefold()
    if suffix in RAW_SUFFIXES:
        raise ValueError("raw_input_requires_local_preview_before_chat_window_edit")
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError(f"unsupported_chat_window_input:{suffix}")


def build_chat_job_from_score(score: dict[str, Any], variant_name: str = "natural") -> dict[str, Any] | None:
    """Build the internal default job without asking the user for a prompt."""

    preview_value = score.get("preview_path")
    if not isinstance(preview_value, str) or not preview_value.strip():
        return None
    preview = Path(preview_value).expanduser().resolve()
    if not preview.is_file() or preview.suffix.casefold() not in SUPPORTED_INPUT_SUFFIXES:
        return None
    category = _CATEGORY_LABELS.get(str(score.get("primary_category", "other-unsupported")), "其他/待确认")
    treatments = score.get("recommended_treatment")
    if not isinstance(treatments, list):
        treatments = []
    treatment = "、".join(str(value) for value in treatments[:2] if value) or "仅执行能改善画面且通过质量检查的生成式修改"
    photo_id = str(score.get("photo_id") or preview.stem)
    return {
        "job_id": f"{photo_id}-{variant_name}",
        "review_key": score.get("review_key"),
        "photo_id": score.get("photo_id"),
        "variant_name": variant_name,
        "source_path": str(preview),
        "prompt": (
            f"这是{category}照片，按{variant_name}方向进行精细生成式后期。"
            f"目标：{treatment}。自动执行必要的移除、补全、扩图或局部重绘；"
            "保留主体身份、主要轮廓、真实光线关系、重要边缘、建筑线条、文字和摄影现场逻辑，"
            "不要凭空增加主体，不要改变未授权区域。"
        ),
        "invariants": [
            "保留主体身份与主要轮廓",
            "保留未授权区域、文字、建筑线条和现场关系",
            "不得改变摄影作品的核心叙事",
        ],
    }


def create_batch_manifest(
    output_dir: str,
    jobs: Iterable[dict[str, Any]],
    run_id: str,
    processing_locality: str = "mixed",
) -> dict[str, Any]:
    """Create a durable, host-executable chat-window batch manifest.

    Each job is one image_gen call.  The host must make the target image
    visible in the current conversation first, then use the job's prompt and
    invariants.  The manifest never embeds image bytes or API credentials.
    """

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    normalized: list[dict[str, Any]] = []
    for index, raw_job in enumerate(jobs, start=1):
        if not isinstance(raw_job, dict):
            raise ValueError("chat_window_job_must_be_object")
        source_value = raw_job.get("source_path")
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError("chat_window_job_source_path_required")
        source = Path(source_value).expanduser().resolve()
        _validate_source(source)
        prompt = raw_job.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("chat_window_job_prompt_required")
        job_id = str(raw_job.get("job_id") or f"chat-image-{index:03d}")
        normalized.append({
            "job_id": job_id,
            "review_key": raw_job.get("review_key"),
            "photo_id": raw_job.get("photo_id"),
            "variant_name": raw_job.get("variant_name", "natural"),
            "status": "awaiting-host-imagegen",
            "source_path": str(source),
            "source_sha256": _sha256(source),
            "input_role": "edit-target",
            "prompt": prompt.strip(),
            "invariants": list(raw_job.get("invariants", [])) if isinstance(raw_job.get("invariants", []), list) else [],
            "mask_path": str(Path(raw_job["mask_path"]).expanduser().resolve()) if raw_job.get("mask_path") else None,
            "mask_handling": "conversation-visible-reference-only",
            "backend": BACKEND_NAME,
            "backend_locality": "chat-window",
            "requires_visible_conversation_image": True,
            "output_dir": str(destination),
            "output_path": None,
            "output_sha256": None,
            "resume": dict(raw_job.get("resume", {})) if isinstance(raw_job.get("resume"), dict) else {},
            "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        })
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "backend": BACKEND_NAME,
        "api_mode": False,
        "processing_locality": processing_locality,
        "execution": "one-built-in-imagegen-call-per-job",
        "jobs": normalized,
        "disclosure": "生成式步骤通过当前 ChatGPT/Codex 聊天窗口的内置 image_gen 工具执行，不使用 OpenAI API Key。",
    }
    manifest_path = destination / "chat-window-image-batch.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def record_result(manifest_path: str, job_id: str, generated_path: str, note: str | None = None) -> dict[str, Any]:
    """Copy one host result and create a durable next-stage resume request.

    The host image tool and the local Adobe bridge are separate capabilities.
    Recording the image therefore closes only the generation step; it queues
    the same photo for the next Photoshop/quality/export stage instead of
    claiming that a final deliverable already exists.
    """

    path = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("chat_window_manifest_jobs_missing")
    job = next((item for item in jobs if isinstance(item, dict) and item.get("job_id") == job_id), None)
    if job is None:
        raise ValueError(f"chat_window_job_not_found:{job_id}")
    source = Path(generated_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.casefold() not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError("chat_window_result_must_be_image")
    output_dir = Path(str(job.get("output_dir", path.parent))).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(str(job.get("source_path", job_id))).stem
    destination = output_dir / f"{stem}.chatgen{source.suffix.casefold()}"
    counter = 1
    while destination.exists():
        destination = output_dir / f"{stem}.chatgen-{counter:02d}{source.suffix.casefold()}"
        counter += 1
    shutil.copy2(source, destination)
    job.update({
        "status": "completed",
        "host_generated_path": str(source),
        "output_path": str(destination),
        "output_sha256": _sha256(destination),
        "result_note": note,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    })
    resume = job.get("resume") if isinstance(job.get("resume"), dict) else {}
    resume_request = {
        "run_id": manifest.get("run_id"),
        "job_id": job_id,
        "photo_id": job.get("photo_id"),
        "variant_name": job.get("variant_name", "natural"),
        "generated_path": str(destination),
        "generated_sha256": job["output_sha256"],
        "source_path": job.get("source_path"),
        "source_sha256": job.get("source_sha256"),
        "next_stage": "photoshop-fine-edit-and-quality-gate",
        "status": "queued-for-adobe-resume" if resume else "recorded-awaiting-next-stage",
        "resume": resume,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }
    resume_path = output_dir / f"{stem}.chatgen-resume.json"
    resume_path.write_text(json.dumps(resume_request, ensure_ascii=False, indent=2), encoding="utf-8")
    job["resume_request_path"] = str(resume_path)
    job["resume_status"] = resume_request["status"]
    queue_path = resume.get("queue_path")
    queue_item_id = resume.get("queue_item_id")
    if isinstance(queue_path, str) and isinstance(queue_item_id, str):
        try:
            from durable_queue import get_item, transition, update_checkpoint

            current = get_item(queue_path, queue_item_id)
            if current is not None:
                update_checkpoint(queue_path, queue_item_id, {
                    "stage": "chat-window-imagegen-completed",
                    "generated_path": str(destination),
                    "generated_sha256": job["output_sha256"],
                    "resume_request_path": str(resume_path),
                })
                if current.get("state") in {"paused", "failed"}:
                    transition(queue_path, queue_item_id, "queued")
        except (OSError, ValueError, KeyError):
            job["resume_status"] = "recorded-queue-update-failed"
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    manifest["completed_jobs"] = sum(item.get("status") == "completed" for item in jobs if isinstance(item, dict))
    manifest["pending_jobs"] = sum(item.get("status") != "completed" for item in jobs if isinstance(item, dict))
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "manifest_path": str(path),
        "job_id": job_id,
        "output_path": str(destination),
        "output_sha256": job["output_sha256"],
        "resume_request_path": str(resume_path),
        "resume_status": job.get("resume_status"),
        "status": job["status"],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="创建或回写聊天窗口 image_gen 批处理清单")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--job-id")
    parser.add_argument("--generated")
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe_chat_window_image_backend(), ensure_ascii=False, indent=2))
        return 0
    if not args.manifest or not args.job_id or not args.generated:
        parser.error("回写结果需要 --manifest、--job-id 和 --generated")
    print(json.dumps(record_result(args.manifest, args.job_id, args.generated), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
