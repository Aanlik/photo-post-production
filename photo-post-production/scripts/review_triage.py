"""Build a compact, batch-decision review board for photo culling."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from score_explainer import build_score_explanation


_KEY_RE = re.compile(r"\b([ABC])(\d{1,3})(?:-([ABC])(\d{1,3}))?\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d{1,3}(?!\d)")
_ACTION_ALIASES = {
    "keep": "keep",
    "保留": "keep",
    "select": "keep",
    "reject": "reject",
    "淘汰": "reject",
    "删除": "reject",
    "不要": "reject",
    "不保留": "reject",
    "borderline": "borderline",
    "review": "borderline",
    "待定": "borderline",
    "复核": "borderline",
    "都要": "keep",
    "要": "keep",
}
_CATEGORY_LABELS = {
    "landscape-nature": "自然风景/植物",
    "urban-landscape": "城市风景",
    "architecture-urban-space": "建筑/城市空间",
    "street-documentary": "街头/纪实",
    "portrait-environmental": "人物/环境肖像",
    "animal-wildlife": "动物/野生动物",
    "plant-macro": "植物/微距",
    "other-unsupported": "其他/待确认",
}


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return float(value)
    return default


def _has_risk(record: dict) -> bool:
    gates = record.get("technical_gates")
    if isinstance(gates, dict) and any(value in {"warn", "fail"} for value in gates.values()):
        return True
    warnings = record.get("warnings")
    return isinstance(warnings, list) and bool(warnings)


def _candidate_key(record: dict) -> str:
    for field in ("photo_id", "filename", "source_path", "fixture_id"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("each review record needs photo_id, filename, source_path, or fixture_id")


def _normalise(records: list[dict]) -> list[dict]:
    normalised: list[dict] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("review records must be objects")
        identity = _candidate_key(record)
        if identity in seen:
            raise ValueError(f"duplicate review record: {identity}")
        seen.add(identity)
        item = dict(record)
        item["_identity"] = identity
        item["_potential"] = _finite(record.get("candidate_potential"), -1.0)
        item["_risk"] = _has_risk(record)
        normalised.append(item)
    return normalised


def build_triage_board(
    records: list[dict],
    priority_limit: int = 12,
    attention_limit: int = 12,
) -> dict:
    """Return a compact A/B/C board without silently rejecting any image.

    A is the small high-potential shortlist, B is the risk/borderline set, and
    C is deferred. Every item remains a human decision; lane names are only a
    review-order optimization.
    """
    if priority_limit < 1 or attention_limit < 1:
        raise ValueError("lane limits must be positive")
    items = _normalise(records)
    ordered = sorted(
        items,
        key=lambda item: (-item["_potential"], -_finite(item.get("preference_fit"), 50.0), item["_identity"]),
    )
    priority_pool = [item for item in ordered if not item["_risk"]]
    priority = priority_pool[:priority_limit]
    assigned = {item["_identity"] for item in priority}
    remaining = [item for item in ordered if item["_identity"] not in assigned]
    attention_pool = [item for item in remaining if item["_risk"]]
    attention_pool += [item for item in remaining if not item["_risk"] and item not in attention_pool]
    attention = attention_pool[:attention_limit]
    assigned.update(item["_identity"] for item in attention)
    deferred = [item for item in ordered if item["_identity"] not in assigned]

    lanes = {"A": priority, "B": attention, "C": deferred}
    board: dict[str, Any] = {
        "lanes": {},
        "counts": {"priority": len(priority), "attention": len(attention), "defer": len(deferred)},
        "category_counts": dict(Counter(str(item.get("primary_category", "other-unsupported")) for item in items)),
        "instructions": "直接回复批量决定，例如：保留 A01,A03-A06；淘汰 B02；其余待定。",
    }
    for lane, lane_items in lanes.items():
        name = {"A": "priority", "B": "attention", "C": "defer"}[lane]
        rendered = []
        for index, item in enumerate(lane_items, 1):
            rendered_item = dict(item)
            rendered_item.pop("_identity", None)
            rendered_item.pop("_potential", None)
            rendered_item.pop("_risk", None)
            rendered_item["review_key"] = f"{lane}{index:02d}"
            rendered_item["lane"] = name
            rendered_item["score_explanation"] = build_score_explanation(rendered_item)
            rendered.append(rendered_item)
        board["lanes"][lane] = rendered
    return board


def _expand_key(board: dict, lane: str, start: int, end_lane: str | None, end: int | None) -> list[str]:
    end_lane = (end_lane or lane).upper()
    if end_lane != lane:
        raise ValueError("ranges must stay within one lane")
    lane_items = board.get("lanes", {}).get(lane, [])
    if start < 1 or end is None or end < start or end > len(lane_items):
        raise ValueError(f"review key range out of bounds: {lane}{start}-{end_lane}{end}")
    return [f"{lane}{index:02d}" for index in range(start, end + 1)]


def _assign(assignments: dict[str, str], key: str, action: str, known: set[str]) -> None:
    if key not in known:
        raise ValueError(f"unknown review key: {key}")
    previous = assignments.get(key)
    if previous is not None and previous != action:
        raise ValueError(f"conflicting decisions for {key}")
    assignments[key] = action


def _parse_segment(
    segment: str,
    board: dict,
    known: set[str],
    assignments: dict[str, str],
    default_lane: str | None,
) -> None:
    action_matches = []
    occupied: list[tuple[int, int]] = []
    for alias in sorted(_ACTION_ALIASES, key=len, reverse=True):
        pattern = re.compile(re.escape(alias), re.IGNORECASE if alias.isascii() else 0)
        for match in pattern.finditer(segment):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            occupied.append(span)
            action_matches.append((match, _ACTION_ALIASES[alias]))
    action_matches.sort(key=lambda item: item[0].start())
    if len(action_matches) > 1:
        start = 0
        for match, _ in action_matches:
            _parse_segment(segment[start:match.end()], board, known, assignments, default_lane)
            start = match.end()
        return
    if not action_matches:
        raise ValueError(f"missing decision action in segment: {segment}")
    action = action_matches[0][1]
    matches = list(_KEY_RE.finditer(segment))
    lane_match = re.search(r"(?<![A-Za-z])([ABC])\s*区", segment, re.IGNORECASE)
    if not matches and lane_match:
        lane = lane_match.group(1).upper()
        for item in board.get("lanes", {}).get(lane, []):
            key = item.get("review_key")
            if isinstance(key, str):
                _assign(assignments, key, action, known)
        return
    if not matches and default_lane:
        bare_numbers = [int(value) for value in _BARE_NUMBER_RE.findall(segment)]
        if bare_numbers:
            lane = default_lane.upper()
            for number in bare_numbers:
                _assign(assignments, f"{lane}{number:02d}", action, known)
            return
    if not matches and "其余" in segment:
        lane_items = board.get("lanes", {}).get(default_lane.upper(), []) if default_lane else []
        keys = [item.get("review_key") for item in lane_items] if default_lane else sorted(known)
        for key in keys:
            if isinstance(key, str) and key not in assignments:
                _assign(assignments, key, action, known)
        return
    if not matches:
        raise ValueError(f"missing review keys in segment: {segment}")
    for match in matches:
        lane, start, end_lane, end = match.groups()
        keys = _expand_key(board, lane.upper(), int(start), end_lane, int(end) if end else int(start))
        for key in keys:
            _assign(assignments, key, action, known)


def parse_batch_decisions(text: str, board: dict, default_lane: str | None = None) -> list[dict]:
    """Parse Chinese/English batch decisions into review-key assignments.

    When the user is responding to one displayed lane, pass that lane as
    ``default_lane`` so shorthand such as ``8、11 淘汰，其余保留`` remains
    scoped to that lane instead of affecting other lanes.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("decision text must not be empty")
    if default_lane is not None and default_lane.upper() not in {"A", "B", "C"}:
        raise ValueError("default_lane must be A, B, or C")
    known = {
        item["review_key"]
        for lane_items in board.get("lanes", {}).values()
        for item in lane_items
    }
    assignments: dict[str, str] = {}
    for segment in re.split(r"[;；\n]+", text):
        segment = segment.strip()
        if not segment:
            continue
        _parse_segment(segment, board, known, assignments, default_lane)
    return [{"review_key": key, "decision": assignments[key]} for key in sorted(assignments)]


