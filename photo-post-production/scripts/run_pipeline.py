"""One-command local analysis and review pipeline for mixed RAW folders.

This is the deterministic host-side entry point. It prepares previews, runs
on-device visual evidence, writes score explanations and a chat review board,
creates candidate plans, and persists a resumable queue. Lightroom/Photoshop
calls are represented as pending adapter work until the host has independently
passed those MCP/bridge capability gates.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from capability_registry import probe_generative_backend  # noqa: E402
from adapter_plans import build_lightroom_plan, build_photoshop_plan  # noqa: E402
from calibration import build_project_calibration  # noqa: E402
from chat_review import render_chat_sheet  # noqa: E402
from chat_window_image_backend import build_chat_job_from_score, create_batch_manifest  # noqa: E402
from durable_queue import enqueue, get_item  # noqa: E402
from edit_plans import build_variant_specs, materialize_executable_plan  # noqa: E402
from execution_engine import _sync_execution_plan, build_execution_jobs, execute_batch, merge_final_scores  # noqa: E402
from prepare_batch import prepare_batch  # noqa: E402
from review_triage import build_triage_board, parse_batch_decisions, render_markdown  # noqa: E402
from run_manifest import create_run_manifest, seal_run_manifest, verify_run_manifest  # noqa: E402
from schema_validator import validate_edit_plan, validate_score_record  # noqa: E402
from reference_style import derive_style_recipe  # noqa: E402
from series_consistency import build_series_plan  # noqa: E402
from style_memory import get_profile, init_store  # noqa: E402
from visual_analysis import analyze_visual_paths  # noqa: E402
from operation_graph import build_operation_graph, validate_operation_graph  # noqa: E402
from pipeline_enrichment import enrich_scores, route_problem_plan  # noqa: E402
from action_descriptor_registry import init_registry  # noqa: E402


DEFAULT_APP_ROOT = Path.home() / "Library/Application Support/PhotoPostProduction"


def _brief(intent: str) -> dict[str, Any]:
    return {
        "project_goal": "从混合 RAW 照片中筛选并完成可分享的高完成度作品",
        "subject_priority": ["按视觉证据自动分类，动物单独处理"],
        "target_use": "shareable-photo",
        "mood": "有层次、可信、有摄影感",
        "photographer_intent": "保留主体与现场逻辑，允许用户授权的精细和变换式后期",
        "creative_intensity": 35,
        "source_fidelity": 80,
        "transformation_disclosure": "明确记录裁切、移除、添加、生成式和风格重构操作",
        "allowed_operations": [
            "remove-element", "add-element", "generative-fill", "generative-expand",
            "replace-sky-or-background", "reshape-geometry", "large-crop",
            "relight-subject", "style-reconstruct",
        ],
    }


def _write_scores_csv(path: Path, scores: list[dict[str, Any]]) -> None:
    fields = ["review_key", "source_path", "primary_category", "candidate_potential", "keep_value", "editability", "expected_gain", "score_confidence", "final_score", "final_score_source", "final_variant", "decision", "technical_gates"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for score in scores:
            writer.writerow({field: json.dumps(score.get(field), ensure_ascii=False) if field == "technical_gates" else score.get(field, "") for field in fields})


def _apply_user_decisions(scores: list[dict[str, Any]], board: dict[str, Any], decision_text: str | None, default_lane: str | None) -> list[dict[str, Any]]:
    if not decision_text:
        return scores
    assignments = parse_batch_decisions(decision_text, board, default_lane=default_lane)
    by_key = {item.get("review_key"): item for item in scores}
    for assignment in assignments:
        item = by_key.get(assignment["review_key"])
        if item is not None:
            item["human_decision"] = assignment["decision"]
            item["decision"] = {"keep": "selected", "reject": "rejected", "borderline": "review"}[assignment["decision"]]
            item.setdefault("audit", []).append({"type": "user-batch-decision", "decision": assignment["decision"]})
    return scores


def run_pipeline(
    input_dir: str,
    output_dir: str,
    run_id: str | None = None,
    mode: str = "auto",
    intent: str = "natural-enhancement",
    use_vision: bool = True,
    decision_text: str | None = None,
    default_lane: str | None = None,
    style_db: str | None = None,
    processing_locality: str = "mixed",
    max_candidates: int = 1,
) -> dict[str, Any]:
    if mode not in {"auto", "review"}:
        raise ValueError("mode must be auto or review")
    if processing_locality not in {"local-only", "mixed", "mixed-locality", "allow-cloud-generation"}:
        raise ValueError("processing_locality must be local-only, mixed, mixed-locality, or allow-cloud-generation")
    if int(max_candidates) != 1:
        raise ValueError("one-photo/one-PSD workflow only supports max_candidates=1")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_name = run_id or datetime.now(timezone.utc).strftime("photo-run-%Y%m%dT%H%M%SZ")
    prepared = prepare_batch(input_dir, str(output_root), run_name)
    contract = create_run_manifest(run_name, input_dir, intent, "full")
    contract["director_brief"] = _brief(intent)
    contract["content_policy"] = "user-authorized-transformative"
    contract["processing_locality"] = processing_locality
    contract["locality_policy"] = processing_locality
    contract["resource_budget"] = {
        "max_candidates": 1,
        "max_retries": 3,
        "max_iterations": 3,
        "concurrency": 1,
        "disk_budget_bytes": int(os.environ.get("PHOTO_POST_DISK_BUDGET_BYTES", str(40 * 1024 ** 3))),
        "time_budget_seconds": 0,
    }
    contract = seal_run_manifest(contract)
    if not verify_run_manifest(contract):
        raise RuntimeError("sealed run manifest failed verification")
    (output_root / "run-manifest.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    db_path = str(Path(style_db).expanduser().resolve()) if style_db else str(DEFAULT_APP_ROOT / "style-memory.sqlite3")
    init_store(db_path)
    profile = get_profile(db_path)
    records = []
    for item in prepared["preview_records"]:
        record = dict(item)
        record["filename"] = Path(str(item.get("source_path", "unknown"))).name
        record["photo_id"] = item.get("asset_group_id") or Path(record["filename"]).stem
        records.append(record)
    scores = analyze_visual_paths(records, use_vision=use_vision)
    scores = enrich_scores(scores, db_path, profile_name=str(profile.get("profile_name") or "default"))
    style_recipes: dict[str, dict[str, Any]] = {}
    if profile.get("references"):
        categories = {str(item.get("primary_category", "other-unsupported")) for item in scores}
        for category in categories:
            style_recipes[category] = derive_style_recipe(db_path, category=category)
    for item in scores:
        category = str(item.get("primary_category", "other-unsupported"))
        recipe = style_recipes.get(category, {})
        item["style_fit"] = min(95.0, 50.0 + float(recipe.get("recipe", {}).get("confidence", 0.0)) * 45.0) if recipe.get("status") == "derived" else 50.0
        if isinstance(item.get("score_record"), dict):
            item["score_record"]["style_fit"] = item["style_fit"]
        item["style_recipe"] = recipe
    valid_scores = []
    invalid_scores = []
    for item in scores:
        errors = validate_score_record(item.get("score_record", {}))
        item["contract_errors"] = errors
        if errors:
            item["decision"] = "review"
            if isinstance(item.get("score_record"), dict):
                item["score_record"]["decision"] = "review"
            item.setdefault("risks", []).append("评分记录契约未通过：" + ", ".join(errors[:3]))
            invalid_scores.append(item)
        else:
            valid_scores.append(item)
    board = build_triage_board(valid_scores + invalid_scores)
    scores = [item for lane in ("A", "B", "C") for item in board["lanes"].get(lane, [])]
    scores = _apply_user_decisions(scores, board, decision_text, default_lane)
    calibration = build_project_calibration(scores, intent=intent, mode=mode, sample_limit=5)
    series_plan = build_series_plan(scores)
    # Automatic mode queues high-confidence selected records; review mode only
    # prepares the board until the user explicitly supplies decisions.
    queue_path = output_root / "queue.sqlite3"
    capabilities = {
        "generative": probe_generative_backend(processing_locality=processing_locality),
        "photoshop": {
            "adapter": str(SCRIPT_DIR / "photoshop_mcp_adapter.py"),
            "configured": (SCRIPT_DIR / "photoshop_mcp_adapter.py").is_file(),
            "backend": "dcc-mcp-photoshop",
            "version": os.environ.get("PHOTO_PHOTOSHOP_VERSION", "0"),
            "stable_tools": ["subject-relight", "skin-tone-correct", "selective-sharpen"],
            # UI automation is opt-in.  A local adapter file alone is not
            # evidence that Photoshop is open, connected, or safe to drive.
            "ui_available": os.environ.get("PHOTO_POST_PHOTOSHOP_UI_AVAILABLE") == "1",
        },
        "lightroom": {
            "adapter": str(SCRIPT_DIR / "lightroom_mcp_adapter.py"),
            "configured": (SCRIPT_DIR / "lightroom_mcp_adapter.py").is_file(),
            "backend": "lightroom-mcp-server 0.9.0",
        },
        "hybrid_policy": {
            "global_edits": "local-lightroom-photoshop",
            "generative_edits": "chatgpt-built-in-imagegen" if processing_locality != "local-only" else "local-only",
            "chat_window_tool_required": processing_locality in {"mixed", "mixed-locality", "allow-cloud-generation"},
            "api_mode": False,
        },
    }
    descriptor_db = str(DEFAULT_APP_ROOT / "action-descriptors.sqlite3")
    init_registry(descriptor_db)
    for item in scores:
        item["problem_driven_plan"] = route_problem_plan(
            item,
            capabilities,
            descriptor_db,
            photoshop_version=str(capabilities["photoshop"].get("version") or "0"),
            document_mode="RGB",
            bit_depth=16,
        )
    pending = []
    chat_jobs = []
    for item in scores:
        if item.get("decision") != "selected":
            continue
        variants = build_variant_specs(item, intent=intent, max_candidates=1)
        generative = capabilities.get("generative", {})
        generative_planning_eligible = bool(generative.get("available") or generative.get("planning_eligible"))
        for variant in variants:
            variant["adapter_plan"] = {
                "lightroom": build_lightroom_plan(item, item.get("style_recipe"), variant["variant_name"]),
                "photoshop": build_photoshop_plan(
                    item,
                    variant["variant_name"],
                    generative_planning_eligible,
                    generative_capability=generative,
                    processing_locality=processing_locality,
                ),
            }
            variant["problem_driven_plan"] = item.get("problem_driven_plan", {})
            variant["operation_graph"] = build_operation_graph(item, variant, variant["adapter_plan"], capabilities)
            variant["operation_graph_errors"] = validate_operation_graph(variant["operation_graph"])
            variant.update(materialize_executable_plan(variant, variant["adapter_plan"], variant["operation_graph"]))
            # Runtime planning fields are intentionally separate from the
            # strict provenance-bearing edit-plan contract.
            runtime_only_fields = {
                "adapter_plan", "problem_driven_plan", "operation_graph",
                "operation_graph_errors", "contract_errors", "planned_operations",
                "optional_operations", "color_plan", "detail_plan", "plan_consistency",
                "execution_audit",
            }
            variant["contract_errors"] = validate_edit_plan({
                key: value for key, value in variant.items() if key not in runtime_only_fields
            })
            if variant["contract_errors"]:
                raise RuntimeError("invalid edit plan: " + "; ".join(variant["contract_errors"]))
        item["variant_plans"] = variants
        if mode == "auto":
            for variant in variants:
                variant_name = str(variant.get("variant_name", "natural"))
                item_id = f"{run_name}:{item.get('photo_id')}:{variant_name}"
                queue_item = enqueue(
                    str(queue_path),
                    run_name,
                    str(item.get("photo_id")),
                    {"score": item, "variant": variant, "stage": "awaiting-lightroom-adapter"},
                    item_id=item_id,
                )
                pending.append({"item_id": queue_item, "photo_id": item.get("photo_id"), "variant_name": variant_name, "stage": "awaiting-lightroom-adapter"})
                generative_operations = [
                    operation for operation in variant.get("operation_graph", {}).get("operations", [])
                    if isinstance(operation, dict) and operation.get("backend") == "chat-window-imagegen"
                ]
                requires_chat_generation = any(operation.get("required") is True for operation in generative_operations)
                if processing_locality != "local-only" and requires_chat_generation:
                    chat_job = build_chat_job_from_score(item, variant_name=variant_name)
                    if chat_job is not None:
                        chat_job["resume"] = {
                            "queue_path": str(queue_path),
                            "queue_item_id": item_id,
                            "execution_plan_path": str(output_root / "execution-plan.json"),
                            "next_stage": "photoshop-fine-edit-and-quality-gate",
                        }
                        chat_jobs.append(chat_job)
    board["mode"] = mode
    board["processing_locality"] = processing_locality
    board["pending_adapter_work"] = pending
    board["capabilities"] = capabilities
    (output_root / "review-board.md").write_text(render_markdown(board, title="照片后期快速审核板"), encoding="utf-8")
    sheet_paths = {}
    preview_dir = str(output_root / "previews")
    for lane in ("A", "B"):
        lane_records = board["lanes"].get(lane, [])
        if lane_records:
            sheet = render_chat_sheet(lane_records, preview_dir, str(output_root / f"review-{lane}.jpg"), lane)
            sheet_paths[lane] = sheet
    chat_batch = None
    if processing_locality != "local-only":
        if chat_jobs:
            chat_batch = create_batch_manifest(
                str(output_root / "chat-window-results"),
                chat_jobs,
                run_name,
                processing_locality=processing_locality,
            )
        else:
            chat_batch = {"status": "not-created", "reason": "no-required-generative-operations"}
    execution_jobs = build_execution_jobs(scores, run_name, str(output_root), processing_locality, capabilities)
    execution = execute_batch(
        execution_jobs,
        str(queue_path),
        str(output_root),
        contract,
        mode=mode,
        dry_run=False,
    )
    merge_final_scores(scores, execution["quality"])
    final_pending = []
    for job, execution_item in zip(execution_jobs, execution.get("execution", {}).get("executions", [])):
        item_id = str(job.get("item_id") or job.get("job_id") or "")
        queue_item_state = get_item(str(queue_path), item_id) if item_id else None
        if not isinstance(queue_item_state, dict) or queue_item_state.get("state") not in {"queued", "processing", "paused", "failed"}:
            continue
        final_pending.append({
            "item_id": item_id,
            "photo_id": job.get("photo_id"),
            "variant_name": job.get("variant_name"),
            "stage": "adapter-retry" if execution_item.get("status") != "completed" else "quality-review",
            "state": queue_item_state.get("state"),
            "reason": (execution_item.get("blockers") or [None])[0],
        })
    board["pending_adapter_work"] = final_pending
    (output_root / "review-board.md").write_text(render_markdown(board, title="照片后期快速审核板"), encoding="utf-8")
    _write_scores_csv(output_root / "scores.csv", scores)
    report = {
        "status": (
            "completed"
            if execution["quality"].get("status") == "completed" and not final_pending
            else "completed-with-quality-gates"
            if execution["quality"].get("status") == "completed-with-quality-gates" and not final_pending
            else "pending-adapter-or-review"
        ),
        "run_id": run_name,
        "mode": mode,
        "intent": intent,
        "processing_locality": processing_locality,
        "locality_policy": processing_locality,
        "input_dir": str(Path(input_dir).expanduser().resolve()),
        "output_dir": str(output_root),
        "source_snapshot_sha256": contract["source_snapshot_sha256"],
        "classification": {"status": "completed-with-local-vision" if use_vision else "completed-without-vision"},
        "scores": scores,
        "counts": {"total": len(scores), "selected": sum(item.get("decision") == "selected" for item in scores), "review": sum(item.get("decision") == "review" for item in scores), "rejected": sum(item.get("decision") == "rejected" for item in scores)},
        "style_memory": {"db_path": db_path, "profile_version": profile.get("version"), "reference_count": len(profile.get("references", [])), "recipe_count": len(profile.get("style_recipes", [])) + len(style_recipes), "recipes_used": style_recipes},
        "calibration": calibration,
        "series": series_plan,
        "review_board": {"markdown": str(output_root / "review-board.md"), "sheets": sheet_paths},
        "chat_window_batch": chat_batch,
        "execution": {"results": str(output_root / "execution-results.json"), "job_count": len(execution_jobs), "status": execution["quality"].get("status")},
        "quality_report": str(output_root / "quality-report.json"),
        "rollback_ledger": str(output_root / "rollback-ledger.json"),
        "queue": {"path": str(queue_path), "pending_adapter_work": final_pending},
        "downgrades": [
            "local_adobe_adapter_pending" if final_pending else None,
            board["capabilities"]["generative"].get("reason")
            if not board["capabilities"]["generative"].get("planning_eligible", board["capabilities"]["generative"].get("available"))
            else None,
            "chat_window_imagegen_requires_current_host_tool"
            if chat_jobs and board["capabilities"]["generative"].get("requires_host_tool") and not board["capabilities"]["generative"].get("ready_for_execution")
            else None,
        ],
        "next_step": (
            "本次已完成 Lightroom RAW 解码、Photoshop 精修、PSD/JPG 导出和质量门验证；请从 quality-report.json 或 selected/ 查看结果。"
            if execution["quality"].get("status") in {"completed", "completed-with-quality-gates"} and not final_pending
            else "自动模式会继续执行已配置的本地适配器；等待态任务会从 execution-results.json 的检查点恢复。"
        ),
    }
    report["downgrades"] = [item for item in report["downgrades"] if item]
    (output_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "rejected.json").write_text(
        json.dumps([item for item in scores if item.get("decision") == "rejected"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    execution_plan_path = output_root / "execution-plan.json"
    execution_plan_path.write_text(json.dumps({"run_manifest": str(output_root / "run-manifest.json"), "items": pending, "jobs": execution_jobs, "adapter_policy": board["capabilities"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    _sync_execution_plan(execution_plan_path, execution.get("execution", {}))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="运行照片后期本地分析、评分、审核板和可恢复队列")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--mode", choices=("auto", "review"), default="auto")
    parser.add_argument("--intent", default="natural-enhancement")
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--decision")
    parser.add_argument("--default-lane", choices=("A", "B", "C"))
    parser.add_argument("--style-db")
    parser.add_argument("--processing-locality", choices=("local-only", "mixed", "mixed-locality", "allow-cloud-generation"), default="mixed")
    parser.add_argument("--max-candidates", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run_pipeline(args.input, args.output, args.run_id, args.mode, args.intent, not args.no_vision, args.decision, args.default_lane, args.style_db, args.processing_locality, args.max_candidates), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
