"""Regression tests for final-review trust and release boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_photoshop_adapter as photoshop_adapter  # noqa: E402
import quality_gate  # noqa: E402
import run_manifest  # noqa: E402
from asset_groups import group_related_assets  # noqa: E402
from schema_validator import validate_edit_plan  # noqa: E402
from validate_export import validate_export  # noqa: E402


def trusted_context() -> dict:
    return {
        "source_path": "/work/source.ARW",
        "source_sha256": "source-hash",
        "source_assets": [{"path": "/work/source.ARW", "sha256": "source-hash"}],
        "source_snapshot_sha256": "snapshot-hash",
        "source_snapshot_immutable": True,
        "intent": "competition-standard",
        "edit_authority": "full",
        "intent_budget": {
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
        "locality_policy": "local-only",
    }


def report(**changes: object) -> dict:
    value = {
        "candidate_id": "candidate-1",
        "final_score": 90,
        "score_confidence": 0.95,
        "technical_gates": {"render": "pass", "source": "pass"},
        "warnings": [],
        "source_path": "/work/source.ARW",
        "source_sha256": "source-hash",
        "source_snapshot_sha256": "snapshot-hash",
        "before_path": "/work/source.ARW",
        "after_path": "/work/candidate.tif",
        "processing_locality": "local-only",
        "transformation_level": "global",
    }
    value.update(changes)
    return value


def trusted_evaluation(**changes: object) -> dict:
    value = {
        "candidate_id": "candidate-1",
        "final_score": 90,
        "score_confidence": 0.95,
        "technical_gates": {"render": "pass", "source": "pass"},
    }
    value.update(changes)
    return value


def complete_transform_plan(root: Path) -> dict:
    before = root / "before.tif"
    after = root / "after.tif"
    mask = root / "mask.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    mask.write_bytes(b"mask")
    return {
        "director_brief": {
            "project_goal": "Produce a competition-ready result.",
            "subject_priority": ["subject"],
            "target_use": "competition-quality",
            "mood": "controlled",
            "photographer_intent": "Preserve the central subject.",
            "creative_intensity": 70,
            "source_fidelity": 80,
            "transformation_disclosure": "Generative repair disclosed.",
            "allowed_operations": ["generative-fill"],
        },
        "intent": "competition-standard",
        "edit_authority": "full",
        "content_policy": "user-authorized-transformative",
        "adjustment_budget": copy.deepcopy(trusted_context()["intent_budget"]),
        "global_adjustments": [{"exposure": 0.5, "temperature_delta": 300}],
        "regions": [],
        "operations": [{
            "operation_id": "repair-1",
            "type": "generative-fill",
            "depends_on": [],
            "backend": "photoshop",
            "reason": "Repair selected damage.",
            "affected_region": "subject",
            "parameters": {"crop_fraction": 0.1},
            "risk": "medium",
            "checkpoint": "before-repair",
            "generative": True,
            "input_layer": "base",
            "output_layer": "repair",
        }],
        "operation_records": [{
            "operation_id": "repair-1",
            "before_path": str(before),
            "before_sha256": hashlib.sha256(before.read_bytes()).hexdigest(),
            "after_path": str(after),
            "after_sha256": hashlib.sha256(after.read_bytes()).hexdigest(),
            "model": "local-model",
            "model_version": "1.0",
            "software": "Adobe Photoshop 2026",
            "prompt": "Repair only the selected damage.",
            "mask_reference": str(mask),
            "mask_sha256": hashlib.sha256(mask.read_bytes()).hexdigest(),
        }],
        "variant_name": "competition-standard",
    }


class QualityGateRegressionTests(unittest.TestCase):
    def test_candidate_visual_scores_cannot_override_trusted_gate_failure(self) -> None:
        candidate = report(
            technical_gates={"render": "fail"},
            visual_scores={
                "technical_gates": {"render": "pass"},
                "final_score": 99,
                "score_confidence": 1.0,
            },
        )

        result = quality_gate.evaluate_candidate(
            trusted_context(), candidate, trusted_evaluation(technical_gates={"render": "fail"})
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("technical_gate_failed", result["failure_reasons"])

    def test_empty_technical_gates_are_rejected(self) -> None:
        result = quality_gate.evaluate_candidate(
            trusted_context(), report(technical_gates={}), trusted_evaluation(technical_gates={})
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("invalid_technical_gates", result["failure_reasons"])

    def test_missing_confidence_is_rejected_instead_of_defaulting_to_one(self) -> None:
        evaluation = trusted_evaluation()
        evaluation.pop("score_confidence")

        result = quality_gate.evaluate_candidate(trusted_context(), report(), evaluation)

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("invalid_score_confidence", result["failure_reasons"])

    def test_non_finite_scores_and_confidence_are_rejected(self) -> None:
        for field, value in (("final_score", math.inf), ("final_score", math.nan), ("score_confidence", math.inf), ("score_confidence", math.nan)):
            with self.subTest(field=field, value=value):
                result = quality_gate.evaluate_candidate(
                    trusted_context(), report(), trusted_evaluation(**{field: value})
                )
                self.assertEqual(result["decision"], "rejected")
                self.assertIn(f"invalid_{field}", result["failure_reasons"])

    def test_competition_intent_rejects_render_below_competition_threshold(self) -> None:
        context = trusted_context()
        payload = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        context["trust_root_digest"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        result = quality_gate.evaluate_candidate(
            context,
            report(),
            trusted_evaluation(
                final_score=76.02,
                technical_score=82.63,
                aesthetic_score=82.18,
                improvement_score=50.58,
            ),
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("competition_final_score_below_85", result["failure_reasons"])

    def test_multi_source_manifest_rejects_path_outside_manifest(self) -> None:
        context = trusted_context()
        context["source_path"] = None
        context["source_sha256"] = None
        context["source_assets"] = [
            {"path": "/work/one.ARW", "sha256": "one-hash"},
            {"path": "/work/two.ARW", "sha256": "two-hash"},
        ]
        outside = report(source_path="/outside/not-in-manifest.ARW", source_sha256="outside-hash")

        result = quality_gate.evaluate_candidate(context, outside, trusted_evaluation())

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("source_not_in_manifest", result["failure_reasons"])

    def test_generative_candidate_without_operation_graph_is_rejected(self) -> None:
        result = quality_gate.evaluate_candidate(
            trusted_context(),
            report(transformation_level="generative", generative_used=True, edit_plan=None),
            trusted_evaluation(),
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("missing_edit_plan", result["failure_reasons"])

    def test_transform_provenance_requires_hashes_model_prompt_mask_and_software(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = complete_transform_plan(Path(directory))
            plan["operation_records"][0].pop("after_sha256")
            result = quality_gate.evaluate_candidate(
                trusted_context(), report(transformation_level="major", edit_plan=plan), trusted_evaluation()
            )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("missing_transformation_provenance", result["failure_reasons"])

    def test_intent_budget_rejects_extreme_adjustment_magnitude(self) -> None:
        plan = {
            "global_adjustments": [{"exposure": 1000}],
            "regions": [],
            "operations": [],
            "operation_records": [],
        }

        result = quality_gate.evaluate_candidate(
            trusted_context(), report(edit_plan=plan), trusted_evaluation()
        )

        self.assertEqual(result["decision"], "rejected")
        self.assertIn("adjustment_magnitude:exposure", result["failure_reasons"])

    def test_release_decision_cannot_label_global_only_as_competition_standard(self) -> None:
        decide_release = getattr(quality_gate, "decide_release", None)
        self.assertTrue(callable(decide_release), "release decision API is required")

        decision = decide_release(
            report(decision="selected", final_score=95, technical_gates={"render": "pass"}),
            {
                "requested_label": "competition-standard",
                "adapter_status": {"available": False, "mode": "global-only", "fine_edit_mode": False},
                "export_validation": {"valid": True, "profile": "competition-quality", "release_blockers": []},
                "editable_master": {"valid": True, "editable": True, "layered": True, "layer_ids": ["base"], "mask_ids": ["subject"]},
                "transformation_disclosure": "All transformations disclosed.",
            },
        )

        self.assertEqual(decision["final_label"], "global-only")
        self.assertIn("photoshop_fine_edit_unavailable", decision["release_blockers"])


class ManifestAndGroupingRegressionTests(unittest.TestCase):
    def test_manifest_rejects_unsupported_intent_and_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_manifest.create_run_manifest("run", directory, "invented-intent", "full")
            with self.assertRaises(ValueError):
                run_manifest.create_run_manifest("run", directory, "natural-enhancement", "admin")

    def test_sealed_manifest_digest_detects_trusted_context_mutation(self) -> None:
        seal = getattr(run_manifest, "seal_run_manifest", None)
        verify = getattr(run_manifest, "verify_run_manifest", None)
        self.assertTrue(callable(seal), "manifest sealing API is required")
        self.assertTrue(callable(verify), "manifest verification API is required")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ARW"
            source.write_bytes(b"source")
            manifest = seal(run_manifest.create_run_manifest("run", directory, "natural-enhancement", "full"))
            self.assertTrue(verify(manifest))
            manifest["trusted_context"]["intent_budget"]["max_exposure_delta"] = 999
            self.assertFalse(verify(manifest))

    def test_source_snapshot_excludes_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            root = Path(directory)
            outside = Path(outside_directory) / "private.ARW"
            outside.write_bytes(b"outside")
            (root / "inside.ARW").write_bytes(b"inside")
            try:
                os.symlink(outside, root / "escape.ARW")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")

            manifest = run_manifest.create_run_manifest("run", directory, "natural-enhancement", "full")

        self.assertEqual([Path(asset["path"]).name for asset in manifest["source_assets"]], ["inside.ARW"])

    def test_asset_group_id_is_stable_and_hash_sensitive(self) -> None:
        paths = ["/shoot/DSC_0001.ARW", "/shoot/DSC_0001.xmp"]
        hashes = {paths[0]: "raw-hash", paths[1]: "xmp-hash"}

        first = group_related_assets(paths, hashes)[0]
        second = group_related_assets(list(reversed(paths)), hashes)[0]
        changed = group_related_assets(paths, {**hashes, paths[1]: "changed"})[0]

        self.assertRegex(first["asset_group_id"], r"^ag-[0-9a-f]{24}$")
        self.assertEqual(first["asset_group_id"], second["asset_group_id"])
        self.assertNotEqual(first["asset_group_id"], changed["asset_group_id"])


class EditPlanSchemaRegressionTests(unittest.TestCase):
    def test_global_lightroom_adjustments_are_schema_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = complete_transform_plan(Path(directory))

            errors = validate_edit_plan(plan)

        self.assertEqual(errors, [])

    def test_non_finite_and_out_of_budget_adjustments_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = complete_transform_plan(Path(directory))
            plan["global_adjustments"] = [{"exposure": math.nan}]
            errors = validate_edit_plan(plan)

        self.assertTrue(any("finite" in error for error in errors))


class ExportReleaseRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "source.ARW"
        self.source.write_bytes(b"source")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def policy(self, export: Path, **changes: object) -> dict:
        checksum = hashlib.sha256(export.read_bytes()).hexdigest()
        policy = {
            "source_checksum": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "expected_source_path": str(self.source),
            "result_snapshot_path": str(export),
            "result_checksum": checksum,
            "result_snapshot_sha256": checksum,
            "expected_icc_profile": "sRGB",
            "required_fields": [],
        }
        policy.update(changes)
        return policy

    def test_wrong_icc_required_metadata_disclosure_and_semantic_artifacts_block_release(self) -> None:
        export = self.root / "export.jpg"
        Image.new("RGB", (3200, 2400), "navy").save(export, "JPEG", quality=92)
        result = validate_export(
            str(export), str(self.source), "competition-quality",
            self.policy(
                export,
                required_fields=["make"],
                required_xmp_markers=["transformation:disclosed"],
                semantic_artifacts={"faces": True},
            ),
        )

        self.assertFalse(result["valid"])
        self.assertTrue({
            "wrong_color_profile",
            "missing_metadata:make",
            "missing_xmp_marker:transformation:disclosed",
            "semantic_artifact:faces",
        }.issubset(result["release_blockers"]))

    def test_print_master_requires_16_bit_editable_layered_masked_evidence(self) -> None:
        export = self.root / "master.tif"
        Image.new("RGB", (4000, 3000), "navy").save(export, "TIFF")
        checksum = hashlib.sha256(export.read_bytes()).hexdigest()
        result = validate_export(
            str(export), str(self.source), "print-master",
            self.policy(export, editable_master={
                "path": str(export),
                "sha256": checksum,
                "editable": False,
                "layered": False,
                "layer_ids": [],
                "mask_ids": [],
            }),
        )

        self.assertFalse(result["valid"])
        self.assertIn("print_master_bit_depth", result["errors"])
        self.assertIn("print_master_not_editable", result["errors"])
        self.assertIn("print_master_not_layered", result["errors"])
        self.assertIn("print_master_missing_masks", result["errors"])


def operation_payload(root: Path) -> dict:
    files = {}
    for name, content in (
        ("before.tif", b"before"),
        ("after.tif", b"after"),
        ("mask.png", b"mask"),
        ("candidate.jpg", b"jpeg"),
    ):
        path = root / name
        path.write_bytes(content)
        files[name] = path
    entry = {
        "operation_id": "op-1",
        "depends_on": [],
        "type": "relight-subject",
        "region": "subject",
        "mask_reference": str(files["mask.png"]),
        "mask_sha256": hashlib.sha256(files["mask.png"].read_bytes()).hexdigest(),
        "parameters": {"exposure": 0.2},
        "risk": "low",
        "input_layer_id": "layer-in",
        "output_layer_id": "layer-out",
        "before_path": str(files["before.tif"]),
        "before_sha256": hashlib.sha256(files["before.tif"].read_bytes()).hexdigest(),
        "after_path": str(files["after.tif"]),
        "after_sha256": hashlib.sha256(files["after.tif"].read_bytes()).hexdigest(),
        "generative": False,
    }
    manifest_hash = hashlib.sha256(
        json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "requested_operation_id": "op-1",
        "operation_id": "op-1",
        "status": "completed",
        "idempotent_replay": False,
        "execution": {
            "backend": "dcc-mcp-photoshop",
            "software": "Adobe Photoshop",
            "software_version": "27.9.1",
            "bridge_version": "0.2.0",
            "locality": "local",
        },
        "document_evidence": {
            "input_document_id": "doc-1",
            "output_document_id": "doc-1",
            "input_layer_id": "layer-in",
            "output_layer_id": "layer-out",
            "editable": True,
            "layered": True,
            "non_destructive": True,
            "flattened": False,
        },
        "export_evidence": {
            "status": "completed",
            "path": str(files["candidate.jpg"]),
            "sha256": hashlib.sha256(files["candidate.jpg"].read_bytes()).hexdigest(),
            "format": "JPEG",
            "source_master_sha256": entry["after_sha256"],
        },
        "mask_validation": {
            "status": "valid",
            "dimensions_match": True,
            "edges_checked": True,
            "artifact_warnings": [],
        },
        "generative": {
            "used": False,
            "reported": True,
            "controlled": True,
            "backend_healthy": True,
            "backend_locality": "local",
            "model": None,
            "model_version": None,
            "prompt": None,
        },
        "operation_manifest": {
            "status": "valid",
            "operation_id": "op-1",
            "manifest_hash": manifest_hash,
            "entry": entry,
        },
        "warnings": [],
    }


class PhotoshopEvidenceRegressionTests(unittest.TestCase):
    def test_local_file_hashes_are_recomputed_and_fake_hashes_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = operation_payload(Path(directory))
            payload["operation_manifest"]["entry"]["mask_sha256"] = "fake-mask"
            payload["operation_manifest"]["entry"]["before_sha256"] = "fake-before"
            payload["operation_manifest"]["entry"]["after_sha256"] = "fake-after"
            payload["export_evidence"]["sha256"] = "fake-export"
            payload["operation_manifest"]["manifest_hash"] = "fake-manifest"

            result = photoshop_adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertTrue({
            "manifest_hash_mismatch",
            "mask_sha256_mismatch",
            "before_sha256_mismatch",
            "after_sha256_mismatch",
            "export_sha256_mismatch",
        }.issubset(result["errors"]))

    def test_missing_or_nonlocal_evidence_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = operation_payload(Path(directory))
            payload["operation_manifest"]["entry"]["mask_reference"] = "https://example.invalid/mask.png"
            result = photoshop_adapter.normalize_operation_result(payload)

        self.assertFalse(result["valid"])
        self.assertIn("mask_path_not_local", result["errors"])


if __name__ == "__main__":
    unittest.main()
