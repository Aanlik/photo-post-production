"""Tests for explainable culling scores and post-edit outlook."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from score_explainer import build_score_explanation, compact_explanation  # noqa: E402


class ScoreExplainerTests(unittest.TestCase):
    def test_explains_weighted_formula_and_contributions(self) -> None:
        explanation = build_score_explanation({
            "primary_category": "urban-landscape",
            "keep_value": 84,
            "editability": 86.24,
            "expected_gain": 34.89,
            "candidate_potential": 77.08,
            "score_confidence": 0.70,
            "technical_gates": {"preview": "pass"},
        })
        self.assertTrue(explanation["available"])
        self.assertEqual(explanation["formula"], "84.00 × 65% + 86.24 × 20% + 34.89 × 15% = 77.08")
        self.assertEqual(explanation["components"][0]["contribution"], 54.6)
        self.assertEqual(explanation["components"][1]["contribution"], 17.25)
        self.assertEqual(explanation["components"][2]["contribution"], 5.23)
        self.assertIn("评分置信度 0.70", explanation["risks"][-1])
        self.assertIn("精修", explanation["post_edit_outlook"]["headline"])

    def test_does_not_claim_full_outlook_without_score_components(self) -> None:
        explanation = build_score_explanation({
            "primary_category": "animal-wildlife",
            "candidate_potential": 88,
        })
        self.assertFalse(explanation["available"])
        self.assertEqual(explanation["formula"], "评分构成不可用：缺少保留价值、可编辑性或预期收益")
        self.assertIn("主体", explanation["post_edit_outlook"]["recommended_treatments"][0])

    def test_compact_lines_are_suitable_for_chat(self) -> None:
        lines = compact_explanation({
            "primary_category": "animal-wildlife",
            "keep_value": 84,
            "editability": 88,
            "expected_gain": 30,
            "candidate_potential": 76.7,
            "score_confidence": 0.7,
        })
        self.assertEqual(len(lines), 3)
        self.assertIn("× 65%", lines[0])
        self.assertIn("后期方向", lines[2])


if __name__ == "__main__":
    unittest.main()
