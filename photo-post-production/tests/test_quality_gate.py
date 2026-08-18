"""Contracts for bounded candidate comparison and immutable run manifests."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from quality_gate import choose_best_candidate as _choose_best_candidate  # noqa: E402
from quality_gate import evaluate_candidate as _evaluate_candidate  # noqa: E402
from run_manifest import create_run_manifest as _create_run_manifest, seal_run_manifest  # noqa: E402


def valid_plan() -> dict:
    return {
        "intent": "competition-standard",
        "adjustment_budget": {
            "max_global_adjustments": 2,
            "max_local_adjustments": 2,
            "max_transformative_operations": 1,
            "max_exposure_delta": 2.0,
            "max_crop_fraction": 0.35,
            "max_temperature_delta": 3000,
            "max_sharpening": 120,
            "max_geometry_delta": 15,
            "max_candidates": 4,
        },
        "global_adjustments": [{"exposure": 0.2}],
        "regions": [{"id": "tower", "adjustments": {"texture": 8}}],
        "operations": [{
            "operation_id": "repair-sign",
            "type": "generative-fill",
            "depends_on": [],
            "generative": True,
        }],
        "operation_records": [{
            "operation_id": "repair-sign",
            "before_path": "checkpoint.tif",
            "before_sha256": "before-hash",
            "after_path": "candidate.tif",
            "after_sha256": "after-hash",
            "model": "local-model",
            "model_version": "1.0",
            "prompt": "Repair only the clipped sign edge.",
            "mask_reference": "masks/sign.png",
            "mask_sha256": "mask-hash",
            "software": "editor 1.0",
        }],
    }


def candidate(candidate_id: str, score: float, **changes: object) -> dict:
    report = {
        "candidate_id": candidate_id,
        "iteration": 1,
        "final_score": score,
        "score_confidence": 0.95,
        "technical_gates": {"render": "pass", "source": "pass"},
        "warnings": [],
        "source_path": "/work/source.RAW",
        "source_sha256": "source-hash",
        "source_snapshot_sha256": "snapshot-hash",
        "before_path": "/work/source.RAW",
        "after_path": f"/work/{candidate_id}.tif",
        "processing_locality": "local-only",
        "locality_policy": "local-only",
        "edit_plan": valid_plan(),
        "brief_satisfied": False,
        "score_delta": 1.0,
    }
    report.update(changes)
    return report


def selection_context() -> dict:
    context = {
        "source_path": "/work/source.RAW",
        "source_sha256": "source-hash",
        "source_snapshot_sha256": "snapshot-hash",
        "source_assets": [{"path": "/work/source.RAW", "sha256": "source-hash"}],
        "source_snapshot_immutable": True,
        "intent_budget": valid_plan()["adjustment_budget"],
        "locality_policy": "local-only",
    }
    payload = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    context["trust_root_digest"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return context


def _sealed_context(**changes: object) -> dict:
    context = selection_context()
    context.pop("trust_root_digest")
    budget_changes = changes.pop("intent_budget", None)
    if isinstance(budget_changes, dict):
        context["intent_budget"].update(budget_changes)
    source_path = changes.pop("source_path", context["source_path"])
    source_hash = changes.pop("expected_source_sha256", changes.pop("source_sha256", context["source_sha256"]))
    snapshot_hash = changes.pop("expected_source_snapshot_sha256", changes.pop("source_snapshot_sha256", context["source_snapshot_sha256"]))
    context.update(changes)
    context["source_path"] = source_path
    context["source_sha256"] = source_hash
    context["source_snapshot_sha256"] = snapshot_hash
    context["source_assets"] = [{"path": source_path, "sha256": source_hash}]
    payload = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    context["trust_root_digest"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return context


def evaluate_candidate(before: dict, after: dict, trusted_scores: dict) -> dict:
    context = _sealed_context(**(before if isinstance(before, dict) else {}))
    evaluation = {
        "candidate_id": after.get("candidate_id"),
        "final_score": after.get("final_score"),
        "score_confidence": after.get("score_confidence"),
        "technical_gates": after.get("technical_gates"),
    }
    evaluation.update(trusted_scores if isinstance(trusted_scores, dict) else {})
    return _evaluate_candidate(context, after, evaluation)


def choose_best_candidate(reports: list[dict]) -> dict:
    evaluations = [
        {
            "candidate_id": report.get("candidate_id"),
            "final_score": report.get("final_score"),
            "score_confidence": report.get("score_confidence"),
            "technical_gates": report.get("technical_gates"),
        }
        for report in reports if isinstance(report, dict)
    ]
    return _choose_best_candidate(reports, evaluations)


def create_run_manifest(run_id: str, input_dir: str, intent: str, authority: str) -> dict:
    return seal_run_manifest(_create_run_manifest(run_id, input_dir, intent, authority))


def with_trusted_context(reports: list[dict]) -> list[dict]:
    reports = copy.deepcopy(reports)
    reports[0]["trusted_context"] = selection_context()
    return reports


class RunManifestTests(unittest.TestCase):
    def test_manifest_captures_immutable_source_identity_and_run_contract(self) -> None:
        manifest = create_run_manifest("run-7", "/work/input", "natural-enhancement", "full")

        self.assertEqual(manifest["run_id"], "run-7")
        self.assertEqual(manifest["intent"], "natural-enhancement")
        self.assertEqual(manifest["edit_authority"], "full")
        self.assertEqual(manifest["processing_locality"], "local-only")
        self.assertTrue(manifest["source_snapshot_immutable"])
        self.assertEqual(manifest["source_assets"], [])
        self.assertEqual(manifest["asset_group_ids"], [])
        self.assertIn("director_brief", manifest)
        self.assertIn("creative_controls", manifest)
        self.assertIn("resource_budget", manifest)
        self.assertIn("edit_graph", manifest)
        self.assertIn("adapter_status", manifest)
        self.assertIn("iteration_records", manifest)
        self.assertIn("checkpoints", manifest)
        self.assertIn("operation_ids", manifest)
        self.assertIn("provenance", manifest)
        self.assertIn("score_deltas", manifest)
        self.assertIn("transformation_level", manifest)


class CandidateComparisonTests(unittest.TestCase):
    def test_higher_scoring_candidate_rolls_back_to_best_checkpoint(self) -> None:
        chosen = choose_best_candidate(with_trusted_context([
            candidate("first", 81, iteration=1),
            candidate("better", 87, iteration=2),
            candidate("regression", 80, iteration=3),
        ]))

        self.assertEqual(chosen["candidate_id"], "better")
        self.assertEqual(chosen["stopping_reason"], "max_iterations")
        self.assertEqual(chosen["rollback_to"], "/work/better.tif")
        self.assertEqual(chosen["rejected_candidates"], ["regression"])

    def test_candidate_selection_is_bounded_to_three_iterations(self) -> None:
        chosen = choose_best_candidate(with_trusted_context([candidate(f"candidate-{index}", 80 + index, iteration=index) for index in range(1, 5)]))

        self.assertEqual(chosen["candidate_id"], "candidate-3")
        self.assertEqual(chosen["evaluated_iterations"], 3)
        self.assertEqual(chosen["stopping_reason"], "max_iterations")

    def test_sealed_candidate_budget_can_lower_iteration_limit(self) -> None:
        reports = [candidate("first", 80), candidate("second", 90, iteration=2)]
        reports[0]["trusted_context"] = _sealed_context(intent_budget={"max_candidates": 1})

        chosen = choose_best_candidate(reports)

        self.assertEqual(chosen["candidate_id"], "first")
        self.assertEqual(chosen["evaluated_iterations"], 1)
        self.assertEqual(chosen["stopping_reason"], "max_iterations")

    def test_quality_saturation_stops_iteration_early(self) -> None:
        chosen = choose_best_candidate(with_trusted_context([
            candidate("one", 80, iteration=1),
            candidate("two", 80.1, iteration=2, score_delta=0.1),
        ]))

        self.assertEqual(chosen["candidate_id"], "two")
        self.assertEqual(chosen["stopping_reason"], "quality_saturation")

    def test_director_brief_satisfaction_stops_iteration_early(self) -> None:
        chosen = choose_best_candidate(with_trusted_context([
            candidate("one", 80, iteration=1),
            candidate("brief-met", 82, iteration=2, brief_satisfied=True),
        ]))

        self.assertEqual(chosen["candidate_id"], "brief-met")
        self.assertEqual(chosen["stopping_reason"], "director_brief_satisfied")

    def test_lower_scoring_candidate_is_rejected(self) -> None:
        chosen = choose_best_candidate(with_trusted_context([candidate("winner", 88), candidate("lower", 87, iteration=2)]))

        self.assertEqual(chosen["candidate_id"], "winner")
        self.assertIn("lower", chosen["rejected_candidates"])

    def test_selector_rejects_candidate_below_release_threshold_when_metrics_are_trusted(self) -> None:
        report = candidate(
            "low-quality",
            92,
            technical_score=60.0,
            aesthetic_score=60.0,
            improvement_score=50.0,
        )
        evaluations = [{
            "candidate_id": "low-quality",
            "final_score": 92,
            "score_confidence": 0.95,
            "technical_gates": {"render": "pass", "source": "pass"},
            "technical_score": 60.0,
            "aesthetic_score": 60.0,
            "improvement_score": 50.0,
        }]

        chosen = _choose_best_candidate(with_trusted_context([report]), evaluations)

        self.assertEqual(chosen["decision"], "rejected")
        self.assertIsNone(chosen["candidate_id"])
        self.assertIn("low-quality", chosen["rejected_candidates"])

    def test_selector_infers_competition_request_from_verified_candidate_plan(self) -> None:
        chosen = _choose_best_candidate(
            with_trusted_context([candidate("competition", 90)]),
            [{
                "candidate_id": "competition",
                "final_score": 90,
                "score_confidence": 0.95,
                "technical_gates": {"render": "pass", "source": "pass"},
            }],
        )

        self.assertEqual(chosen["decision"], "selected")
        self.assertEqual(chosen["final_label"], "global-only")
        self.assertNotIn("competition_standard_not_requested", chosen["release_blockers"])
        self.assertIn("photoshop_fine_edit_unavailable", chosen["release_blockers"])

    def test_selector_releases_verified_fine_edit_as_competition_standard(self) -> None:
        report = candidate(
            "verified-competition",
            90,
            adapter_status={"available": True, "fine_edit_mode": True, "mode": "fine-edit"},
            editable_master={
                "valid": True,
                "path": "/work/verified-competition.psd",
                "format": "PSD",
                "editable": True,
                "layered": True,
                "layer_ids": ["base", "retouch"],
                "mask_ids": ["subject-mask"],
            },
            export_validation={
                "valid": True,
                "path": "/work/verified-competition.jpg",
                "profile": "competition-quality",
                "release_blockers": [],
            },
            transformation_disclosure="记录局部精修和构图裁切。",
        )
        chosen = _choose_best_candidate(
            with_trusted_context([report]),
            [{
                "candidate_id": "verified-competition",
                "final_score": 90,
                "score_confidence": 0.95,
                "technical_gates": {"render": "pass", "source": "pass"},
            }],
        )

        self.assertEqual(chosen["decision"], "selected")
        self.assertEqual(chosen["final_label"], "competition-standard")
        self.assertEqual(chosen["release_blockers"], [])

    def test_unedited_candidate_needs_no_operation_manifest(self) -> None:
        chosen = choose_best_candidate(with_trusted_context([{
            "candidate_id": "baseline",
            "final_score": 80,
            "score_confidence": 0.9,
            "technical_gates": {"render": "pass"},
            "source_path": "/work/source.RAW",
            "source_sha256": "source-hash",
            "source_snapshot_sha256": "snapshot-hash",
            "after_path": "/work/baseline.tif",
            "processing_locality": "local-only",
        }]))

        self.assertEqual(chosen["candidate_id"], "baseline")
        self.assertEqual(chosen["decision"], "selected")

    def test_selection_without_trusted_context_is_not_auto_selected(self) -> None:
        chosen = choose_best_candidate([candidate("untrusted", 99)])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertEqual(chosen["stopping_reason"], "missing_trusted_context")

    def test_public_selection_uses_generated_manifest_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-public", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            report = candidate(
                "foreign-hash", 99, source_path=asset["path"], source_sha256="foreign",
                source_snapshot_sha256=manifest["source_snapshot_sha256"], run_manifest=manifest,
            )

            chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertIn("foreign-hash", chosen["rejected_candidates"])

    def test_public_selection_carries_generated_manifest_intent_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-budget", directory, "natural-enhancement", "full")
            manifest["intent_budget"]["max_transformative_operations"] = 0
            manifest = seal_run_manifest(manifest)
            asset = manifest["source_assets"][0]
            report = candidate(
                "forbidden-operation", 99, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], intent_budget={"max_transformative_operations": 9},
                run_manifest=manifest,
            )

            chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertIn("forbidden-operation", chosen["rejected_candidates"])

    def test_public_selection_carries_generated_manifest_locality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-locality", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            report = candidate(
                "remote-escape", 99, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], processing_locality="remote",
                locality_policy="remote", run_manifest=manifest,
            )

            chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertIn("remote-escape", chosen["rejected_candidates"])

    def test_public_selection_allows_baseline_with_generated_manifest_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-baseline", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            report = candidate(
                "baseline-public", 80, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], edit_plan=None, run_manifest=manifest,
            )

            chosen = choose_best_candidate([report])

        self.assertEqual(chosen["candidate_id"], "baseline-public")
        self.assertEqual(chosen["decision"], "selected")

    def test_manifest_context_conflict_is_rejected_before_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-conflict", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            report = candidate(
                "conflicting-context", 99, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], run_manifest=manifest,
                trusted_context={"source_path": asset["path"], "source_sha256": "foreign-hash"},
            )

            chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertEqual(chosen["stopping_reason"], "trust_context_conflict")

    def test_manifest_asset_context_conflict_is_rejected_before_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-asset-conflict", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            report = candidate(
                "conflicting-assets", 99, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], run_manifest=manifest,
                trusted_context={"source_assets": [{"path": asset["path"], "sha256": "foreign-hash"}]},
            )

            chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertEqual(chosen["stopping_reason"], "trust_context_conflict")

    def test_later_manifest_context_conflict_cannot_replace_valid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-later-conflict", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            valid = candidate(
                "valid-first", 80, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], edit_plan=None,
                run_manifest=manifest,
            )
            conflicting = candidate(
                "conflicting-later", 99, iteration=2, source_path=asset["path"],
                source_sha256=asset["sha256"], source_snapshot_sha256=manifest["source_snapshot_sha256"],
                edit_plan=None, trusted_context={"source_sha256": "foreign-hash"},
            )

            chosen = choose_best_candidate([valid, conflicting])

        self.assertEqual(chosen["candidate_id"], "valid-first")
        self.assertEqual(chosen["decision"], "selected")
        self.assertIn("conflicting-later", chosen["rejected_candidates"])

    def test_later_empty_trusted_context_cannot_replace_valid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-later-empty", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            valid = candidate(
                "valid-first", 80, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], edit_plan=None,
                run_manifest=manifest,
            )
            empty = candidate(
                "empty-later", 99, iteration=2, source_path=asset["path"],
                source_sha256=asset["sha256"], source_snapshot_sha256=manifest["source_snapshot_sha256"],
                edit_plan=None, trusted_context={},
            )

            chosen = choose_best_candidate([valid, empty])

        self.assertEqual(chosen["candidate_id"], "valid-first")
        self.assertEqual(chosen["decision"], "selected")
        self.assertIn("empty-later", chosen["rejected_candidates"])

    def test_later_partial_trusted_context_cannot_replace_valid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.RAW"
            source.write_bytes(b"trusted source")
            manifest = create_run_manifest("run-later-partial", directory, "natural-enhancement", "full")
            asset = manifest["source_assets"][0]
            valid = candidate(
                "valid-first", 80, source_path=asset["path"], source_sha256=asset["sha256"],
                source_snapshot_sha256=manifest["source_snapshot_sha256"], edit_plan=None,
                run_manifest=manifest,
            )
            partial = candidate(
                "partial-later", 99, iteration=2, source_path=asset["path"],
                source_sha256=asset["sha256"], source_snapshot_sha256=manifest["source_snapshot_sha256"],
                edit_plan=None,
                trusted_context={"source_path": asset["path"], "source_sha256": asset["sha256"]},
            )

            chosen = choose_best_candidate([valid, partial])

        self.assertEqual(chosen["candidate_id"], "valid-first")
        self.assertEqual(chosen["decision"], "selected")
        self.assertIn("partial-later", chosen["rejected_candidates"])

    def test_empty_trusted_context_is_rejected_before_selection(self) -> None:
        report = candidate("empty-context", 99, trusted_context={})

        chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertEqual(chosen["stopping_reason"], "invalid_trusted_context")

    def test_incomplete_trusted_context_is_rejected_before_selection(self) -> None:
        report = candidate("incomplete-context", 99, trusted_context={
            "source_path": "/work/source.RAW",
            "source_sha256": "source-hash",
        })

        chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertEqual(chosen["stopping_reason"], "invalid_trusted_context")

    def test_snapshot_only_context_requires_immutable_marker(self) -> None:
        context = selection_context()
        del context["source_path"]
        del context["source_sha256"]
        report = candidate("mutable-snapshot", 99, trusted_context=context)

        chosen = choose_best_candidate([report])

        self.assertEqual(chosen["decision"], "rejected")
        self.assertEqual(chosen["stopping_reason"], "invalid_trusted_context")

    def test_low_confidence_candidate_is_routed_to_review(self) -> None:
        result = evaluate_candidate({}, candidate("uncertain", 92), {"score_confidence": 0.74})

        self.assertEqual(result["decision"], "review")
        self.assertIn("low_confidence", result["warnings"])

    def test_technical_gate_failure_precedes_high_score(self) -> None:
        rejected = candidate("broken", 99, technical_gates={"render": "fail"})
        result = evaluate_candidate({}, rejected, {})

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["rejection_reason"], "technical_gate_failed")

    def test_critical_warning_prevents_selection(self) -> None:
        result = evaluate_candidate({}, candidate("critical", 99, warnings=["critical: haloing"]), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["rejection_reason"], "critical_warning")

    def test_adjustment_budget_violation_is_rejected(self) -> None:
        plan = valid_plan()
        plan["global_adjustments"] = [{"exposure": 0.2}, {"contrast": 4}, {"highlights": -5}]
        result = evaluate_candidate(
            {"intent_budget": valid_plan()["adjustment_budget"]},
            candidate("over-budget", 90, edit_plan=plan),
            {},
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("adjustment_budget", result["warnings"])

    def test_broken_edit_graph_dependency_is_rejected(self) -> None:
        plan = valid_plan()
        plan["operations"][0]["depends_on"] = ["missing-operation"]
        result = evaluate_candidate({}, candidate("broken-graph", 90, edit_plan=plan), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("broken_operation_dependency", result["warnings"])

    def test_locality_policy_violation_is_rejected(self) -> None:
        result = evaluate_candidate({}, candidate("remote", 90, processing_locality="remote", locality_policy="local-only"), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["rejection_reason"], "locality_policy_violation")

    def test_missing_transformation_provenance_is_rejected(self) -> None:
        plan = valid_plan()
        plan["operation_records"] = []
        result = evaluate_candidate({}, candidate("unproven", 90, edit_plan=plan), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("missing_transformation_provenance", result["warnings"])

    def test_root_edit_graph_and_selected_intent_budget_are_enforced(self) -> None:
        plan = valid_plan()
        result = evaluate_candidate(
            {"intent_budget": {"max_transformative_operations": 0}},
            candidate("root-graph", 90, edit_plan=None, edit_graph=plan),
            {},
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("adjustment_budget", result["warnings"])

    def test_candidate_cannot_raise_the_run_intent_budget(self) -> None:
        plan = valid_plan()
        result = evaluate_candidate(
            {"intent_budget": {"max_transformative_operations": 0}},
            candidate("budget-escape", 90, edit_plan=plan, intent_budget={"max_transformative_operations": 5}),
            {},
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("adjustment_budget", result["warnings"])

    def test_source_identity_requires_canonical_path_hash_and_snapshot_hash(self) -> None:
        result = evaluate_candidate(
            {
                "source_path": "/work/source.RAW",
                "expected_source_sha256": "expected-source",
                "expected_source_snapshot_sha256": "expected-snapshot",
            },
            candidate(
                "hash-mismatch", 90,
                source_path="/work/../work/source.RAW",
                source_sha256="changed-source",
                source_snapshot_sha256="changed-snapshot",
            ),
            {},
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("source_sha256_mismatch", result["warnings"])
        self.assertIn("source_snapshot_sha256_mismatch", result["warnings"])

    def test_candidate_cannot_relax_run_locality_policy(self) -> None:
        result = evaluate_candidate(
            {"locality_policy": "local-only"},
            candidate("locality-escape", 90, processing_locality="remote", locality_policy="remote"),
            {},
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["rejection_reason"], "locality_policy_violation")

    def test_technical_and_critical_failures_are_not_masked(self) -> None:
        result = evaluate_candidate(
            {"source_path": "/work/source.RAW", "locality_policy": "local-only"},
            candidate(
                "many-failures", 99, source_path="/other/source.RAW", processing_locality="remote",
                technical_gates={"render": "fail"}, warnings=["critical: artifacts"],
            ),
            {},
        )

        self.assertEqual(result["rejection_reason"], "technical_gate_failed")
        self.assertFalse(result["technical_gate_passed"])
        self.assertIn("critical_warning", result["warnings"])
        self.assertEqual(result["failure_reasons"][:2], ["technical_gate_failed", "critical_warning"])
        self.assertIn("source_path_mismatch", result["failure_reasons"])
        self.assertIn("locality_policy_violation", result["failure_reasons"])

    def test_blank_operation_id_is_rejected_before_graph_construction(self) -> None:
        plan = valid_plan()
        plan["operations"][0]["operation_id"] = "  "
        result = evaluate_candidate({}, candidate("blank-operation", 90, edit_plan=plan), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("blank_operation_id", result["warnings"])

    def test_duplicate_operation_id_is_rejected_before_graph_construction(self) -> None:
        plan = valid_plan()
        plan["operations"].append(dict(plan["operations"][0]))
        result = evaluate_candidate({}, candidate("duplicate-operation", 90, edit_plan=plan), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("duplicate_operation_id", result["warnings"])

    def test_candidate_cannot_change_source_path(self) -> None:
        result = evaluate_candidate({"source_path": "/work/source.RAW"}, candidate("mismatch", 90, source_path="/other/source.RAW"), {})

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("source_path_mismatch", result["warnings"])


if __name__ == "__main__":
    unittest.main()
