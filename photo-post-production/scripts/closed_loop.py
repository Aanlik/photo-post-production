"""Pure closed-loop candidate acceptance, rollback and stopping decisions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def evaluate_iteration(
    previous: dict[str, Any],
    candidate: dict[str, Any],
    no_op_streak: int = 0,
    minimum_improvement: float = 0.5,
) -> dict[str, Any]:
    previous_score = float(previous.get("score", 0.0))
    candidate_score = float(candidate.get("score", 0.0))
    material_change = candidate.get("material_change") is not False
    technical_pass = candidate.get("technical_pass") is True
    semantic_pass = candidate.get("semantic_pass") is True
    accepted = bool(material_change and technical_pass and semantic_pass and candidate_score >= previous_score + minimum_improvement)
    next_streak = 0 if material_change else max(0, int(no_op_streak)) + 1
    stop = next_streak >= 2
    if stop:
        reason = "consecutive_no_effect_operations"
    elif not technical_pass:
        reason = "technical_gate_failed"
    elif not semantic_pass:
        reason = "semantic_gate_failed"
    elif not material_change:
        reason = "no_material_change"
    elif not accepted:
        reason = "improvement_below_threshold"
    else:
        reason = "candidate_improved"
    return {
        "accepted": accepted,
        "best": deepcopy(candidate if accepted else previous),
        "candidate": deepcopy(candidate),
        "no_op_streak": next_streak,
        "stop": stop or not technical_pass or not semantic_pass,
        "stopping_reason": reason,
        "score_delta": round(candidate_score - previous_score, 4),
    }


def select_best_iteration(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [item for item in candidates if item.get("technical_pass") is True and item.get("semantic_pass") is True and item.get("material_change") is not False]
    if not eligible:
        return None
    return deepcopy(max(eligible, key=lambda item: (float(item.get("score", 0.0)), -int(item.get("iteration", 0)))))
