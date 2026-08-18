"""Apply natural-language review decisions to a persisted pipeline report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from adapter_plans import build_lightroom_plan, build_photoshop_plan  # noqa: E402
from capability_registry import probe_generative_backend  # noqa: E402
from chat_window_image_backend import build_chat_job_from_score, create_batch_manifest  # noqa: E402
from durable_queue import enqueue  # noqa: E402
from edit_plans import build_variant_specs, materialize_executable_plan  # noqa: E402
from execution_engine import build_execution_jobs, execute_batch, merge_final_scores  # noqa: E402
from operation_graph import build_operation_graph, validate_operation_graph  # noqa: E402
from review_triage import build_triage_board, parse_batch_decisions  # noqa: E402
from style_memory import record_feedback  # noqa: E402


def _chat_window_jobs(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for item in scores:
        if item.get("decision") != "selected" or item.get("human_decision") != "keep":
            continue
        variants = item.get("variant_plans") if isinstance(item.get("variant_plans"), list) else []
        selected_variants = variants or [{"variant_name": "natural"}]
        for variant in selected_variants:
            variant_name = str(variant.get("variant_name", "natural"))
            job = build_chat_job_from_score(item, variant_name=variant_name)
            if job is not None:
                jobs.append(job)
    return jobs


def _approved_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only photos explicitly kept by the current human review."""

    return [
        item for item in scores
        if item.get("decision") == "selected" and item.get("human_decision") == "keep"
    ]


def apply_decisions(report_path: str, text: str, default_lane: str | None = None, output_path: str | None = None) -> dict[str, Any]:
    path = Path(report_path).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    scores = report.get("scores")
    if not isinstance(scores, list):
        raise ValueError("report does not contain scores")
    board = build_triage_board(scores)
    assignments = parse_batch_decisions(text, board, default_lane=default_lane)
    by_key = {item.get("review_key"): item for item in scores}
    for assignment in assignments:
        item = by_key.get(assignment["review_key"])
        if item is None:
            raise ValueError(f"review key not found in report: {assignment['review_key']}")
        item["human_decision"] = assignment["decision"]
        item["decision"] = {"keep": "selected", "reject": "rejected", "borderline": "review"}[assignment["decision"]]
        item.setdefault("audit", []).append({"type": "user-batch-decision", "decision": assignment["decision"]})
        style_db = report.get("style_memory", {}).get("db_path") if isinstance(report.get("style_memory"), dict) else None
        if style_db and item.get("photo_id"):
            record_feedback(
                str(style_db),
                str(report.get("run_id", "review")),
                str(item.get("photo_id")),
                "positive" if assignment["decision"] == "keep" else "negative",
                f"用户批量审核：{assignment['decision']}",
                None,
            )
    processing_locality = report.get("processing_locality", "local-only")
    capabilities = {
        "generative": probe_generative_backend(processing_locality=processing_locality),
        "photoshop": {
            "adapter": str(SCRIPT_DIR / "photoshop_mcp_adapter.py"),
            "configured": (SCRIPT_DIR / "photoshop_mcp_adapter.py").is_file(),
            "backend": "dcc-mcp-photoshop",
        },
        "lightroom": {
            "adapter": str(SCRIPT_DIR / "lightroom_mcp_adapter.py"),
            "configured": (SCRIPT_DIR / "lightroom_mcp_adapter.py").is_file(),
            "backend": "lightroom-mcp-server 0.9.0",
        },
    }
    queue_path = report.get("queue", {}).get("path") or str(path.parent / "queue.sqlite3")
    pending = []
    for item in scores:
        if item.get("decision") != "selected" or item.get("human_decision") != "keep":
            continue
        variants = build_variant_specs(item, intent=report.get("intent", "natural-enhancement"), max_candidates=3)
        generative = capabilities["generative"]
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
            variant["operation_graph"] = build_operation_graph(item, variant, variant["adapter_plan"], capabilities)
            variant["operation_graph_errors"] = validate_operation_graph(variant["operation_graph"])
            variant.update(materialize_executable_plan(variant, variant["adapter_plan"], variant["operation_graph"]))
        item["variant_plans"] = variants
        for variant in variants:
            variant_name = str(variant.get("variant_name", "natural"))
            item_id = enqueue(
                queue_path,
                report["run_id"],
                str(item.get("photo_id")),
                {"score": item, "variant": variant, "stage": "awaiting-lightroom-adapter", "approval": "user-approved"},
                item_id=f"{report['run_id']}:{item.get('photo_id')}:{variant_name}",
            )
            pending.append({"item_id": item_id, "photo_id": item.get("photo_id"), "variant_name": variant_name, "stage": "awaiting-lightroom-adapter", "approval": "user-approved"})
    report["scores"] = scores
    report["processing_locality"] = processing_locality
    report["capabilities"] = capabilities
    report["downgrades"] = [
        "local_adobe_adapter_pending" if pending else None,
        capabilities["generative"].get("reason") if not capabilities["generative"].get("available") else None,
    ]
    report["downgrades"] = [item for item in report["downgrades"] if item]
    report["queue"] = {"path": queue_path, "pending_adapter_work": pending}
    chat_batch = None
    if processing_locality != "local-only":
        jobs = _chat_window_jobs(scores)
        if jobs:
            chat_batch = create_batch_manifest(str(path.parent / "chat-window-results"), jobs, str(report["run_id"]), processing_locality=processing_locality)
        else:
            chat_batch = {"status": "not-created", "reason": "no-approved-preview-jobs"}
    report["chat_window_batch"] = chat_batch
    manifest_path = path.parent / "run-manifest.json"
    if manifest_path.is_file():
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Once the user has made an explicit batch decision, only the kept
        # records are execution-approved.  Auto-selected records that were
        # not part of the keep list must not leak back into the adapter queue.
        jobs = build_execution_jobs(_approved_scores(scores), str(report["run_id"]), str(path.parent), processing_locality, capabilities)
        execution = execute_batch(jobs, queue_path, str(path.parent), run_manifest, mode="auto", dry_run=False)
        merge_final_scores(scores, execution["quality"])
        report["execution"] = {"results": str(path.parent / "execution-results.json"), "job_count": len(jobs), "status": execution["quality"].get("status")}
        report["quality_report"] = str(path.parent / "quality-report.json")
        report["rollback_ledger"] = str(path.parent / "rollback-ledger.json")
    report.setdefault("review_decisions", []).append({"text": text, "default_lane": default_lane, "assignments": assignments})
    destination = Path(output_path).expanduser().resolve() if output_path else path
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report_path": str(destination), "assignments": assignments, "pending_adapter_work": pending, "chat_window_batch": chat_batch, "counts": {"selected": sum(item.get("decision") == "selected" for item in scores), "review": sum(item.get("decision") == "review" for item in scores), "rejected": sum(item.get("decision") == "rejected" for item in scores)}}


def main() -> int:
    parser = argparse.ArgumentParser(description="将聊天窗口中的自然语言审核决定写回照片运行报告")
    parser.add_argument("--report", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--default-lane", choices=("A", "B", "C"))
    parser.add_argument("--output")
    args = parser.parse_args()
    print(json.dumps(apply_decisions(args.report, args.decision, args.default_lane, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
