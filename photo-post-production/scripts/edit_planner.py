"""Build a small problem-driven atomic edit plan from score evidence."""

from __future__ import annotations

from typing import Any


def _operation(operation_type: str, reason: str, success: str, region: str, risk: str = "medium") -> dict[str, Any]:
    return {
        "type": operation_type,
        "reason": reason,
        "affected_region": region,
        "success_criteria": success,
        "risk": risk,
    }


def build_problem_driven_plan(score: dict[str, Any], max_operations: int = 3) -> dict[str, Any]:
    if max_operations < 1:
        raise ValueError("max_operations must be positive")
    category = str(score.get("primary_category") or "other-unsupported")
    outlook = score.get("edit_outlook") if isinstance(score.get("edit_outlook"), dict) else {}
    problem = str(outlook.get("largest_problem") or "；".join(str(item) for item in score.get("risks", []))).casefold()
    operations: list[dict[str, Any]] = []

    if any(word in problem for word in ("暗", "曝光", "面部", "主体")):
        operations.append(_operation("subject-relight", "主体或面部亮度不足", "主体可读性提高且高光不过曝", "primary-subject"))
    if any(word in problem for word in ("背景", "干扰", "竞争", "杂乱")):
        operations.append(_operation("background-restraint", "背景与主体竞争", "背景存在感降低但环境关系仍完整", "background"))
    if any(word in problem for word in ("裁切", "构图", "边缘", "留白")):
        operations.append(_operation("crop-and-straighten", "构图或边缘关系需要整理", "主体、肢体和重要环境元素保持完整", "global-frame", "high"))
    if category == "portrait-environmental" and not operations:
        operations.extend([
            _operation("skin-tone-correct", "人像需要检查肤色与环境色关系", "肤色自然且不影响背景中性色", "face-skin"),
            _operation("subject-relight", "建立人物与环境的明暗层级", "人物更可读且没有局部光晕", "primary-subject"),
        ])
    elif category == "plant-macro" and not operations:
        operations.extend([
            _operation("selective-sharpen", "强化焦平面细节", "焦点区域更清晰且背景噪点不增加", "focus-plane"),
            _operation("background-restraint", "降低背景色彩或亮度竞争", "主体边缘自然且背景更安静", "background"),
        ])
    elif not operations:
        operations.append(_operation("global-tone", "建立基础层次", "高光、暗部和主体层级同时改善", "global-frame"))
    return {
        "photo_id": score.get("photo_id"),
        "category": category,
        "largest_problem": outlook.get("largest_problem") or (score.get("risks") or ["基础层次需验证"])[0],
        "operations": operations[:max_operations],
        "max_operations": max_operations,
        "planner_version": "problem-driven-2026.08.17-v1",
    }
