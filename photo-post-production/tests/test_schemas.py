"""Contract tests for score records and reversible edit plans."""

from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
REFERENCES_DIR = Path(__file__).resolve().parents[1] / "references"
sys.path.insert(0, str(SCRIPTS_DIR))

from schema_validator import validate_edit_plan, validate_score_record  # noqa: E402


def urban_night_score_record() -> dict:
    return {
        "primary_category": "urban-landscape",
        "secondary_tags": ["night", "reflection"],
        "classification_confidence": 0.91,
        "score_version": "1.1",
        "evidence": ["histogram", "duplicate-cluster", "visual-review"],
        "score_confidence": 0.84,
        "style_fit": 72,
        "technical_gates": {
            "source_readable": "pass",
            "irrecoverable_defect": "pass",
        },
        "technical": 78,
        "composition": 84,
        "light_color": 80,
        "moment_story": 62,
        "coherence": 82,
        "photographic_value": 78,
        "editability": 88,
        "expected_gain": 14,
        "keep_value": 78,
        "candidate_potential": 70.4,
        "final_score": None,
        "decision": "review",
        "strengths": ["central tower", "layered reflections"],
        "risks": ["bright signs", "dark lower buildings"],
        "recommended_treatment": ["recover shadows", "control signage highlights"],
    }


def complete_edit_plan() -> dict:
    return {
        "director_brief": {
            "project_goal": "Produce a readable, atmospheric night cityscape.",
            "subject_priority": ["central tower", "water reflections"],
            "target_use": "competition-quality",
            "mood": "controlled night atmosphere",
            "photographer_intent": "Preserve the tower as the visual anchor.",
            "creative_intensity": 62,
            "source_fidelity": 78,
            "transformation_disclosure": "Disclose all content transformations.",
            "allowed_operations": ["generative-fill", "relight-subject"],
        },
        "intent": "competition-standard",
        "edit_authority": "full",
        "content_policy": "user-authorized-transformative",
        "adjustment_budget": {
            "max_global_adjustments": 12,
            "max_local_adjustments": 8,
            "max_transformative_operations": 3,
            "max_exposure_delta": 2.0,
            "max_crop_fraction": 0.35,
            "max_temperature_delta": 3000,
            "max_sharpening": 120,
            "max_geometry_delta": 15,
            "max_candidates": 4,
        },
        "regions": [
            {
                "id": "main-building",
                "label": "subject architecture",
                "mask_type": "semantic",
                "purpose": "Make the central tower readable without flattening the night.",
                "adjustments": {"exposure": 0.25, "texture": 12, "clarity": 8},
                "confidence": 0.86,
                "forbidden_changes": ["do not alter building geometry"],
            }
        ],
        "operations": [
            {
                "operation_id": "recover-signage",
                "type": "generative-fill",
                "depends_on": [],
                "backend": "photoshop",
                "reason": "Repair a distracting clipped sign edge.",
                "affected_region": "main-building",
                "parameters": {"feather": 4},
                "risk": "medium",
                "checkpoint": "before-signage-repair",
                "generative": True,
                "input_layer": "base-render",
                "output_layer": "signage-repair",
            }
        ],
        "operation_records": [
            {
                "operation_id": "recover-signage",
                "before_path": "before-signage-repair.tif",
                "before_sha256": "before-hash",
                "after_path": "signage-repair.tif",
                "after_sha256": "after-hash",
                "model": "generative-backend",
                "model_version": "2026.08",
                "software": "Adobe Photoshop 2026",
                "prompt": "Repair the clipped sign edge while preserving its structure.",
                "mask_reference": "masks/signage.png",
                "mask_sha256": "mask-hash",
            }
        ],
        "variant_name": "competition-standard",
    }


class ScoreRecordContractTests(unittest.TestCase):
    def test_complete_urban_night_score_record_passes(self) -> None:
        self.assertEqual(validate_score_record(urban_night_score_record()), [])

    def test_score_outside_zero_to_one_hundred_fails(self) -> None:
        record = urban_night_score_record()
        record["technical"] = 101
        self.assertTrue(any("technical" in error for error in validate_score_record(record)))

    def test_low_confidence_record_is_review_only(self) -> None:
        record = urban_night_score_record()
        record["score_confidence"] = 0.74
        record["decision"] = "selected"
        self.assertTrue(any("review" in error for error in validate_score_record(record)))

    def test_failed_technical_gate_cannot_be_auto_accepted(self) -> None:
        record = urban_night_score_record()
        record["decision"] = "selected"
        record["keep_value"] = 80
        record["editability"] = 85
        record["candidate_potential"] = 75
        record["expected_gain"] = 40
        record["technical_gates"]["source_readable"] = "fail"
        self.assertTrue(any("technical gate" in error for error in validate_score_record(record)))

    def test_candidate_potential_must_match_the_weighted_formula(self) -> None:
        record = urban_night_score_record()
        record["decision"] = "selected"
        record["candidate_potential"] = 100
        self.assertTrue(any("formula" in error for error in validate_score_record(record)))

    def test_below_threshold_candidate_cannot_be_auto_selected(self) -> None:
        record = urban_night_score_record()
        record["decision"] = "selected"
        self.assertTrue(any("threshold" in error for error in validate_score_record(record)))

    def test_whitespace_only_score_version_fails(self) -> None:
        record = urban_night_score_record()
        record["score_version"] = " \t "
        self.assertTrue(any("score_version" in error for error in validate_score_record(record)))

    def test_animal_wildlife_is_a_supported_primary_category(self) -> None:
        record = urban_night_score_record()
        record["primary_category"] = "animal-wildlife"
        self.assertEqual(validate_score_record(record), [])


