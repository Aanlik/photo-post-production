"""Build and validate the executable operation graph for one photo.

The edit-plan contract describes a completed, provenance-bearing plan.  A
runtime graph is deliberately separate: it can contain planned operations
without inventing before/after hashes.  Once an adapter returns evidence, the
host can promote the completed operations into the strict edit-plan contract.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


OPERATION_TYPES = {
    "remove-element", "add-element", "generative-fill", "generative-expand",
    "replace-sky-or-background", "reshape-geometry", "large-crop",
    "relight-subject", "style-reconstruct",
}


def _photo_key(score: dict[str, Any]) -> str:
    value = score.get("photo_id") or score.get("stable_photo_id") or "photo"
    return str(value).replace("/", "-").replace(" ", "-")


def _face_mask_parameters(score: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded normalized mask from local Vision face evidence."""
    evidence = score.get("visual_evidence") if isinstance(score.get("visual_evidence"), dict) else {}
    boxes = evidence.get("face_boxes") if isinstance(evidence.get("face_boxes"), list) else []
    candidates: list[tuple[float, float, float, float]] = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        try:
            x, y, width, height = (float(box[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            candidates.append((x, y, width, height))
    if not candidates:
        return {}
    x, y, width, height = max(candidates, key=lambda value: value[2] * value[3])
    center_x = min(1.0, max(0.0, x + width / 2.0))
    center_y = min(1.0, max(0.0, 1.0 - y - height / 2.0))
    pad_x, pad_y = max(0.045, width * 1.35), max(0.060, height * 1.25)
    return {
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


def _operation(
    operation_id: str,
    operation_type: str,
    backend: str,
    reason: str,
    region: str,
    parameters: dict[str, Any],
    risk: str,
    checkpoint: str,
    generative: bool,
    input_layer: str,
    output_layer: str,
    depends_on: list[str] | None = None,
    adapter_operation: str | None = None,
    required: bool = True,
) -> dict[str, Any]:
    if operation_type not in OPERATION_TYPES:
        raise ValueError(f"unsupported operation type: {operation_type}")
    return {
        "operation_id": operation_id,
        "type": operation_type,
        "depends_on": list(depends_on or []),
        "backend": backend,
        "reason": reason,
        "affected_region": region,
        "parameters": deepcopy(parameters),
        "risk": risk,
        "checkpoint": checkpoint,
        "generative": bool(generative),
        "input_layer": input_layer,
        "output_layer": output_layer,
        "adapter_operation": adapter_operation,
        "required": bool(required),
        "status": "planned",
    }


def build_operation_graph(
    score: dict[str, Any],
    edit_plan: dict[str, Any],
    adapter_plans: dict[str, dict[str, Any]] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded, dependency-checked graph without claiming execution."""

    score = score if isinstance(score, dict) else {}
    edit_plan = edit_plan if isinstance(edit_plan, dict) else {}
    adapter_plans = adapter_plans if isinstance(adapter_plans, dict) else {}
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    key = _photo_key(score)
    category = str(score.get("primary_category", "other-unsupported"))
    operations: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    previous: str | None = None

    lightroom = adapter_plans.get("lightroom") if isinstance(adapter_plans.get("lightroom"), dict) else {}
    adapter_settings = lightroom.get("settings") if isinstance(lightroom.get("settings"), dict) else {}
    global_adjustments = edit_plan.get("global_adjustments")
    fallback_settings = global_adjustments[0] if isinstance(global_adjustments, list) and global_adjustments and isinstance(global_adjustments[0], dict) else {}
    global_settings = adapter_settings or fallback_settings
    if any(abs(float(value)) > 0.0001 for value in global_settings.values() if isinstance(value, (int, float))):
        item = _operation(
            f"{key}:lr-global",
            "relight-subject",
            "lightroom-mcp",
            "先完成全局曝光、动态范围和色彩基线，避免局部调整建立在错误底片上",
            "global-frame",
            global_settings,
            "low",
            "before-lightroom-global",
            False,
            "raw-develop",
            "lightroom-render",
            adapter_operation="set_develop_settings",
        )
        operations.append(item)
        previous = item["operation_id"]

    regions = edit_plan.get("regions") if isinstance(edit_plan.get("regions"), list) else []
    for region in regions:
        if not isinstance(region, dict):
            continue
        adjustments = region.get("adjustments") if isinstance(region.get("adjustments"), dict) else {}
        confidence = float(region.get("confidence", 0.0)) if isinstance(region.get("confidence"), (int, float)) else 0.0
        if not adjustments or confidence < 0.75:
            continue
        face_mask = _face_mask_parameters(score) if str(region.get("id")) == "subject-review" else {}
        if not face_mask:
            # A region adjustment without a geometric/semantic mask would
            # silently become an edge-to-edge Photoshop adjustment.
            continue
        item = _operation(
            f"{key}:ps-region-{region.get('id', 'subject')}",
            "relight-subject",
            "photoshop-fine-edit",
            str(region.get("purpose", "按语义区域完成局部塑形")),
            str(region.get("id", "subject-review")),
            {**adjustments, **face_mask, "mask_confidence": round(confidence, 4), "output_layer_name": "05 · 主体局部塑形"},
            "medium",
            f"before-region-{region.get('id', 'subject')}",
            False,
            "lightroom-render" if previous else "raw-develop",
            f"ps-layer-{region.get('id', 'subject')}",
            depends_on=[previous] if previous else [],
            adapter_operation="region_mask_operation",
        )
        operations.append(item)
        previous = item["operation_id"]

    photoshop = adapter_plans.get("photoshop", {})
    ps_operations = photoshop.get("operations") if isinstance(photoshop, dict) else []
    ps_operations = ps_operations if isinstance(ps_operations, list) else []
    mapping = {
        "layer_operation": ("relight-subject", "建立可回滚的 Photoshop 图层结构", True, False),
        "smart_object": ("relight-subject", "保留原始像素的智能对象工作层", True, False),
        "layer_mask": ("relight-subject", "建立并验证非破坏性语义蒙版", True, False),
        "healing": ("remove-element", "修复传感器污点和小型干扰", False, False),
        "clone_stamp": ("remove-element", "以受控取样修复局部干扰", False, False),
        "eraser_mask": ("remove-element", "使用蒙版擦除明确授权的局部内容", False, False),
        "content_aware_remove": ("remove-element", "清理明确的画面干扰", True, False),
        "apply_crop": ("large-crop", "执行已规划的构图裁切", True, False),
        "perspective_warp": ("reshape-geometry", "校正建筑或空间透视", True, False),
        "liquify": ("reshape-geometry", "对人物局部轮廓进行受控液化", True, False),
        "portrait_beauty": ("relight-subject", "进行受控的人物面部、肤色和轮廓精修", True, False),
        "region_mask_operation": ("relight-subject", "以受控蒙版建立主体明暗层次", True, False),
        "dodge_burn": ("relight-subject", "用局部明暗塑造主体层次", True, False),
        "selective_color": ("relight-subject", "以局部颜色调整建立色彩关系", True, False),
        "curves_local": ("relight-subject", "通过局部曲线塑造明暗层次", True, False),
        "noise_reduction": ("relight-subject", "抑制高 ISO 和生成式处理带来的噪点", True, False),
        "sharpening": ("relight-subject", "在 100% 检查下进行受控锐化", True, False),
        "recorded_action": ("style-reconstruct", "执行版本对应的风格化动作", True, False),
        "generative-fill": ("generative-fill", "执行有明确目的的生成式局部修复", True, True),
    }
    for adapter_operation in ps_operations:
        tool = str(adapter_operation.get("tool", "")) if isinstance(adapter_operation, dict) else ""
        if tool not in mapping:
            continue
        operation_type, reason, required, generative = mapping[tool]
        status = str(adapter_operation.get("status", "planned")) if isinstance(adapter_operation, dict) else "planned"
        if isinstance(adapter_operation, dict) and adapter_operation.get("applicable") is False:
            optional.append({"tool": tool, "status": "not-applicable", "reason": "operation_not_applicable_to_current_photo"})
            continue
        if tool == "generative-fill" and status.startswith("downgraded"):
            optional.append({"tool": tool, "status": status, "reason": adapter_operation.get("reason")})
            continue
        if tool == "generative-fill" and not (capabilities.get("generative", {}).get("planning_eligible") or capabilities.get("generative", {}).get("available")):
            optional.append({"tool": tool, "status": "downgraded-backend-unavailable", "reason": "no_eligible_generative_backend"})
            continue
        is_required = bool(adapter_operation.get("required", required)) if isinstance(adapter_operation, dict) else required
        # Content removal and generative repair are optional unless the plan
        # explicitly asks for them.  They remain visible in the manifest.
        if tool in {"content_aware_remove", "generative-fill", "healing", "clone_stamp", "eraser_mask"} and not is_required:
            optional.append({"tool": tool, "status": "planned-optional", "reason": reason})
            continue
        operation_suffix = tool.replace("_", "-")
        if isinstance(adapter_operation, dict) and adapter_operation.get("operation"):
            operation_suffix += "-" + str(adapter_operation.get("operation")).replace("_", "-")
        operation_id = f"{key}:ps-{operation_suffix}"
        base_operation_id = operation_id
        duplicate_index = 2
        while any(existing.get("operation_id") == operation_id for existing in operations):
            # Multiple correction layers can use the same Photoshop tool.  A
            # stable sequence suffix keeps every graph node addressable and
            # prevents a duplicate ID from creating a dependency cycle.
            operation_id = f"{base_operation_id}-{duplicate_index}"
            duplicate_index += 1
        operation_parameters = {
            "tool": tool,
            "variant": edit_plan.get("variant_name", "natural"),
        }
        if isinstance(adapter_operation, dict) and isinstance(adapter_operation.get("parameters"), dict):
            operation_parameters.update(deepcopy(adapter_operation["parameters"]))
        if isinstance(adapter_operation, dict) and adapter_operation.get("operation"):
            operation_parameters["layer_operation"] = adapter_operation.get("operation")
        item = _operation(
            operation_id,
            operation_type,
            "chat-window-imagegen" if tool == "generative-fill" else "photoshop-fine-edit",
            reason,
            "subject-review" if category != "architecture-urban-space" else "geometry-review",
            operation_parameters,
            "high" if generative or operation_type in {"large-crop", "reshape-geometry"} else "medium",
            f"before-{tool}",
            generative,
            "ps-layer-current" if previous else ("lightroom-render" if previous else "raw-develop"),
            f"ps-layer-{tool.replace('_', '-')}",
            depends_on=[previous] if previous else [],
            adapter_operation=tool,
            required=is_required,
        )
        if isinstance(adapter_operation, dict):
            if isinstance(adapter_operation.get("execution_route"), dict):
                item["execution_route"] = deepcopy(adapter_operation["execution_route"])
            # Preserve capability requirements on the runtime node.  The
            # executor needs these fields to fail closed for brush/geometry
            # tools instead of treating an unverified plan as executable.
            for field in ("requires_descriptor", "requires_100_percent_check", "requires_face_mask", "review", "applicable"):
                if field in adapter_operation:
                    item[field] = deepcopy(adapter_operation[field])
            if adapter_operation.get("problem_reason"):
                item["reason"] = str(adapter_operation["problem_reason"])
            if adapter_operation.get("success_criteria"):
                item["success_criteria"] = str(adapter_operation["success_criteria"])
        if tool == "generative-fill":
            item["status"] = "awaiting-host-imagegen"
            item["backend_locality"] = "chat-window"
            item["requires_visible_conversation_image"] = True
        operations.append(item)
        previous = operation_id

    checkpoints = [
        {"checkpoint_id": "source-read-only", "after_operation": None, "verified": True},
        *[
            {"checkpoint_id": item["checkpoint"], "after_operation": item["operation_id"], "verified": False}
            for item in operations
        ],
    ]
    return {
        "graph_version": "1.0",
        "photo_id": score.get("photo_id"),
        "variant_name": edit_plan.get("variant_name", "natural"),
        "status": "planned",
        "operations": operations,
        "optional_operations": optional,
        "checkpoints": checkpoints,
        "max_iterations": 3,
        "rollback_policy": "keep-best-verified-checkpoint",
        "dependency_order": [item["operation_id"] for item in operations],
        "execution_policy": {
            "source_read_only": True,
            "serialized_adobe_operations": True,
            "no_unverified_completion": True,
        },
    }


def validate_operation_graph(graph: dict[str, Any]) -> list[str]:
    """Validate runtime graph structure, including dependencies and cycles."""

    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["graph must be an object"]
    operations = graph.get("operations")
    if not isinstance(operations, list):
        return ["operations must be a list"]
    known: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, operation in enumerate(operations):
        prefix = f"operations[{index}]"
        if not isinstance(operation, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for field in ("operation_id", "type", "backend", "reason", "affected_region", "parameters", "risk", "checkpoint", "generative", "input_layer", "output_layer", "depends_on"):
            if field not in operation:
                errors.append(f"{prefix}.{field} is required")
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            continue
        if operation_id in known:
            errors.append(f"{prefix}.operation_id must be unique")
        known.add(operation_id)
        if operation.get("type") not in OPERATION_TYPES:
            errors.append(f"{prefix}.type is unsupported")
        if not isinstance(operation.get("depends_on"), list):
            errors.append(f"{prefix}.depends_on must be a list")
        else:
            dependencies[operation_id] = list(operation["depends_on"])
    for operation_id, deps in dependencies.items():
        for dependency in deps:
            if dependency not in known:
                errors.append(f"{operation_id} has broken dependency {dependency}")
            if dependency == operation_id:
                errors.append(f"{operation_id} has self-dependency")
    visiting: set[str] = set()
    visited: set[str] = set()

    def cyclic(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(cyclic(dep) for dep in dependencies.get(node, []) if dep in dependencies):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    if any(cyclic(node) for node in dependencies):
        errors.append("graph contains a dependency cycle")
    return errors


def completed_edit_plan(
    edit_plan: dict[str, Any],
    graph: dict[str, Any],
    operation_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Promote verified graph evidence into the strict edit-plan shape."""

    result = deepcopy(edit_plan)
    result["operations"] = [
        {key: deepcopy(value) for key, value in operation.items() if key in {
            "operation_id", "type", "depends_on", "backend", "reason", "affected_region", "parameters", "risk", "checkpoint", "generative", "input_layer", "output_layer",
        }}
        for operation in graph.get("operations", [])
        if isinstance(operation, dict) and operation.get("status") == "completed"
    ]
    result["operation_records"] = deepcopy(operation_records)
    return result
