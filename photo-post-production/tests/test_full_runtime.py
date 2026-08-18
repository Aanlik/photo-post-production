"""Integration contracts for the complete local orchestration boundary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from adapter_plans import _crop_context_guard, build_lightroom_plan, build_photoshop_plan  # noqa: E402
from color_style import build_color_style  # noqa: E402
from calibration import build_project_calibration  # noqa: E402
from edit_plans import attach_execution_audit, build_edit_plan, build_variant_specs, materialize_executable_plan  # noqa: E402
from execution_engine import _disk_budget_bytes, _materialize_selected_outputs, _operation_request, _quality_for_completed_job, _reconcile_quality_release_labels, _sync_execution_plan, _sync_top_level_report, _verified_document_identity_rollover, build_execution_jobs, execute_batch, execute_job  # noqa: E402
from apply_review_decisions import _approved_scores  # noqa: E402
import lightroom_mcp_adapter  # noqa: E402
import photoshop_mcp_adapter  # noqa: E402
from operation_graph import build_operation_graph, validate_operation_graph  # noqa: E402
from run_manifest import create_run_manifest, seal_run_manifest  # noqa: E402
from schema_validator import validate_edit_plan  # noqa: E402
from series_consistency import build_series_plan  # noqa: E402


def score(photo_id: str, category: str = "portrait-environmental") -> dict:
    return {
        "photo_id": photo_id,
        "asset_group_id": f"group-{photo_id}",
        "source_path": f"/tmp/{photo_id}.ARW",
        "preview_path": f"/tmp/{photo_id}.jpg",
        "primary_category": category,
        "candidate_potential": 88.0,
        "keep_value": 90.0,
        "editability": 85.0,
        "expected_gain": 80.0,
        "score_confidence": 0.92,
        "classification_confidence": 0.92,
        "technical": 88.0,
        "photographic_value": 86.0,
        "technical_gates": {"source_readable": "pass", "irrecoverable_defect": "pass", "preview_quality": "pass"},
        "recommended_treatment": ["人物脸部曝光和肤色精修"],
        "decision": "selected",
        "mean_luma": 0.42,
    }


class FullRuntimeTests(unittest.TestCase):
    def test_lightroom_write_readback_requires_requested_values(self) -> None:
        metadata = {"developSettings": {"Exposure2012": 0.5, "Saturation": -4}}
        evidence = lightroom_mcp_adapter._verify_settings_readback(
            {"Exposure2012": 0.5, "Saturation": -4}, metadata
        )
        self.assertEqual(evidence["status"], "verified")
        quantized = lightroom_mcp_adapter._verify_settings_readback(
            {"Sharpness": 45.8}, {"developSettings": {"Sharpness": 46}}
        )
        self.assertEqual(quantized["actual"]["Sharpness"], 46)
        self.assertIn("Sharpness", quantized["quantized_readback"])
        with self.assertRaises(lightroom_mcp_adapter.AdapterError):
            lightroom_mcp_adapter._verify_settings_readback(
                {"Exposure2012": 0.7}, {"developSettings": {"Exposure2012": 0.0}}
            )
        with self.assertRaises(lightroom_mcp_adapter.AdapterError):
            lightroom_mcp_adapter._verify_settings_readback(
                {"Sharpness": 45.8}, {"developSettings": {"Sharpness": 47}}
            )

    def test_lightroom_handoff_name_is_unique_per_run(self) -> None:
        first = lightroom_mcp_adapter._working_export_name({
            "run_id": "run-a",
            "photo_id": "photo-1",
            "variant_name": "competition-standard",
        }, ".tif")
        second = lightroom_mcp_adapter._working_export_name({
            "run_id": "run-b",
            "photo_id": "photo-1",
            "variant_name": "competition-standard",
        }, ".tif")

        self.assertNotEqual(first, second)
        self.assertEqual(first, "run-a--photo-1--competition-standard.tif")

    def test_optional_structured_operations_are_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            Image.new("RGB", (32, 24), "#456").save(source)
            graph = {
                "operations": [
                    {
                        "operation_id": "lr-base",
                        "type": "relight-subject",
                        "depends_on": [],
                        "backend": "lightroom-mcp",
                        "reason": "global",
                        "affected_region": "global-frame",
                        "parameters": {},
                        "risk": "low",
                        "checkpoint": "before-lr",
                        "generative": False,
                        "input_layer": "raw",
                        "output_layer": "render",
                        "required": True,
                    },
                    {
                        "operation_id": "ps-optional-tone",
                        "type": "relight-subject",
                        "depends_on": ["lr-base"],
                        "backend": "photoshop-fine-edit",
                        "adapter_operation": "selective_color",
                        "reason": "tone",
                        "affected_region": "global-frame",
                        "parameters": {"saturation": -10.0},
                        "risk": "medium",
                        "checkpoint": "before-tone",
                        "generative": False,
                        "input_layer": "render",
                        "output_layer": "tone",
                        "required": False,
                    },
                ],
                "max_iterations": 1,
            }
            job = {
                "item_id": "run:p:natural",
                "run_id": "run",
                "photo_id": "p",
                "variant_name": "natural",
                "source_path": str(source),
                "output_dir": str(root),
                "processing_locality": "mixed",
                "operation_graph": graph,
                "score": {"source_path": str(source), "candidate_potential": 80},
                "adapter_plan": {},
            }
            calls: list[str] = []

            def fake_adapter(_command: str, request: dict, timeout: int = 600) -> dict:
                del timeout
                operation_id = request["operation"]["operation_id"]
                calls.append(operation_id)
                return {
                    "status": "completed",
                    "operation_id": operation_id,
                    "evidence": {
                        "after_path": str(source),
                        "document_id": "doc-1",
                    },
                }

            with patch("execution_engine._command_for_backend", return_value=("fake-adapter", "local")), patch(
                "execution_engine._call_json_adapter", side_effect=fake_adapter
            ):
                result = execute_job(job, str(root / "queue.sqlite3"), "run")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["operation_graph"]["status"], "completed")
            self.assertEqual(calls, ["lr-base", "ps-optional-tone"])

    def test_fine_edit_mode_requires_all_photoshop_nodes_to_have_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            Image.new("RGB", (32, 24), "#456").save(source)
            manifest = seal_run_manifest(create_run_manifest("run-coverage", str(root), "natural-enhancement", "full"))
            graph = {
                "operations": [
                    {
                        "operation_id": "ps-done",
                        "backend": "photoshop-fine-edit",
                        "status": "completed",
                        "required": True,
                    },
                    {
                        "operation_id": "ps-skipped",
                        "backend": "photoshop-fine-edit",
                        "status": "skipped",
                        "required": False,
                    },
                ]
            }
            execution = {
                "job_id": "p:natural",
                "operation_graph": graph,
                "operation_results": [
                    {"operation_id": "ps-done", "status": "completed", "evidence": {"after_path": str(source)}},
                    {"operation_id": "ps-skipped", "status": "skipped", "reason": "verified_action_descriptor_required"},
                ],
            }
            job = {
                "photo_id": "p",
                "variant_name": "natural",
                "source_path": str(source),
                "score": {"source_path": str(source), "score_confidence": 0.9},
                "processing_locality": "mixed",
                "edit_plan": {},
            }

            quality = _quality_for_completed_job(job, execution, manifest)

            self.assertFalse(quality["adapter_status"]["fine_edit_mode"])
            self.assertEqual(quality["operation_coverage"]["photoshop"]["coverage"], 0.5)

    def test_lightroom_import_response_resolves_original_path(self) -> None:
        response = {"imported": [{"id": "catalog-42", "path": "/Users/test/DSC00042.ARW"}]}
        self.assertEqual(
            lightroom_mcp_adapter._catalog_photo_id_from_import(response, "/Users/test/DSC00042.ARW"),
            "catalog-42",
        )

    def test_oriented_face_geometry_produces_true_four_by_five_crop(self) -> None:
        portrait = score("oriented-face")
        portrait["visual_evidence"] = {
            "faces": 1,
            "face_boxes": [{"x": 0.38, "y": 0.51, "width": 0.06, "height": 0.10}],
            "face_boxes_display": [{"x": 0.51, "y": 0.38, "width": 0.10, "height": 0.06}],
            "display_dimensions": [1200, 1800],
            "box_coordinate_space": "display-top-left-normalized",
            "exif_orientation": 6,
        }

        plan = build_photoshop_plan(portrait, variant="competition-standard")
        crop = next(item for item in plan["operations"] if item["tool"] == "apply_crop")["parameters"]["crop_bounds"]
        crop_width = crop["right"] - crop["left"]
        crop_height = crop["bottom"] - crop["top"]
        displayed_aspect = crop_width * 1200 / (crop_height * 1800)
        face_center_x = 0.56
        face_center_y = 0.41

        self.assertAlmostEqual(displayed_aspect, 0.8, places=3)
        self.assertAlmostEqual((face_center_x - crop["left"]) / crop_width, 0.5, places=2)
        self.assertAlmostEqual((face_center_y - crop["top"]) / crop_height, 0.2, places=2)

    def test_environmental_portrait_allows_headroom_only_crop_without_safe_secondary_boxes(self) -> None:
        portrait = score("crowded-portrait")
        portrait["secondary_tags"] = ["people", "crowd"]
        portrait["visual_evidence"] = {
            "faces": 1,
            "labels": [["people", 0.8], ["crowd", 0.4]],
            "face_boxes_display": [{"x": 0.51, "y": 0.38, "width": 0.10, "height": 0.06}],
            "display_dimensions": [1200, 1800],
            "box_coordinate_space": "display-top-left-normalized",
        }

        plan = build_photoshop_plan(portrait, variant="competition-standard")
        crop = next(item for item in plan["operations"] if item["tool"] == "apply_crop")

        self.assertTrue(crop["required"])
        self.assertFalse(plan["crop_guard"]["preserve_context"])
        self.assertEqual(plan["crop_guard"]["policy"], "automatic-aesthetic-crop-headroom-only")
        bounds = crop["parameters"]["crop_bounds"]
        self.assertLessEqual(bounds["right"], 0.90)

    def test_environmental_portrait_still_blocks_crop_that_wastes_context_or_cuts_body(self) -> None:
        portrait = score("unsafe-context-crop")
        portrait["secondary_tags"] = ["people", "crowd"]
        guard = _crop_context_guard(
            portrait,
            {
                "crop_bounds": {
                    "left": 0.04,
                    "top": 0.05,
                    "right": 0.92,
                    "bottom": 0.74,
                }
            },
        )

        self.assertTrue(guard["preserve_context"])
        self.assertEqual(guard["policy"], "preserve-or-review")

    def test_competition_night_architecture_plans_decisive_finish(self) -> None:
        architecture = score("night-architecture", "architecture-urban-space")
        architecture["secondary_tags"] = ["minimal", "night"]
        architecture["visual_evidence"] = {
            "labels": [["sky", 0.83], ["cloudy", 0.78], ["building", 0.76]],
            "display_dimensions": [1800, 1200],
        }

        plan = build_photoshop_plan(architecture, variant="competition-standard")
        operations = plan["operations"]
        by_name = {
            item.get("parameters", {}).get("output_layer_name"): item
            for item in operations
            if isinstance(item.get("parameters"), dict)
        }

        self.assertIn("03 · 夜空去脏灰", by_name)
        self.assertIn("02C · 全局夜景色彩基底", by_name)
        self.assertIn("04 · 建筑主体塑形", by_name)
        self.assertIn("04B · 建筑冷暖分离", by_name)
        self.assertIn("05 · 前景蓝紫抑制", by_name)
        self.assertTrue(all(by_name[name]["required"] for name in (
            "03 · 夜空去脏灰", "04 · 建筑主体塑形", "04B · 建筑冷暖分离", "05 · 前景蓝紫抑制",
        )))
        noise = next(item for item in operations if item["tool"] == "noise_reduction")
        sharpen = next(item for item in operations if item["tool"] == "sharpening")
        self.assertTrue(noise["required"])
        self.assertTrue(noise["applicable"])
        self.assertFalse(noise.get("requires_descriptor", False))
        self.assertTrue(sharpen["required"])
        self.assertTrue(sharpen["applicable"])
        self.assertFalse(sharpen.get("requires_descriptor", False))
        crop = next(item for item in operations if item["tool"] == "apply_crop")
        self.assertFalse(crop["required"])
        self.assertFalse(crop["applicable"])
        self.assertTrue(plan["crop_guard"]["preserve_context"])
        self.assertEqual(plan["crop_guard"]["policy"], "preserve-context-unless-geometry-evidence-requires-crop")
        required_operations = [
            item for item in operations
            if item.get("required") is True and item.get("applicable") is not False
        ]
        self.assertEqual(required_operations[-1]["tool"], "selective_color")

    def test_competition_architecture_color_rejects_mixed_light_pollution(self) -> None:
        architecture = score("architecture-color", "architecture-urban-space")
        architecture["secondary_tags"] = ["minimal", "night"]
        architecture["visual_evidence"] = {
            "labels": [["sky", 0.83], ["cloudy", 0.78], ["building", 0.76]],
            "display_dimensions": [1800, 1200],
        }

        color = build_color_style(architecture, None, "competition-standard")

        self.assertEqual(color["hsl"]["purple"]["saturation"], -30.0)
        self.assertEqual(color["hsl"]["magenta"]["saturation"], -32.0)
        self.assertLessEqual(color["hsl"]["blue"]["saturation"], -18.0)
        self.assertGreater(color["hsl"]["red"]["saturation"], 0.0)

        plan = build_lightroom_plan(architecture, variant="competition-standard")
        settings = plan["settings"]
        self.assertGreaterEqual(settings["exposure"], 0.25)
        self.assertLessEqual(settings["exposure"], 0.80)
        self.assertLessEqual(settings["highlights"], -40.0)
        self.assertLessEqual(settings["whites"], -1.0)
        self.assertGreaterEqual(settings["contrast"], 5.0)
        self.assertGreaterEqual(settings["dehaze"], 2.0)
        self.assertEqual(plan["color_strategy"]["scene_mode"], "night-architecture")

    def test_environmental_portrait_can_crop_when_secondary_subject_is_kept_whole(self) -> None:
        portrait = score("safe-crowded-portrait")
        portrait["secondary_tags"] = ["people", "crowd"]
        portrait["visual_evidence"] = {
            "faces": 1,
            "labels": [["people", 0.8], ["crowd", 0.4]],
            "face_boxes_display": [{"x": 0.51, "y": 0.38, "width": 0.10, "height": 0.06}],
            "secondary_subject_boxes_display": [
                {"x": 0.76, "y": 0.42, "width": 0.08, "height": 0.12}
            ],
            "display_dimensions": [1200, 1800],
            "box_coordinate_space": "display-top-left-normalized",
        }

        plan = build_photoshop_plan(portrait, variant="competition-standard")
        crop = next(item for item in plan["operations"] if item["tool"] == "apply_crop")

        self.assertTrue(crop["required"])
        self.assertFalse(plan["crop_guard"]["preserve_context"])
        self.assertEqual(plan["crop_guard"]["policy"], "automatic-aesthetic-crop")

    def test_competition_lightroom_plan_contains_material_global_adjustments(self) -> None:
        settings = build_lightroom_plan(score("global-tone"), variant="competition-standard")["settings"]

        self.assertGreaterEqual(settings["exposure"], 0.28)
        self.assertLessEqual(settings["highlights"], -26.0)
        self.assertGreaterEqual(settings["shadows"], 18.0)
        self.assertGreaterEqual(settings["vibrance"], 14.0)
        self.assertGreaterEqual(settings["clarity"], 4.0)

    def test_category_color_styles_are_distinct_and_auditable(self) -> None:
        categories = [
            "portrait-environmental",
            "landscape-nature",
            "street-documentary",
            "architecture-urban-space",
            "animal-wildlife",
            "plant-macro",
        ]

        styles = {
            category: build_color_style(score(category, category), variant="competition-standard")
            for category in categories
        }

        self.assertEqual(len({json.dumps(item["hsl"], sort_keys=True) for item in styles.values()}), len(categories))
        for category, style in styles.items():
            self.assertEqual(style["strategy"]["category"], category)
            self.assertTrue(style["strategy"]["rationale"])
            self.assertIn("unsupported_color_features", style["strategy"])

    def test_portrait_color_style_protects_skin_tones(self) -> None:
        style = build_color_style(
            score("portrait-color", "portrait-environmental"),
            variant="competition-standard",
        )

        self.assertTrue(style["strategy"]["skin_tone_protection"])
        self.assertGreater(style["hsl"]["orange"]["luminance"], 0)
        self.assertGreaterEqual(style["hsl"]["orange"]["saturation"], -6)
        self.assertLessEqual(style["hsl"]["orange"]["saturation"], 4)
        self.assertGreaterEqual(style["hsl"]["red"]["saturation"], -8)

    def test_style_recipe_hsl_and_measured_white_balance_are_bounded(self) -> None:
        item = score("learned-color", "landscape-nature")
        item["white_balance"] = {"temperature": 6125, "tint": 14}
        recipe = {
            "recipe": {
                "lightroom": {
                    "temperature_bias": 180,
                    "tint_bias": -3,
                    "hsl": {"blue": {"saturation": 50, "luminance": -6}},
                }
            }
        }

        style = build_color_style(item, recipe, "competition-standard")

        self.assertEqual(style["white_balance"], "Custom")
        self.assertEqual(style["temperature"], 6305)
        self.assertEqual(style["tint"], 11)
        self.assertLessEqual(style["hsl"]["blue"]["saturation"], 40)
        self.assertEqual(style["strategy"]["white_balance_basis"], "measured-source-plus-style-bias")

    def test_lightroom_plan_includes_category_color_settings(self) -> None:
        plan = build_lightroom_plan(
            score("plant-color", "plant-macro"),
            variant="competition-standard",
        )

        self.assertIn("hsl", plan["settings"])
        self.assertLess(plan["settings"]["hsl"]["green"]["hue"], 0)
        self.assertEqual(plan["color_strategy"]["category"], "plant-macro")

    def test_environmental_portrait_adds_visible_subject_background_separation_layers(self) -> None:
        portrait = score("subject-separation")
        portrait["secondary_tags"] = ["people", "crowd"]
        portrait["visual_evidence"] = {
            "faces": 1,
            "face_boxes_display": [{"x": 0.51, "y": 0.38, "width": 0.10, "height": 0.06}],
            "display_dimensions": [1200, 1800],
            "box_coordinate_space": "display-top-left-normalized",
        }

        operations = build_photoshop_plan(portrait, variant="competition-standard")["operations"]
        background = next(item for item in operations if item.get("parameters", {}).get("output_layer_name") == "03 · 环境色彩克制")
        subject = next(item for item in operations if item.get("parameters", {}).get("output_layer_name") == "04 · 主体明暗分离")

        self.assertTrue(background["required"])
        self.assertEqual(background["tool"], "selective_color")
        self.assertEqual(background["parameters"]["mask_kind"], "all")
        self.assertLess(background["parameters"]["saturation"], 0)
        self.assertTrue(subject["required"])
        self.assertEqual(subject["tool"], "region_mask_operation")
        self.assertEqual(subject["parameters"]["mask_kind"], "rectangle")
        self.assertGreater(subject["parameters"]["brightness"], 0)
        self.assertGreater(subject["parameters"]["contrast"], 0)
        crowd_restraint = next(item for item in operations if item.get("parameters", {}).get("output_layer_name") == "03A · 邻近人群降噪")
        face_fill = next(item for item in operations if item.get("parameters", {}).get("output_layer_name") == "06 · 帽檐下自然补光")
        self.assertTrue(crowd_restraint["required"])
        self.assertEqual(crowd_restraint["parameters"]["mask_kind"], "rectangle")
        self.assertLess(crowd_restraint["parameters"]["saturation"], 0)
        self.assertTrue(face_fill["required"])
        self.assertEqual(face_fill["parameters"]["mask_kind"], "ellipse")
        self.assertGreater(face_fill["parameters"]["brightness"], 0)

    def test_review_keep_list_is_the_only_execution_approval(self) -> None:
        scores = [
            {"photo_id": "auto-selected", "decision": "selected"},
            {"photo_id": "explicit-keep", "decision": "selected", "human_decision": "keep"},
            {"photo_id": "explicit-reject", "decision": "rejected", "human_decision": "reject"},
        ]
        self.assertEqual([item["photo_id"] for item in _approved_scores(scores)], ["explicit-keep"])

    def test_selected_outputs_reference_canonical_files_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = root / "preview.jpg"
            web = root / "web.jpg"
            competition = root / "competition.jpg"
            master = root / "master.psd"
            for path in (preview, web, competition, master):
                path.write_bytes(path.name.encode("utf-8"))
            winner = {
                "photo_id": "p-selected",
                "variant_name": "editorial",
                "after_path": str(preview),
                "export_validations": {
                    "web-share": {"valid": True, "path": str(web)},
                    "competition-quality": {"valid": True, "path": str(competition)},
                },
                "editable_master": {"path": str(master), "editable": True, "layered": True},
            }
            _materialize_selected_outputs(root, "p-selected", winner, "editorial")
            self.assertFalse((root / "selected").exists())
            self.assertEqual(winner["selected_output_paths"]["web-share"], str(web))
            self.assertEqual(winner["selected_output_paths"]["competition-quality"], str(competition))
            self.assertEqual(winner["selected_output_paths"]["editable-master"], str(master))
            self.assertEqual(winner["selected_output_path"], str(competition))

    def test_quality_report_sync_releases_legacy_verified_psd_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ARW"
            master = root / "master.psd"
            source.write_bytes(b"raw-source")
            master.write_bytes(b"layered-psd")
            manifest = seal_run_manifest(create_run_manifest("legacy-psd", str(root), "competition-standard", "full"))
            asset = manifest["source_assets"][0]
            item = {
                "photo_id": "p-legacy",
                "candidate_id": "p-legacy:competition-standard",
                "iteration": 1,
                "source_path": asset["path"],
                "source_sha256": asset["sha256"],
                "source_snapshot_sha256": manifest["source_snapshot_sha256"],
                "processing_locality": manifest["processing_locality"],
                "locality_policy": manifest["locality_policy"],
                "run_manifest": manifest,
                "requested_label": "competition-standard",
                "warnings": [],
                "final_score": 90.0,
                "score_confidence": 0.95,
                "technical_gates": {"rendered_pixels": "pass"},
                "technical_score": 95.0,
                "aesthetic_score": 90.0,
                "improvement_score": 20.0,
                "critical_artifacts": False,
                "quality_status": "evaluated",
                "decision": "selected",
                "editable_master": {
                    "path": str(master),
                    "format": "PSD",
                    "editable": True,
                    "layered": True,
                    "layer_ids": ["base", "retouch"],
                    "mask_ids": ["subject-mask"],
                },
                "adapter_status": {"available": True, "fine_edit_mode": True, "mode": "fine-edit"},
                "export_validation": {
                    "valid": True,
                    "path": str(root / "final.jpg"),
                    "profile": "competition-quality",
                    "release_blockers": [],
                },
                "transformation_disclosure": "记录局部精修。",
            }
            report = {"items": [item], "chosen_candidates": []}

            self.assertTrue(_reconcile_quality_release_labels(report))
            self.assertTrue(report["items"][0]["editable_master"]["valid"])
            self.assertEqual(report["chosen_candidates"][0]["selection"]["final_label"], "competition-standard")

    def test_one_photo_builds_one_executable_variant(self) -> None:
        variants = build_variant_specs(score("single"), intent="competition-standard", max_candidates=4)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["variant_name"], "competition-standard")

    def test_photoshop_retains_only_final_master_and_removes_previous_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {
                "output_dir": str(root),
                "photo_id": "p-retention",
                "variant_name": "natural",
            }
            photoshop_dir = root / "final" / "p-retention"
            photoshop_dir.mkdir(parents=True)
            old_master = photoshop_dir / "old-master.psd"
            final_master = photoshop_dir / "final-master.psd"
            previous_preview = photoshop_dir / "previous-preview.jpg"
            for path in (old_master, final_master, previous_preview):
                path.write_bytes(path.name.encode("utf-8"))

            photoshop_mcp_adapter._cleanup_ephemeral_outputs(
                request,
                retain_psd=final_master,
                remove_previous_preview=str(previous_preview),
            )

            self.assertFalse(old_master.exists())
            self.assertTrue(final_master.exists())
            self.assertFalse(previous_preview.exists())

    def test_photoshop_operations_share_one_document_and_one_session_artifact_set(self) -> None:
        request = {
            "output_dir": "/tmp/photo-post-single-document",
            "run_id": "run-single-document",
            "photo_id": "p-single",
            "variant_name": "natural",
            "is_final_operation": False,
        }
        first_tool, first = photoshop_mcp_adapter._operation_arguments(
            request,
            {"operation_id": "op-one", "checkpoint": "before-one", "adapter_operation": "layer_operation"},
            "/tmp/source.tif",
            document_id="doc-1",
        )
        second_tool, second = photoshop_mcp_adapter._operation_arguments(
            request,
            {"operation_id": "op-two", "checkpoint": "before-two", "adapter_operation": "region_mask_operation"},
            first["export_path"],
            document_id="doc-1",
        )

        self.assertEqual(first["document_id"], "doc-1")
        self.assertEqual(second["document_id"], "doc-1")
        self.assertIsNone(first["after_path"])
        self.assertIsNone(second["after_path"])
        self.assertIsNone(first["export_path"])
        self.assertIsNone(second["export_path"])
        self.assertFalse(first["persist_final"])
        self.assertFalse(second["persist_final"])
        self.assertEqual(first["history_snapshot_name"], "photo-post:p-single:before-one")
        self.assertEqual(first["before_path"], "/tmp/source.tif")
        self.assertEqual(first["layer_operation"], "duplicate")
        self.assertNotIn("checkpoint_mode", first)
        self.assertNotIn("persist_intermediate", first)
        self.assertNotIn("save_master", first)
        self.assertEqual(second["operation_type"], "brightness_contrast")
        self.assertTrue(first_tool.startswith("photoshop_fine_edit__"))
        self.assertTrue(second_tool.startswith("photoshop_fine_edit__"))

    def test_operation_request_carries_document_and_history_policy_forward(self) -> None:
        job = {
            "photo_id": "p-forward",
            "variant_name": "natural",
            "source_path": "/tmp/source.ARW",
            "output_dir": "/tmp/photo-post-forward",
            "score": {"primary_category": "portrait-environmental"},
            "operation_graph": {
                "operations": [
                    {"operation_id": "op-one", "required": True},
                    {"operation_id": "op-final", "required": True},
                ],
            },
        }
        request = _operation_request(
            job,
            {"operation_id": "op-one", "checkpoint": "history-one"},
            "run-forward",
            runtime_context={"document_id": "doc-forward", "working_path": "/tmp/session-preview.jpg"},
        )

        self.assertEqual(request["document_id"], "doc-forward")
        self.assertEqual(request["history_policy"]["mode"], "photoshop-history")
        self.assertFalse(request["history_policy"]["persist_intermediate"])
        self.assertFalse(request["is_final_operation"])
        self.assertEqual(request["score"]["primary_category"], "portrait-environmental")

    def test_photoshop_toolchain_is_derived_from_required_plan_operations(self) -> None:
        request = {
            "adapter_plan": {
                "photoshop": {
                    "operations": [
                        {"tool": "portrait_beauty", "required": True},
                        {"tool": "sharpening", "required": True},
                        {"tool": "liquify", "required": False},
                        {"tool": "apply_crop", "required": True},
                    ]
                }
            }
        }

        tools = photoshop_mcp_adapter._requested_tools(request)

        self.assertEqual(tools, ["portrait_beauty", "sharpen", "apply_crop"])

    def test_verified_descriptor_uses_native_fine_edit_tool_without_extra_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "descriptors.sqlite3")
            registered = __import__("action_descriptor_registry").register_descriptor(
                db_path,
                name="liquify-v1",
                operation_type="liquify-bounded",
                descriptor=[{"_obj": "liquify", "strength": 0.1}],
                photoshop_version="27.9",
                document_modes=["RGB"],
                bit_depths=[16],
                parameter_schema={"strength": {"maximum": 0.2}},
            )
            request = {
                "output_dir": directory,
                "run_id": "run-descriptor",
                "photo_id": "p-descriptor",
                "variant_name": "natural",
                "is_final_operation": False,
            }
            operation = {
                "operation_id": "op-liquify",
                "checkpoint": "before-liquify",
                "adapter_operation": "liquify",
                "execution_route": {"tier": "descriptor-verified", "descriptor_id": registered["descriptor_id"]},
                "parameters": {"descriptor_id": registered["descriptor_id"], "strength": 0.1},
            }
            with patch.dict("os.environ", {"PHOTO_PHOTOSHOP_DESCRIPTOR_DB": db_path}, clear=False):
                tool_name, arguments = photoshop_mcp_adapter._operation_arguments(
                    request, operation, "/tmp/source.tif", document_id="doc-1"
                )

            self.assertEqual(tool_name, "photoshop_fine_edit__apply_tool_operation")
            self.assertEqual(arguments["tool"], "liquify")
            self.assertEqual(arguments["action_descriptors"], [{"_obj": "liquify", "strength": 0.1}])

    def test_final_photoshop_quality_evidence_detects_real_and_identity_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.jpg"
            changed = root / "changed.jpg"
            identical = root / "identical.jpg"
            Image.new("RGB", (400, 300), "#303030").save(before, quality=100)
            Image.new("RGB", (400, 300), "#707070").save(changed, quality=100)
            Image.new("RGB", (400, 300), "#303030").save(identical, quality=100)

            changed_quality = photoshop_mcp_adapter._final_quality_evidence(str(before), str(changed))
            identity_quality = photoshop_mcp_adapter._final_quality_evidence(str(before), str(identical))

            self.assertTrue(changed_quality["material_change"])
            self.assertIn("score", changed_quality)
            self.assertIn("technical_pass", changed_quality)
            self.assertIn("semantic_pass", changed_quality)
            self.assertFalse(identity_quality["material_change"])

    def test_photoshop_execute_uses_history_then_saves_one_final_master(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.tif"
            Image.new("RGB", (320, 200), "#789").save(source)
            calls: list[tuple[str, dict]] = []

            def fake_call(tool: str, arguments: dict, _session_id: str) -> dict:
                calls.append((tool, arguments))
                if tool == "photoshop_fine_edit__plan_toolchain":
                    return {"status": "planned"}
                if tool == "photoshop_document__list_documents":
                    return {"documents": [{"id": "doc-history", "path": str(source)}]}
                if tool == "photoshop_fine_edit__open_document":
                    return {"document_id": "doc-history"}
                if tool == "photoshop_fine_edit__save_master":
                    output = Path(str(arguments["path"]))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"layered-master")
                    return {
                        "status": "completed",
                        "master_path": str(output),
                        "master_sha256": "master-hash",
                        "format": str(arguments.get("format", "psd")).upper(),
                        "editable": True,
                        "layered": True,
                        "layer_ids": ["base", "retouch"],
                        "mask_ids": ["subject-mask"],
                    }
                if tool.endswith("apply_crop"):
                    preview = Path(str(arguments["export_path"]))
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (320, 200), "#89a").save(preview, quality=92)
                    return {
                        "document_evidence": {"output_document_id": "doc-history"},
                        "export_evidence": {"status": "completed", "path": str(preview)},
                        "history_evidence": {
                            "status": "snapshot-created",
                            "mode": "photoshop-history",
                            "snapshot_name": arguments["history_snapshot_name"],
                            "native_history_api": True,
                            "persistent": False,
                        },
                        "operation_manifest": {
                            "status": "valid",
                            "operation_id": arguments["operation_id"],
                        },
                    }
                return {
                    "document_evidence": {"output_document_id": "doc-history"},
                    "history_evidence": {
                        "status": "snapshot-created",
                        "mode": "photoshop-history",
                        "snapshot_name": arguments["history_snapshot_name"],
                        "native_history_api": True,
                        "persistent": False,
                    },
                    "operation_manifest": {
                        "status": "valid",
                        "operation_id": arguments["operation_id"],
                    },
                }

            common = {
                "output_dir": str(root),
                "run_id": "run-history",
                "photo_id": "p-history",
                "variant_name": "natural",
                "source_path": str(source),
                "working_path": str(source),
            }
            with patch.object(photoshop_mcp_adapter, "_call", side_effect=fake_call):
                non_final = photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {"operation_id": "op-one", "checkpoint": "phase-one", "adapter_operation": "layer_operation"},
                    "is_final_operation": False,
                })
                final = photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {"operation_id": "op-final", "checkpoint": "phase-final", "adapter_operation": "apply_crop"},
                    "is_final_operation": True,
                })
                post_final = photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {"operation_id": "op-after-final", "checkpoint": "phase-polish", "adapter_operation": "region_mask_operation"},
                    "is_final_operation": False,
                })

            self.assertEqual(non_final["status"], "completed")
            self.assertEqual(non_final["evidence"]["document_mode"], "single-document")
            self.assertEqual(non_final["evidence"]["rollback"]["status"], "snapshot-created")
            self.assertEqual(non_final["evidence"]["rollback"]["mode"], "photoshop-history")
            self.assertTrue(non_final["evidence"]["rollback"]["native_history_api"])
            self.assertEqual(final["status"], "completed")
            self.assertTrue((root / "final" / "p-history" / "working.psd").is_file())
            operation_calls = [arguments for tool, arguments in calls if tool.endswith("apply_layer_operation") or tool.endswith("apply_crop")]
            self.assertIsNone(operation_calls[0]["after_path"])
            self.assertIsNone(operation_calls[0]["export_path"])
            self.assertIsNotNone(operation_calls[-1]["after_path"])
            self.assertIsNotNone(operation_calls[-1]["export_path"])
            self.assertEqual(len([tool for tool, _ in calls if tool == "photoshop_fine_edit__save_master"]), 1)
            self.assertEqual(post_final["status"], "completed", post_final)
            self.assertTrue((root / "final" / "p-history" / "working.psd").is_file())

    def test_crop_export_recovery_does_not_apply_geometry_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.tif"
            Image.new("RGB", (320, 200), "#789").save(source)
            calls: list[tuple[str, dict]] = []

            def fake_call(tool: str, arguments: dict, _session_id: str) -> dict:
                calls.append((tool, arguments))
                if tool == "photoshop_fine_edit__plan_toolchain":
                    return {"status": "planned"}
                if tool == "photoshop_fine_edit__open_document":
                    return {"document_id": "doc-crop-recovery"}
                if tool == "photoshop_fine_edit__apply_crop":
                    return {
                        "document_evidence": {"output_document_id": "doc-crop-recovery"},
                        "geometry_evidence": {
                            "crop_bounds": {"left": 16, "top": 10, "right": 300, "bottom": 190}
                        },
                        "operation_manifest": {
                            "status": "valid",
                            "operation_id": arguments["operation_id"],
                            "entry": {"operation_id": arguments["operation_id"], "type": "large-crop"},
                        },
                    }
                if tool == "photoshop_fine_edit__save_master":
                    output = Path(str(arguments["path"]))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"layered-master")
                    return {
                        "status": "completed",
                        "master_path": str(output),
                        "master_sha256": "master-hash",
                        "format": "PSD",
                        "editable": True,
                        "layered": True,
                        "layer_ids": ["base", "retouch"],
                        "mask_ids": ["subject-mask"],
                    }
                if tool == "photoshop_fine_edit__export_jpeg":
                    output = Path(str(arguments["path"]))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (284, 180), "#89a").save(output, quality=92)
                    return {"status": "completed", "path": str(output), "sha256": "jpeg-hash"}
                raise AssertionError(f"Unexpected tool: {tool}")

            with patch.object(photoshop_mcp_adapter, "_call", side_effect=fake_call):
                result = photoshop_mcp_adapter.execute({
                    "output_dir": str(root),
                    "run_id": "run-crop-recovery",
                    "photo_id": "p-crop-recovery",
                    "variant_name": "competition-standard",
                    "source_path": str(source),
                    "working_path": str(source),
                    "operation": {
                        "operation_id": "op-crop",
                        "checkpoint": "phase-final",
                        "adapter_operation": "apply_crop",
                        "parameters": {
                            "crop_bounds": {"left": 0.05, "top": 0.05, "right": 0.95, "bottom": 0.95},
                            "crop_units": "normalized",
                        },
                    },
                    "is_final_operation": True,
                })

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len([tool for tool, _ in calls if tool == "photoshop_fine_edit__apply_crop"]), 1)
            self.assertEqual(len([tool for tool, _ in calls if tool == "photoshop_fine_edit__export_jpeg"]), 1)
            self.assertEqual(result["evidence"]["editable_master"]["layer_ids"], ["base", "retouch"])
            self.assertTrue(result["evidence"]["editable_master"]["valid"])

    def test_photoshop_execute_recovers_exact_manifest_after_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.tif"
            Image.new("RGB", (320, 200), "#789").save(source)
            calls: list[tuple[str, dict]] = []

            def fake_call(tool: str, arguments: dict, _session_id: str) -> dict:
                calls.append((tool, arguments))
                if tool == "photoshop_fine_edit__plan_toolchain":
                    return {"status": "planned"}
                if tool == "photoshop_fine_edit__open_document":
                    return {"document_id": "doc-replay"}
                if tool == "photoshop_fine_edit__get_operation_manifest":
                    operation_id = expected_operation_id
                    return {
                        "document_id": "doc-replay",
                        "operations": [{
                            "operation_id": operation_id,
                            "depends_on": [],
                            "type": "hue_saturation",
                            "region": "global-frame",
                            "mask_reference": str(source),
                            "mask_sha256": "mask",
                            "parameters": {"saturation": -12},
                            "risk": "medium",
                            "input_layer_id": "2",
                            "output_layer_id": "5",
                            "before_path": str(source),
                            "before_sha256": "before",
                            "generative": False,
                            "history_snapshot_name": operation_id,
                        }],
                        "checkpoints": [],
                        "manifest_hash": "document-manifest",
                    }
                return {
                    "requested_operation_id": expected_operation_id,
                    "operation_id": expected_operation_id,
                    "status": "completed",
                    "idempotent_replay": True,
                    "document_evidence": {
                        "output_document_id": "doc-replay",
                        "input_layer_id": "2",
                        "output_layer_id": "5",
                    },
                    "history_evidence": {
                        "status": "snapshot-created",
                        "mode": "photoshop-history",
                        "snapshot_name": arguments["history_snapshot_name"],
                        "native_history_api": True,
                        "persistent": False,
                    },
                    "operation_manifest": {
                        "status": "pending_artifact_hashes",
                        "operation_id": expected_operation_id,
                    },
                }

            common = {
                "output_dir": str(root),
                "run_id": "run-replay",
                "photo_id": "p-replay",
                "variant_name": "competition-standard",
                "source_path": str(source),
                "working_path": str(source),
                "is_final_operation": False,
            }
            expected_operation_id = (
                "op-colour:run-run-replay:variant-competition-standard"
            )
            with patch.object(photoshop_mcp_adapter, "_call", side_effect=fake_call):
                result = photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {
                        "operation_id": "op-colour",
                        "checkpoint": "before-colour",
                        "adapter_operation": "selective_color",
                    },
                })

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["evidence"]["operation_manifest"]["operation_id"],
                expected_operation_id,
            )
            self.assertEqual(
                result["evidence"]["operation_manifest"]["recovered_from"],
                "photoshop_fine_edit.get_operation_manifest",
            )
            self.assertEqual(result["evidence"]["rollback"]["mode"], "photoshop-history")
            self.assertTrue(result["evidence"]["rollback"]["native_history_api"])
            self.assertTrue(
                any(tool == "photoshop_fine_edit__get_operation_manifest" for tool, _ in calls)
            )

    def test_photoshop_execute_does_not_accept_a_previous_operation_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.tif"
            Image.new("RGB", (320, 200), "#789").save(source)
            runtime_dir = root / "adobe-runtime"
            runtime_dir.mkdir()
            session = runtime_dir / "ps-run-stale-p-stale.json"
            session.write_text(json.dumps({
                "opened": True,
                "document_id": "doc-stale",
                "working_path": str(source),
                "last_operation_manifest": {
                    "status": "valid",
                    "operation_id": "previous:run-run-stale:variant-natural",
                },
            }), encoding="utf-8")

            def fake_call(tool: str, arguments: dict, _session_id: str) -> dict:
                if tool == "photoshop_fine_edit__get_operation_manifest":
                    return {"document_id": "doc-stale", "operations": [], "checkpoints": []}
                if tool == "photoshop_document__list_documents":
                    return {"documents": [{"id": "doc-stale", "path": str(source)}]}
                return {
                    "requested_operation_id": arguments["operation_id"],
                    "operation_id": arguments["operation_id"],
                    "status": "completed",
                    "idempotent_replay": True,
                    "document_evidence": {"output_document_id": "doc-stale"},
                    "operation_manifest": {
                        "status": "pending_artifact_hashes",
                        "operation_id": arguments["operation_id"],
                    },
                }

            with patch.object(photoshop_mcp_adapter, "_call", side_effect=fake_call):
                result = photoshop_mcp_adapter.execute({
                    "output_dir": str(root),
                    "run_id": "run-stale",
                    "photo_id": "p-stale",
                    "variant_name": "natural",
                    "source_path": str(source),
                    "working_path": str(source),
                    "is_final_operation": False,
                    "operation": {
                        "operation_id": "op-new",
                        "checkpoint": "before-new",
                        "adapter_operation": "selective_color",
                    },
                })

            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["reason"], "photoshop_native_history_checkpoint_unverified")

    def test_photoshop_document_identity_is_revalidated_after_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "working.tif"
            Image.new("RGB", (320, 200), "#789").save(source)
            listing = {"documents": [{"id": 149, "path": str(source)}]}
            with patch.object(photoshop_mcp_adapter, "_call", return_value=listing):
                self.assertFalse(
                    photoshop_mcp_adapter._document_is_live(
                        {"opened": True, "document_id": "120"},
                        str(source),
                        "run:photo:variant",
                    )
                )
                self.assertTrue(
                    photoshop_mcp_adapter._document_is_live(
                        {"opened": True, "document_id": "149"},
                        str(source),
                        "run:photo:variant",
                    )
                )
            context = {"working_path": str(source)}
            self.assertTrue(
                _verified_document_identity_rollover(
                    "120",
                    "149",
                    {
                        "document_identity_reconciled": True,
                        "document_identity_previous": "120",
                        "document_path": str(source),
                    },
                    context,
                )
            )
            self.assertFalse(
                _verified_document_identity_rollover(
                    "120",
                    "149",
                    {
                        "document_identity_reconciled": True,
                        "document_identity_previous": "120",
                        "document_path": str(Path(directory) / "other.tif"),
                    },
                    context,
                )
            )

    def test_group_layer_does_not_replace_current_pixel_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "working.tif"
            Image.new("RGB", (320, 200), "#789").save(source)
            operation_calls: list[tuple[str, dict]] = []

            def fake_call(tool: str, arguments: dict, _session_id: str) -> dict:
                if tool == "photoshop_fine_edit__plan_toolchain":
                    return {"status": "planned"}
                if tool == "photoshop_document__list_documents":
                    return {"documents": [{"id": "doc-layer-routing", "path": str(source)}]}
                if tool == "photoshop_fine_edit__open_document":
                    return {"document_id": "doc-layer-routing"}
                operation_calls.append((tool, arguments))
                if tool.endswith("apply_layer_operation"):
                    output_id = "3" if arguments.get("layer_operation") == "create_group" else "2"
                else:
                    output_id = "4"
                return {
                    "document_evidence": {
                        "output_document_id": "doc-layer-routing",
                        "output_layer_id": output_id,
                    },
                    "history_evidence": {
                        "status": "snapshot-created",
                        "mode": "photoshop-history",
                        "snapshot_name": arguments["history_snapshot_name"],
                        "native_history_api": True,
                        "persistent": False,
                    },
                    "operation_manifest": {
                        "status": "valid",
                        "operation_id": arguments["operation_id"],
                        "manifest_hash": f"manifest-{output_id}",
                    },
                }

            common = {
                "output_dir": str(root),
                "run_id": "run-layer-routing",
                "photo_id": "p-layer-routing",
                "variant_name": "competition-standard",
                "source_path": str(source),
                "working_path": str(source),
                "is_final_operation": False,
            }
            with patch.object(photoshop_mcp_adapter, "_call", side_effect=fake_call):
                photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {
                        "operation_id": "op-duplicate",
                        "adapter_operation": "layer_operation",
                        "parameters": {"layer_operation": "duplicate"},
                    },
                })
                photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {
                        "operation_id": "op-group",
                        "adapter_operation": "layer_operation",
                        "parameters": {"layer_operation": "create_group"},
                    },
                })
                photoshop_mcp_adapter.execute({
                    **common,
                    "operation": {
                        "operation_id": "op-beauty",
                        "adapter_operation": "portrait_beauty",
                        "parameters": {},
                    },
                })

            beauty_arguments = next(arguments for tool, arguments in operation_calls if tool.endswith("apply_tool_operation"))
            self.assertEqual(beauty_arguments["input_layer_id"], "2")

    def test_disk_budget_is_read_from_run_manifest(self) -> None:
        self.assertEqual(
            _disk_budget_bytes({"resource_budget": {"disk_budget_bytes": 123456}}),
            123456,
        )

    def test_execution_pauses_before_starting_job_over_disk_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "source.jpg"
            Image.new("RGB", (320, 200), "#456").save(source)
            (root / "already-large.bin").write_bytes(b"0123456789")
            manifest = seal_run_manifest(create_run_manifest("run-budget", str(input_dir), "natural-enhancement", "full"))
            manifest["resource_budget"] = {"disk_budget_bytes": 1}
            item = score("p-budget")
            item["source_path"] = str(source)
            plan = build_edit_plan(item)
            item["variant_plans"] = [{"variant_name": "natural", **plan, "adapter_plan": {"lightroom": build_lightroom_plan(item), "photoshop": build_photoshop_plan(item)}}]
            jobs = build_execution_jobs([item], "run-budget", str(root), "mixed", {"generative": {"planning_eligible": True}})

            result = execute_batch(jobs, str(root / "queue.sqlite3"), str(root), manifest)

            self.assertEqual(result["execution"]["executions"][0]["status"], "paused")
            self.assertIn("disk_budget_exceeded", result["execution"]["executions"][0]["blockers"])

    def test_build_edit_plan_is_strictly_valid_before_execution(self) -> None:
        plan = build_edit_plan(score("p1"), variant_name="natural")
        self.assertEqual(validate_edit_plan(plan), [])

    def test_operation_graph_has_dependencies_and_keeps_optional_generation_visible(self) -> None:
        item = score("p2")
        item["visual_evidence"] = {
            "faces": 1,
            "face_boxes_display": [{"x": 0.42, "y": 0.24, "width": 0.16, "height": 0.24}],
            "display_dimensions": [1200, 1800],
            "box_coordinate_space": "display-top-left-normalized",
        }
        plan = build_edit_plan(item, variant_name="competition-standard")
        adapter = {
            "lightroom": build_lightroom_plan(item, variant="competition-standard"),
            "photoshop": build_photoshop_plan(item, "competition-standard", True, {"planning_eligible": True, "locality": "chat-window"}, "mixed"),
        }
        graph = build_operation_graph(item, plan, adapter, {"generative": {"planning_eligible": True}})
        self.assertEqual(validate_operation_graph(graph), [])
        self.assertTrue(graph["operations"])
        self.assertIn("rollback_policy", graph)
        self.assertIn("optional_operations", graph)
        self.assertIn("liquify", adapter["photoshop"]["tool_coverage"]["portrait"])
        graph_tools = {item.get("adapter_operation") for item in graph["operations"]}
        self.assertIn("region_mask_operation", graph_tools)
        self.assertIn("selective_color", graph_tools)
        self.assertNotIn("sharpening", graph_tools)
        self.assertTrue(any(item.get("tool") == "sharpening" and item.get("status") == "not-applicable" for item in graph["optional_operations"]))

    def test_operation_graph_dispatches_dynamic_lightroom_adapter_settings(self) -> None:
        item = score("dynamic-color", "landscape-nature")
        plan = build_edit_plan(item, variant_name="competition-standard")
        lightroom = build_lightroom_plan(item, variant="competition-standard")

        graph = build_operation_graph(item, plan, {"lightroom": lightroom}, {})
        operation = next(node for node in graph["operations"] if node["backend"] == "lightroom-mcp")

        self.assertEqual(operation["parameters"], lightroom["settings"])
        self.assertIn("sharpening", operation["parameters"])
        self.assertIn("hsl", operation["parameters"])

    def test_materialized_plan_matches_executable_color_and_operation_graph(self) -> None:
        item = score("night-plan", "architecture-urban-space")
        item["secondary_tags"] = ["night", "minimal"]
        item["visual_evidence"] = {
            "labels": [["night sky", 0.92], ["sky", 0.88]],
            "display_dimensions": [7008, 4672],
        }
        plan = build_edit_plan(item, intent="competition-standard", variant_name="competition-standard")
        adapter = {
            "lightroom": build_lightroom_plan(item, variant="competition-standard"),
            "photoshop": build_photoshop_plan(item, variant="competition-standard"),
        }
        graph = build_operation_graph(item, plan, adapter, {})

        result = materialize_executable_plan(plan, adapter, graph)

        self.assertEqual(result["global_adjustments"][0]["exposure"], adapter["lightroom"]["settings"]["exposure"])
        self.assertEqual(result["color_plan"]["lightroom"]["hsl"], adapter["lightroom"]["settings"]["hsl"])
        self.assertEqual(len(result["planned_operations"]), len(graph["operations"]))
        self.assertTrue(result["color_plan"]["photoshop_layers"])
        self.assertEqual(result["plan_consistency"]["status"], "synchronized")
        self.assertTrue(result["plan_consistency"]["global_adjustments_match_lightroom"])
        self.assertEqual(result["operations"], [])

        audited = attach_execution_audit(
            result,
            {
                "status": "completed",
                "operation_results": [
                    {"operation_id": operation["operation_id"], "status": "completed"}
                    for operation in graph["operations"]
                ],
                "downgrades": [],
                "blockers": [],
            },
            {
                "operation_id": graph["operations"][0]["operation_id"],
                "restore_status": "verified",
                "skipped_settings": [{"key": "Sharpness", "reason": "not-readable"}],
            },
        )
        statuses = [item["status"] for item in audited["planned_operations"]]
        self.assertEqual(statuses.count("completed-with-skips"), 1)
        self.assertEqual(statuses.count("completed"), len(graph["operations"]) - 1)
        self.assertEqual(audited["execution_audit"]["operation_counts"]["completed_with_skips"], 1)
        self.assertEqual(audited["execution_audit"]["lightroom"]["skipped_settings"][0]["key"], "Sharpness")

    def test_lightroom_adapter_maps_and_restores_white_balance_and_hsl(self) -> None:
        settings = lightroom_mcp_adapter._operation_settings({
            "white_balance": "Custom",
            "temperature": 5400,
            "tint": 7,
            "hsl": {
                "orange": {"hue": -2, "saturation": -3, "luminance": 6},
                "blue": {"saturation": 8},
            },
        })

        self.assertEqual(settings["WhiteBalance"], "Custom")
        self.assertEqual(settings["Temperature"], 5400)
        self.assertEqual(settings["Tint"], 7)
        self.assertEqual(settings["HueAdjustmentOrange"], -2)
        self.assertEqual(settings["SaturationAdjustmentOrange"], -3)
        self.assertEqual(settings["LuminanceAdjustmentOrange"], 6)
        self.assertEqual(settings["SaturationAdjustmentBlue"], 8)

        metadata = {"photo": {"developSettings": settings}}
        self.assertEqual(
            lightroom_mcp_adapter._metadata_settings(metadata, list(settings)),
            settings,
        )

    def test_lightroom_adapter_skips_settings_without_a_restore_checkpoint(self) -> None:
        requested = {
            "Exposure2012": 0.32,
            "Sharpness": 46,
            "SaturationAdjustmentBlue": -5,
        }
        metadata = {
            "developSettings": {
                "exposure": 0,
                "hsl": {"SaturationAdjustmentBlue": 0},
            }
        }

        executable, restore, skipped = lightroom_mcp_adapter._restorable_settings(metadata, requested)

        self.assertEqual(executable, {"Exposure2012": 0.32, "SaturationAdjustmentBlue": -5})
        self.assertEqual(restore, {"Exposure2012": 0, "SaturationAdjustmentBlue": 0})
        self.assertEqual(skipped, [{
            "key": "Sharpness",
            "reason": "lightroom_setting_not_readable_for_restore",
        }])

    def test_lightroom_detail_projection_is_readable_for_restore(self) -> None:
        metadata = {
            "developSettings": {
                "Sharpness": 18,
                "LuminanceSmoothing": 55,
                "LuminanceNoiseReductionDetail": 45,
                "LuminanceNoiseReductionContrast": 20,
                "ColorNoiseReduction": 35,
                "ColorNoiseReductionDetail": 50,
                "ColorNoiseReductionSmoothness": 50,
            }
        }
        requested = {
            "Sharpness": 24,
            "LuminanceSmoothing": 48,
            "LuminanceNoiseReductionDetail": 42,
            "LuminanceNoiseReductionContrast": 18,
            "ColorNoiseReduction": 32,
            "ColorNoiseReductionDetail": 47,
            "ColorNoiseReductionSmoothness": 44,
        }

        executable, restore, skipped = lightroom_mcp_adapter._restorable_settings(metadata, requested)

        self.assertEqual(executable, requested)
        self.assertEqual(restore, {
            "Sharpness": 18,
            "LuminanceSmoothing": 55,
            "LuminanceNoiseReductionDetail": 45,
            "LuminanceNoiseReductionContrast": 20,
            "ColorNoiseReduction": 35,
            "ColorNoiseReductionDetail": 50,
            "ColorNoiseReductionSmoothness": 50,
        })
        self.assertEqual(skipped, [])

    def test_non_portrait_graph_does_not_schedule_portrait_only_operations(self) -> None:
        item = score("architecture", "architecture-urban-space")
        plan = build_edit_plan(item, variant_name="competition-standard")
        adapter = {
            "lightroom": build_lightroom_plan(item, variant="competition-standard"),
            "photoshop": build_photoshop_plan(item, "competition-standard"),
        }

        graph = build_operation_graph(item, plan, adapter, {})
        graph_tools = [operation.get("adapter_operation") for operation in graph["operations"]]
        skipped_tools = [operation.get("tool") for operation in graph["optional_operations"] if operation.get("status") == "not-applicable"]

        self.assertNotIn("portrait_beauty", graph_tools)
        self.assertIn("portrait_beauty", skipped_tools)
        self.assertFalse(any(operation.get("adapter_operation") == "apply_crop" for operation in graph["operations"]))

    def test_calibration_is_project_scoped_and_series_has_anchor(self) -> None:
        items = [score("p3", "animal-wildlife"), score("p4", "landscape-nature")]
        calibration = build_project_calibration(items, "natural-enhancement", "auto")
        self.assertEqual(calibration["scope"], "current-project-only")
        self.assertEqual(calibration["status"], "skipped-auto-default")
        series = build_series_plan(items)
        self.assertEqual(series["series_count"], 2)
        self.assertTrue(all(item["anchor_photo_id"] for item in series["series"]))

    def test_series_inference_joins_related_same_scene_frames(self) -> None:
        first = score("p-series-1", "street-documentary")
        second = score("p-series-2", "street-documentary")
        first["source_path"] = "/photos/DSC00001.ARW"
        second["source_path"] = "/photos/DSC00002.ARW"
        first["technical_analysis"] = {"perceptual_hash": "ffffffffffffffff"}
        second["technical_analysis"] = {"perceptual_hash": "fffffffffffffffe"}
        series = build_series_plan([first, second])
        self.assertEqual(series["series_count"], 1)
        self.assertEqual(series["series"][0]["inference"], "visual-fingerprint-and-category")
        self.assertGreater(series["series"][0]["members"][1]["visual_similarity_to_anchor"], 0.9)

    def test_missing_host_adapters_pause_without_marking_quality_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            Image.new("RGB", (320, 200), "#456").save(source)
            input_dir = root / "input"
            input_dir.mkdir()
            source.rename(input_dir / "source.jpg")
            manifest = seal_run_manifest(create_run_manifest("run-full", str(input_dir), "natural-enhancement", "full"))
            item = score("p5")
            item["source_path"] = str(input_dir / "source.jpg")
            plan = build_edit_plan(item)
            item["variant_plans"] = [{"variant_name": "natural", **plan, "adapter_plan": {"lightroom": build_lightroom_plan(item), "photoshop": build_photoshop_plan(item)}}]
            jobs = build_execution_jobs([item], "run-full", str(root), "mixed", {"generative": {"planning_eligible": True}})
            queue = root / "queue.sqlite3"
            from durable_queue import enqueue
            enqueue(str(queue), "run-full", "p5", {"stage": "awaiting"}, item_id="run-full:p5:natural")
            # The production runtime auto-discovers the installed local
            # adapters. Make this contract test deterministic by explicitly
            # simulating a host where those adapters are unavailable.
            with patch("execution_engine._command_for_backend", return_value=(None, "missing")):
                result = execute_batch(jobs, str(queue), str(root), manifest)
            self.assertEqual(result["quality"]["status"], "pending-adapter-or-review")
            self.assertTrue((root / "quality-report.json").is_file())
            saved = json.loads((root / "quality-report.json").read_text())
            self.assertFalse(any(item.get("quality_status") == "evaluated" for item in saved["items"]))

    def test_execution_plan_records_real_completion_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "execution-plan.json"
            plan_path.write_text(json.dumps({
                "items": [{
                    "item_id": "run:p1:natural",
                    "photo_id": "p1",
                    "variant_name": "natural",
                    "stage": "awaiting-lightroom-adapter",
                }],
            }), encoding="utf-8")
            _sync_execution_plan(plan_path, {
                "executions": [{
                    "item_id": "run:p1:natural",
                    "photo_id": "p1",
                    "variant_name": "natural",
                    "status": "completed",
                }],
            })
            saved = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["items"][0]["stage"], "completed")
            self.assertEqual(saved["items"][0]["execution_status"], "completed")
            self.assertEqual(saved["execution"]["counts"], {"completed": 1})

    def test_report_status_matches_completed_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "execution-plan.json"
            report_path = root / "report.json"
            plan_path.write_text(json.dumps({"jobs": [], "items": []}), encoding="utf-8")
            report_path.write_text(json.dumps({"scores": [], "status": None}), encoding="utf-8")
            _sync_top_level_report(
                plan_path,
                [],
                {
                    "quality": {
                        "status": "completed",
                        "items": [{"quality_status": "evaluated"}],
                        "release_ready_count": 1,
                    }
                },
                str(root / "queue.sqlite3"),
            )
            self.assertEqual(json.loads(report_path.read_text())["status"], "completed")


if __name__ == "__main__":
    unittest.main()
