"""Materialize readable executable operations in plans from an existing run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from edit_plans import attach_execution_audit, materialize_executable_plan  # noqa: E402
from operation_graph import validate_operation_graph  # noqa: E402


def repair_run_plans(run_dir: str) -> dict[str, Any]:
    root = Path(run_dir).expanduser().resolve()
    execution_path = root / "execution-results.json"
    execution_report = json.loads(execution_path.read_text(encoding="utf-8")) if execution_path.is_file() else {}
    executions = execution_report.get("executions") if isinstance(execution_report.get("executions"), list) else []
    repaired: list[str] = []
    errors: list[str] = []
    for path in sorted((root / "plans").glob("*/*/edit-plan.json")):
        manifest_path = path.with_name("edit-manifest.json")
        if not manifest_path.is_file():
            errors.append(f"missing operation graph: {manifest_path}")
            continue
        plan = json.loads(path.read_text(encoding="utf-8"))
        graph = json.loads(manifest_path.read_text(encoding="utf-8"))
        graph_errors = validate_operation_graph(graph)
        if graph_errors:
            errors.append(f"invalid operation graph {manifest_path}: {'; '.join(graph_errors)}")
            continue
        adapter_plan = plan.get("adapter_plan") if isinstance(plan.get("adapter_plan"), dict) else {}
        materialized = materialize_executable_plan(plan, adapter_plan, graph)
        stable_id, variant_name = path.parent.parent.name, path.parent.name
        execution = next((
            item for item in executions
            if isinstance(item, dict)
            and str(item.get("photo_id")) == stable_id
            and str(item.get("variant_name")) == variant_name
        ), {})
        runtime_path = next((
            candidate for candidate in sorted((root / "adobe-runtime").glob("lr-*.json"))
            if json.loads(candidate.read_text(encoding="utf-8")).get("operation_id")
            == f"{stable_id}:lr-global"
        ), None)
        lightroom_runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path else {}
        materialized = attach_execution_audit(materialized, execution, lightroom_runtime)
        path.write_text(json.dumps(materialized, ensure_ascii=False, indent=2), encoding="utf-8")
        repaired.append(str(path))
    return {"status": "completed" if not errors else "completed-with-errors", "repaired": repaired, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐历史照片运行中的可执行编辑计划")
    parser.add_argument("run_dir")
    args = parser.parse_args()
    result = repair_run_plans(args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
