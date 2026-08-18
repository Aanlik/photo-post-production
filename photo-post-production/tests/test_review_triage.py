"""Tests for compact batch review decisions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from review_triage import build_triage_board, parse_batch_decisions, render_markdown  # noqa: E402


def record(name: str, potential: float, risk: bool = False, category: str = "animal-wildlife") -> dict:
    return {
        "filename": name,
        "primary_category": category,
        "candidate_potential": potential,
        "technical_gates": {"preview": "warn" if risk else "pass"},
    }


class ReviewTriageTests(unittest.TestCase):
    def test_board_limits_priority_and_keeps_deferred_items(self) -> None:
        board = build_triage_board([record(f"DSC{i:05d}.ARW", 90 - i, risk=i == 2) for i in range(1, 9)], priority_limit=3, attention_limit=2)
        self.assertEqual(board["counts"], {"priority": 3, "attention": 2, "defer": 3})
        self.assertEqual(board["lanes"]["A"][0]["review_key"], "A01")
        self.assertEqual(board["lanes"]["B"][0]["review_key"], "B01")
        self.assertEqual(board["lanes"]["C"][0]["review_key"], "C01")

    def test_batch_decision_expands_ranges(self) -> None:
        board = build_triage_board([record(f"DSC{i:05d}.ARW", 90 - i) for i in range(1, 7)], priority_limit=6, attention_limit=1)
        decisions = parse_batch_decisions("保留 A01-A03；淘汰 A04；其余待定 A05-A06", board)
        self.assertEqual(decisions[:3], [
            {"review_key": "A01", "decision": "keep"},
            {"review_key": "A02", "decision": "keep"},
            {"review_key": "A03", "decision": "keep"},
        ])
        self.assertEqual(decisions[-1], {"review_key": "A06", "decision": "borderline"})

    def test_batch_decision_rejects_cross_lane_ranges(self) -> None:
        board = build_triage_board([record(f"DSC{i:05d}.ARW", 90 - i) for i in range(1, 7)], priority_limit=2, attention_limit=2)
        with self.assertRaises(ValueError):
            parse_batch_decisions("保留 A01-B02", board)

    def test_batch_decision_supports_remaining_items(self) -> None:
        board = build_triage_board([record(f"DSC{i:05d}.ARW", 90 - i) for i in range(1, 4)], priority_limit=3, attention_limit=1)
        decisions = parse_batch_decisions("保留 A01；其余待定", board)
        self.assertEqual(decisions, [
            {"review_key": "A01", "decision": "keep"},
            {"review_key": "A02", "decision": "borderline"},
            {"review_key": "A03", "decision": "borderline"},
        ])

    def test_batch_decision_supports_lane_wide_natural_language(self) -> None:
        board = build_triage_board([record(f"DSC{i:05d}.ARW", 90 - i) for i in range(1, 4)], priority_limit=3, attention_limit=1)
        decisions = parse_batch_decisions("A 区都保留", board)
        self.assertEqual(decisions, [
            {"review_key": "A01", "decision": "keep"},
            {"review_key": "A02", "decision": "keep"},
            {"review_key": "A03", "decision": "keep"},
        ])

    def test_batch_decision_scopes_bare_numbers_and_remaining_to_lane(self) -> None:
        board = {"lanes": {"B": [{"review_key": f"B{i:02d}"} for i in range(1, 5)]}}
        decisions = parse_batch_decisions("2、3 淘汰，其余的保留", board, default_lane="B")
        self.assertEqual(decisions, [
            {"review_key": "B01", "decision": "keep"},
            {"review_key": "B02", "decision": "reject"},
            {"review_key": "B03", "decision": "reject"},
            {"review_key": "B04", "decision": "keep"},
        ])

    def test_batch_decision_supports_keep_and_reject_colloquialisms(self) -> None:
        board = {"lanes": {"C": [{"review_key": f"C{i:02d}"} for i in range(1, 4)]}}
        decisions = parse_batch_decisions("1、3 不要，其余的都要", board, default_lane="C")
        self.assertEqual(decisions, [
            {"review_key": "C01", "decision": "reject"},
            {"review_key": "C02", "decision": "keep"},
            {"review_key": "C03", "decision": "reject"},
        ])

    def test_render_mentions_fast_batch_workflow(self) -> None:
        board = build_triage_board([record("DSC01399.ARW", 88)], priority_limit=1, attention_limit=1)
        rendered = render_markdown(board)
        self.assertIn("A01", rendered)
        self.assertIn("动物/野生动物", rendered)
        self.assertIn("分数构成", rendered)
        self.assertIn("评分构成不可用", rendered)
        self.assertIn("人工确认", rendered)

    def test_board_attaches_score_explanation(self) -> None:
        board = build_triage_board([{
            **record("sample-photo.ARW", 77.08),
            "keep_value": 84,
            "editability": 86.24,
            "expected_gain": 34.89,
            "score_confidence": 0.70,
        }], priority_limit=1, attention_limit=1)
        explanation = board["lanes"]["A"][0]["score_explanation"]
        self.assertEqual(explanation["formula"], "84.00 × 65% + 86.24 × 20% + 34.89 × 15% = 77.08")
        self.assertIn("后期预期", render_markdown(board))

    def test_render_shows_human_decision_when_recorded(self) -> None:
        board = build_triage_board([record("sample-photo.ARW", 77.08)], priority_limit=1, attention_limit=1)
        board["lanes"]["A"][0]["human_decision"] = "keep"
        self.assertIn("已确认保留", render_markdown(board))


if __name__ == "__main__":
    unittest.main()
