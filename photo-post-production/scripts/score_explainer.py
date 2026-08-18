"""Explain photo selection scores without inventing unsupported visual evidence."""

from __future__ import annotations

import math
from typing import Any


_COMPONENTS = (
    (
        "keep_value",
        "保留价值",
        0.65,
        "照片本身的内容、瞬间、主体和叙事是否值得留下",
    ),
    (
        "editability",
        "可编辑性",
        0.20,
        "曝光、色彩、主体分离和裁切等后期是否有安全空间",
    ),
    (
        "expected_gain",
        "预期收益",
        0.15,
        "经过后期后，画面表达相对当前预览可能提升多少",
    ),
)

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

_CATEGORY_TREATMENTS = {
    "landscape-nature": [
        "平衡天空高光与地面暗部",
        "增强前中后景层次和局部纹理",
        "统一色彩并控制过饱和",
        "用裁切强化主体和视觉路径",
    ],
    "urban-landscape": [
        "压低灯牌/天空高光并保留夜色层次",
        "恢复建筑暗部但避免画面变灰",
        "校正建筑线条和透视",
        "强化主体建筑、倒影或前景关系",
    ],
    "architecture-urban-space": [
        "校正垂直线和透视",
        "控制窗户、灯光和天空高光",
        "清理边缘干扰并保留建筑纹理",
        "通过裁切强化几何秩序",
    ],
    "street-documentary": [
        "突出关键人物/动作并保留现场关系",
        "控制复杂背景和高反差光线",
        "统一肤色、环境色和局部对比",
        "必要时去除明确干扰物，但保留纪实语境",
    ],
    "portrait-environmental": [
        "调整人物脸部曝光和肤色",
        "增强眼神、动作或服装等叙事重点",
        "弱化背景竞争但保留环境信息",
        "检查皮肤、头发和身体边缘的自然度",
    ],
    "animal-wildlife": [
        "突出动物主体的眼睛、头部或关键动作",
        "恢复毛发/羽毛/皮肤纹理并抑制噪点",
        "降低背景干扰和色彩竞争",
        "用裁切加强主体姿态与呼吸空间",
    ],
    "plant-macro": [
        "确认焦平面并强化花瓣、叶片或细节纹理",
        "降低背景色彩和高光竞争",
        "保护自然边缘和细微色阶",
        "用裁切强化形态、节奏和留白",
    ],
    "other-unsupported": [
        "先人工确认主体和表达意图",
        "再决定是自然增强、裁切还是创意处理",
    ],
}


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _unique(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip() and item not in result:
            result.append(item)
    return result


def _band(score: float | None) -> tuple[str, str]:
    if score is None:
        return "不可判定", "缺少候选潜力或三项选择分，不能给出可靠排序结论。"
    if score >= 90:
        return "卓越候选", "基础强，值得进入精细后期并进行最终质量复核。"
    if score >= 80:
        return "强候选", "基础较强，适合进入精修队列；是否达到最终成片标准仍需看原图和后期结果。"
    if score >= 70:
        return "可用候选", "有保留和后期价值，建议结合缩略图、100%细节和后期预期再决定。"
    if score >= 60:
        return "边界候选", "需要明确的主题或风格理由才值得投入后期时间。"
    return "低优先级", "除非有特殊记忆、任务或个人偏好，否则不建议优先投入精修。"


def _component_sentence(label: str, value: float, description: str) -> str:
    if value >= 85:
        level = "高"
    elif value >= 70:
        level = "中上"
    elif value >= 50:
        level = "中等"
    else:
        level = "偏低"
    return f"{label} {value:.2f}/100（{level}）：{description}。"


def _risk_items(record: dict[str, Any], score_confidence: float | None) -> list[str]:
    risks: list[str] = []
    existing = record.get("risks")
    if isinstance(existing, list):
        risks.extend(item for item in existing if isinstance(item, str))
    gates = record.get("technical_gates")
    if isinstance(gates, dict):
        for name, status in gates.items():
            if status == "fail":
                risks.append(f"技术门槛失败：{name}")
            elif status == "warn":
                risks.append(f"技术风险需复核：{name}")
    technical_risk = record.get("technical_risk")
    if isinstance(technical_risk, str) and technical_risk.strip() and technical_risk not in {"无", "无锁定技术警告"}:
        risks.append(technical_risk)
    if score_confidence is not None and score_confidence < 0.75:
        risks.append(f"评分置信度 {score_confidence:.2f} 低于自动阈值 0.75")
    return _unique(risks)


def _outlook(
    category: str,
    score: float | None,
    editability: float | None,
    expected_gain: float | None,
    risks: list[str],
) -> dict[str, Any]:
    treatments = _CATEGORY_TREATMENTS.get(category, _CATEGORY_TREATMENTS["other-unsupported"])
    if isinstance(expected_gain, float) and expected_gain >= 50:
        headline = "后期提升空间大：可以做明显的层次、主体和色彩重建"
        visual_change = "从当前预览到成片预计会有明显变化，但必须保留原始主体和结构检查。"
    elif isinstance(expected_gain, float) and expected_gain >= 35:
        headline = "后期提升空间中上：适合做有方向的精修"
        visual_change = "预计可明显改善曝光层次、色彩统一和主体引导，但不应仅靠重度调色掩盖构图问题。"
    elif isinstance(editability, float) and editability >= 85:
        headline = "原片基础较强：以后期精修和质感统一为主"
        visual_change = "更适合控制高光/暗部、色彩、局部对比和裁切，目标是让照片更完整，而不是大幅改造。"
    elif isinstance(editability, float) and editability >= 70:
        headline = "可做轻到中度后期：先验证关键区域"
        visual_change = "可以改善观感，但需要先检查主体细节、噪点、边缘和裁切损失。"
    else:
        headline = "后期空间有限：除非表达意图明确，否则不建议重投入"
        visual_change = "后期可能只能改善局部观感，不能可靠地弥补主体、清晰度或结构性问题。"

    if any("失败" in risk or "fail" in risk for risk in risks):
        headline = "存在技术门槛问题：先复核，不进入自动精修"
        visual_change = "在技术门槛解除前，不应承诺可达到可分享或比赛目标。"
    if editability is None or expected_gain is None:
        headline = "评分构成不完整：先补齐证据，再判断是否值得精修"
        visual_change = "当前只有总分或部分信息，无法可靠推断后期收益；不能仅凭这个分数决定投入时间。"
        decision = "人工确认评分构成"
    else:
        decision = "建议进入精修候选" if score is not None and score >= 75 else "建议暂缓，除非有明确个人偏好"
    return {
        "headline": headline,
        "visual_change": visual_change,
        "recommended_treatments": treatments,
        "decision": decision,
        "limitations": [
            "这是基于预览和结构化评分的后期预期，不是已生成的成片承诺。",
            "最终判断必须通过 RAW 全分辨率、100%细节、前后对比和技术/语义检查。",
        ],
    }


def build_score_explanation(record: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable explanation and edit outlook for one score record.

    This function derives display-only guidance. It must not be used as the
    trusted evaluation input for quality gates or release decisions.
    """
    values = {field: _number(record.get(field)) for field, _, _, _ in _COMPONENTS}
    score = _number(record.get("candidate_potential"))
    if score is None and all(value is not None for value in values.values()):
        score = sum(values[field] * weight for field, _, weight, _ in _COMPONENTS)
    confidence = _number(record.get("score_confidence"))
    category = str(record.get("primary_category", record.get("category", "other-unsupported")))
    risks = _risk_items(record, confidence)
    band_label, band_description = _band(score)
    components: list[dict[str, Any]] = []
    why: list[str] = []
    if all(value is not None for value in values.values()):
        for field, label, weight, description in _COMPONENTS:
            value = values[field]
            assert value is not None
            contribution = value * weight
            components.append({
                "field": field,
                "label": label,
                "value": round(value, 2),
                "weight": weight,
                "contribution": round(contribution, 2),
            })
            why.append(_component_sentence(label, value, description))
        formula = (
            f"{values['keep_value']:.2f} × 65% + {values['editability']:.2f} × 20% + "
            f"{values['expected_gain']:.2f} × 15% = {score:.2f}"
        )
    else:
        formula = "评分构成不可用：缺少保留价值、可编辑性或预期收益"
        why.append(formula)

    strengths = record.get("strengths") if isinstance(record.get("strengths"), list) else []
    outlook = _outlook(category, score, values["editability"], values["expected_gain"], risks)
    confidence_note = (
        "评分置信度足够，可作为排序依据，但仍需看后期候选。"
        if confidence is not None and confidence >= 0.75
        else "评分置信度不足以自动选片，只能作为人工审核的参考。"
    )
    return {
        "available": bool(components),
        "score": round(score, 2) if score is not None else None,
        "score_band": band_label,
        "score_band_description": band_description,
        "formula": formula,
        "components": components,
        "why_score": why,
        "strengths": _unique(strengths),
        "risks": risks,
        "confidence": round(confidence, 2) if confidence is not None else None,
        "confidence_note": confidence_note,
        "post_edit_outlook": outlook,
    }


def compact_explanation(record: dict[str, Any]) -> list[str]:
    """Return short lines suitable for a labelled chat thumbnail."""
    explanation = build_score_explanation(record)
    if not explanation["available"]:
        category = _CATEGORY_LABELS.get(str(record.get("primary_category", record.get("category", ""))), "待确认")
        potential = record.get("candidate_potential", "--")
        return [f"{category}  候选潜力 {potential}"]
    outlook = explanation["post_edit_outlook"]
    return [
        explanation["formula"],
        f"判断：{outlook['decision']}；{outlook['headline']}",
        f"后期方向：{'、'.join(outlook['recommended_treatments'][:2])}",
    ]
