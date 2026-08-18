import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


from action_descriptor_registry import (  # noqa: E402
    find_compatible_descriptor,
    get_descriptor,
    init_registry,
    register_descriptor,
)
from adapter_plans import build_photoshop_plan  # noqa: E402
from category_weighting import get_category_profile  # noqa: E402
from closed_loop import evaluate_iteration, select_best_iteration  # noqa: E402
from comparative_ranker import rank_group  # noqa: E402
from edit_planner import build_problem_driven_plan  # noqa: E402
from execution_engine import _document_identity_matches, _job_disk_budget_decision  # noqa: E402
from executor_router import route_operation  # noqa: E402
from model_signals import normalize_model_signals  # noqa: E402
from operation_graph import build_operation_graph, validate_operation_graph  # noqa: E402
from pipeline_enrichment import route_problem_plan  # noqa: E402
from preference_model import (  # noqa: E402
    apply_preference_model,
    init_preference_store,
    train_preference_model,
)
from resource_guard import check_disk_budget, estimate_peak_disk_bytes  # noqa: E402
from schema_validator import validate_score_record  # noqa: E402
from visual_analysis import analyze_visual_record  # noqa: E402


class ResearchSupplementTests(unittest.TestCase):
    def test_plant_macro_is_classified_and_schema_valid(self):
        record = {
            "photo_id": "flower-1",
            "source_path": "/tmp/flower.ARW",
            "technical_analysis": {
                "highlight_clipping": 0.01,
                "shadow_crush": 0.02,
                "blur_proxy": 0.98,
                "noise_proxy": 0.08,
                "mean_luma": 0.52,
            },
        }
        vision = {
            "available": True,
            "backend": "fixture",
            "classifications": [
                {"identifier": "flower", "confidence": 0.93},
                {"identifier": "macro photography", "confidence": 0.88},
            ],
            "faces": [],
            "animals": [],
            "text": [],
        }

        result = analyze_visual_record(record, vision=vision)

        self.assertEqual(result["primary_category"], "plant-macro")
        self.assertEqual(validate_score_record(result["score_record"]), [])
        self.assertIn("焦平面", "；".join(result["recommended_treatment"]))

    def test_category_profile_is_versioned_and_normalized(self):
        profile = get_category_profile("plant-macro")

        self.assertRegex(profile["version"], r"^category-weights-")
        self.assertAlmostEqual(sum(profile["weights"].values()), 1.0)
        self.assertGreater(profile["weights"]["technical"], 0)

    def test_model_signals_are_clamped_and_keep_provenance(self):
        result = normalize_model_signals(
            {
                "aesthetic": {"score": 123, "confidence": 0.8, "model": "topiq", "version": "1"},
                "saliency": {"score": -4, "confidence": 2, "model": "birefnet", "version": "2"},
            }
        )

        self.assertEqual(result["signals"]["aesthetic"]["score"], 100.0)
        self.assertEqual(result["signals"]["saliency"]["score"], 0.0)
        self.assertEqual(result["signals"]["saliency"]["confidence"], 1.0)
        self.assertIn("topiq:1", result["evidence"])

    def test_comparative_ranker_explains_why_burst_winner_wins(self):
        ranked = rank_group(
            [
                {"photo_id": "a", "technical": 82, "composition": 70, "moment_story": 65, "candidate_potential": 75},
                {"photo_id": "b", "technical": 74, "composition": 88, "moment_story": 84, "candidate_potential": 80},
            ],
            category="street-documentary",
        )

        self.assertEqual(ranked[0]["photo_id"], "b")
        self.assertEqual(ranked[0]["burst_rank"], 1)
        self.assertTrue(ranked[0]["comparative_reasons"])
        self.assertIn("构图", "；".join(ranked[0]["comparative_reasons"]))

    def test_preference_model_trains_only_with_enough_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = str(Path(temporary) / "preferences.sqlite3")
            init_preference_store(db_path)
            insufficient = train_preference_model(
                db_path,
                "default",
                "portrait-environmental",
                [{"better": {"brightness": 0.8}, "worse": {"brightness": 0.2}, "weight": 1}],
                min_samples=4,
            )
            self.assertEqual(insufficient["status"], "insufficient-evidence")

            comparisons = [
                {"better": {"brightness": 0.9, "crop_tightness": 0.4}, "worse": {"brightness": 0.2, "crop_tightness": 0.8}, "weight": 1},
                {"better": {"brightness": 0.8, "crop_tightness": 0.3}, "worse": {"brightness": 0.3, "crop_tightness": 0.9}, "weight": 1},
                {"better": {"brightness": 0.7, "crop_tightness": 0.5}, "worse": {"brightness": 0.1, "crop_tightness": 0.7}, "weight": 1},
                {"better": {"brightness": 0.95, "crop_tightness": 0.35}, "worse": {"brightness": 0.4, "crop_tightness": 0.85}, "weight": 1},
            ]
            trained = train_preference_model(
                db_path,
                "default",
                "portrait-environmental",
                comparisons,
                min_samples=4,
            )
            applied = apply_preference_model(
                db_path,
                "default",
                "portrait-environmental",
                {"brightness": 0.85, "crop_tightness": 0.35},
            )

            self.assertEqual(trained["status"], "active")
            self.assertGreaterEqual(trained["validation_accuracy"], 0.75)
            self.assertGreater(applied["preference_fit"], 50)
            self.assertEqual(applied["model_version"], trained["version"])

    def test_descriptor_registry_requires_json_and_routes_advanced_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = str(Path(temporary) / "descriptors.sqlite3")
            init_registry(db_path)
            registered = register_descriptor(
                db_path,
                name="portrait-liquify-v1",
                operation_type="liquify-bounded",
                descriptor=[{"_obj": "liquify", "strength": 0.12}],
                photoshop_version="27.9",
                document_modes=["RGB"],
                bit_depths=[16],
                parameter_schema={"strength": {"minimum": 0, "maximum": 0.2}},
            )
            found = find_compatible_descriptor(
                db_path,
                operation_type="liquify-bounded",
                photoshop_version="27.9.1",
                document_mode="RGB",
                bit_depth=16,
            )
            route = route_operation(
                {"type": "liquify-bounded", "parameters": {"strength": 0.1}},
                capabilities={"stable_tools": [], "ui_available": True},
                descriptor=found,
            )

            self.assertEqual(found["descriptor_id"], registered["descriptor_id"])
            self.assertEqual(get_descriptor(db_path, registered["descriptor_id"])["descriptor"], [{"_obj": "liquify", "strength": 0.12}])
            self.assertEqual(route["tier"], "descriptor-verified")
            self.assertEqual(route["backend"], "photoshop-batchplay")
            with self.assertRaises(ValueError):
                register_descriptor(
                    db_path,
                    name="bad",
                    operation_type="liquify-bounded",
                    descriptor="app.activeDocument.close()",
                    photoshop_version="27.9",
                    document_modes=["RGB"],
                    bit_depths=[16],
                    parameter_schema={},
                )

    def test_problem_plan_route_promotes_verified_operation_into_graph(self):
        with tempfile.TemporaryDirectory() as temporary:
            descriptor_db = str(Path(temporary) / "descriptors.sqlite3")
            score = {
                "photo_id": "plant-1",
                "primary_category": "plant-macro",
                "problem_driven_plan": {
                    "operations": [{
                        "type": "selective-sharpen",
                        "reason": "焦平面需要加强",
                        "success_criteria": "焦点清楚且背景不增噪",
                        "affected_region": "focus-plane",
                        "risk": "medium",
                    }],
                },
            }
            score["problem_driven_plan"] = route_problem_plan(
                score,
                {"photoshop": {"stable_tools": ["selective-sharpen"], "ui_available": False}, "generative": {}},
                descriptor_db,
                photoshop_version="27.9",
            )
            photoshop_plan = build_photoshop_plan(score)
            sharpening = next(item for item in photoshop_plan["operations"] if item["tool"] == "sharpening")
            graph = build_operation_graph(
                score,
                {"variant_name": "natural"},
                {"photoshop": photoshop_plan},
                {},
            )

            self.assertTrue(sharpening["required"])
            self.assertEqual(sharpening["execution_route"]["tier"], "stable-auto")
            self.assertEqual(validate_operation_graph(graph), [])
            self.assertTrue(any(item.get("adapter_operation") == "sharpening" for item in graph["operations"]))

    def test_resource_guard_estimates_peak_before_starting(self):
        estimate = estimate_peak_disk_bytes(
            source_bytes=50_000_000,
            width=8000,
            height=6000,
            bit_depth=16,
            photoshop_layers=12,
            keep_working_tiff=True,
        )
        decision = check_disk_budget(current_bytes=900_000_000, projected_peak_bytes=estimate, budget_bytes=1_000_000_000)

        self.assertGreater(estimate, 100_000_000)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "projected_disk_budget_exceeded")

    def test_execution_guard_checks_projected_job_footprint_and_document_identity(self):
        job = {
            "source_path": "/tmp/nonexistent.ARW",
            "source_bytes": 50_000_000,
            "display_dimensions": [8000, 6000],
            "photoshop_layers": 12,
        }

        decision = _job_disk_budget_decision(job, current_bytes=900_000_000, budget_bytes=1_000_000_000)

        self.assertFalse(decision["allowed"])
        self.assertTrue(_document_identity_matches(None, "doc-1"))
        self.assertTrue(_document_identity_matches("doc-1", "doc-1"))
        self.assertFalse(_document_identity_matches("doc-1", "doc-2"))

    def test_problem_driven_plan_is_bounded_and_not_every_tool(self):
        plan = build_problem_driven_plan(
            {
                "photo_id": "portrait-1",
                "primary_category": "portrait-environmental",
                "risks": ["暗部压黑风险"],
                "edit_outlook": {"largest_problem": "人物面部偏暗，背景过强"},
                "visual_evidence": {"faces": 1},
            },
            max_operations=3,
        )

        self.assertLessEqual(len(plan["operations"]), 3)
        self.assertIn("subject-relight", [item["type"] for item in plan["operations"]])
        self.assertNotIn("liquify-bounded", [item["type"] for item in plan["operations"]])
        self.assertTrue(all(item["success_criteria"] for item in plan["operations"]))

    def test_closed_loop_accepts_improvement_and_stops_after_two_noops(self):
        accepted = evaluate_iteration(
            previous={"iteration": 1, "score": 72, "technical_pass": True, "semantic_pass": True},
            candidate={"iteration": 2, "score": 78, "technical_pass": True, "semantic_pass": True, "material_change": True},
            no_op_streak=0,
        )
        first_noop = evaluate_iteration(
            previous=accepted["best"],
            candidate={"iteration": 3, "score": 78.1, "technical_pass": True, "semantic_pass": True, "material_change": False},
            no_op_streak=0,
        )
        second_noop = evaluate_iteration(
            previous=first_noop["best"],
            candidate={"iteration": 4, "score": 78.1, "technical_pass": True, "semantic_pass": True, "material_change": False},
            no_op_streak=first_noop["no_op_streak"],
        )

        self.assertTrue(accepted["accepted"])
        self.assertFalse(first_noop["accepted"])
        self.assertTrue(second_noop["stop"])
        self.assertEqual(second_noop["stopping_reason"], "consecutive_no_effect_operations")
        best = select_best_iteration([accepted["best"], first_noop["candidate"], second_noop["candidate"]])
        self.assertEqual(best["iteration"], 2)


if __name__ == "__main__":
    unittest.main()