def render_markdown(board: dict, title: str = "快速审核板") -> str:
    """Render the board as a compact human-facing review sheet."""
    labels = {"A": "优先确认", "B": "重点风险/边界", "C": "暂不优先"}
    lines = [f"# {title}", "", board.get("instructions", ""), ""]
    for lane in ("A", "B", "C"):
        items = board.get("lanes", {}).get(lane, [])
        lines.append(f"## {lane}区：{labels[lane]}（{len(items)}张）")
        lines.append("")
        lines.append("| 编号 | 文件/照片 | 分类 | 分数构成 | 为什么 | 后期预期 | 技术风险 | 建议 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for item in items:
            gates = item.get("technical_gates", {})
            risk = "；".join(f"{key}:{value}" for key, value in gates.items() if value != "pass") or "无"
            explanation = item.get("score_explanation") or build_score_explanation(item)
            outlook = explanation["post_edit_outlook"]
            why = "；".join(explanation["why_score"][:2])
            formula = explanation["formula"]
            treatments = "、".join(outlook["recommended_treatments"][:2])
            human_decision = {
                "keep": "已确认保留",
                "reject": "已确认淘汰",
                "borderline": "已标记待定",
            }.get(item.get("human_decision"), outlook["decision"])
            lines.append(
                f"| `{item['review_key']}` | `{_candidate_key(item)}` | "
                f"{_CATEGORY_LABELS.get(item.get('primary_category'), item.get('primary_category', '其他/待确认'))} | "
                f"{formula} | {why} | {outlook['headline']}；{treatments} | {risk} | {human_decision} |"
            )
        lines.append("")
    return "\n".join(lines)