class EditPlanContractTests(unittest.TestCase):
    def test_complete_edit_plan_passes(self) -> None:
        self.assertEqual(validate_edit_plan(complete_edit_plan()), [])

    def test_director_brief_requires_creative_controls(self) -> None:
        for field in ("creative_intensity", "source_fidelity"):
            with self.subTest(field=field):
                plan = complete_edit_plan()
                del plan["director_brief"][field]
                self.assertTrue(any(field in error for error in validate_edit_plan(plan)))

    def test_edit_plan_requires_content_policy(self) -> None:
        plan = complete_edit_plan()
        del plan["content_policy"]
        self.assertTrue(any("content_policy" in error for error in validate_edit_plan(plan)))

    def test_generative_operation_requires_operation_record(self) -> None:
        plan = complete_edit_plan()
        plan["operation_records"] = []
        self.assertTrue(any("operation record" in error for error in validate_edit_plan(plan)))

    def test_operation_graph_rejects_broken_dependency(self) -> None:
        plan = complete_edit_plan()
        plan["operations"][0]["depends_on"] = ["missing-operation"]
        self.assertTrue(any("dependency" in error for error in validate_edit_plan(plan)))

    def test_operation_graph_rejects_self_dependency(self) -> None:
        plan = complete_edit_plan()
        plan["operations"][0]["depends_on"] = ["recover-signage"]
        self.assertTrue(any("self-dependency" in error for error in validate_edit_plan(plan)))

    def test_operation_graph_rejects_dependency_cycle(self) -> None:
        plan = complete_edit_plan()
        second_operation = dict(plan["operations"][0])
        second_operation["operation_id"] = "relight-building"
        second_operation["type"] = "relight-subject"
        second_operation["depends_on"] = ["recover-signage"]
        plan["operations"].append(second_operation)
        plan["operations"][0]["depends_on"] = ["relight-building"]
        self.assertTrue(any("cycle" in error for error in validate_edit_plan(plan)))

    def test_region_requires_confidence(self) -> None:
        plan = complete_edit_plan()
        del plan["regions"][0]["confidence"]
        self.assertTrue(any("confidence" in error for error in validate_edit_plan(plan)))

    def test_edit_plan_requires_adjustment_budget(self) -> None:
        plan = complete_edit_plan()
        del plan["adjustment_budget"]
        self.assertTrue(any("adjustment_budget" in error for error in validate_edit_plan(plan)))

    def test_full_authority_requires_user_authorized_transformative_policy(self) -> None:
        plan = complete_edit_plan()
        plan["content_policy"] = "source-faithful"
        self.assertTrue(any("user-authorized-transformative" in error for error in validate_edit_plan(plan)))

    def test_transformative_operation_requires_provenance_record(self) -> None:
        plan = complete_edit_plan()
        plan["operations"][0]["generative"] = False
        plan["operation_records"] = []
        self.assertTrue(any("operation record" in error for error in validate_edit_plan(plan)))

    def test_operation_records_must_not_duplicate_declared_operations(self) -> None:
        plan = complete_edit_plan()
        plan["operation_records"].append(dict(plan["operation_records"][0]))
        self.assertTrue(any("duplicate operation record" in error for error in validate_edit_plan(plan)))

    def test_operation_records_must_not_be_orphaned(self) -> None:
        plan = complete_edit_plan()
        plan["operation_records"][0]["operation_id"] = "undeclared-operation"
        self.assertTrue(any("orphaned operation record" in error for error in validate_edit_plan(plan)))

    def test_whitespace_only_director_brief_string_fails(self) -> None:
        plan = complete_edit_plan()
        plan["director_brief"]["project_goal"] = "   "
        self.assertTrue(any("project_goal" in error for error in validate_edit_plan(plan)))


class JsonSchemaAlignmentTests(unittest.TestCase):
    def test_required_string_schemas_require_non_whitespace_content(self) -> None:
        score_schema = json.loads((REFERENCES_DIR / "score-record.schema.json").read_text())
        brief_schema = json.loads((REFERENCES_DIR / "director-brief.schema.json").read_text())
        plan_schema = json.loads((REFERENCES_DIR / "edit-plan.schema.json").read_text())

        self.assertEqual(score_schema["properties"]["score_version"]["pattern"], "\\S")
        self.assertEqual(brief_schema["properties"]["project_goal"]["pattern"], "\\S")
        self.assertEqual(plan_schema["definitions"]["operation"]["properties"]["reason"]["pattern"], "\\S")
        self.assertEqual(plan_schema["definitions"]["operation_record"]["properties"]["prompt"]["pattern"], "\\S")


if __name__ == "__main__":
    unittest.main()
