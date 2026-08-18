"""Focused tests for deterministic local preview analysis."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageFilter


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from batch_analyzer import analyze_paths, cluster_near_duplicates  # noqa: E402
from image_metrics import analyze_preview, compare_rendered_pixels, evaluate_rendered_candidate  # noqa: E402


class ImageMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.gradient = self.directory / "gradient.png"
        self.clipped = self.directory / "clipped.png"
        self.crushed = self.directory / "crushed.png"
        self.identical_a = self.directory / "identical-a.png"
        self.identical_b = self.directory / "identical-b.png"
        self.sharp_checkerboard = self.directory / "sharp-checkerboard.png"
        self.blurred_checkerboard = self.directory / "blurred-checkerboard.png"
        self.uniform = self.directory / "uniform.png"

        gradient = Image.new("RGB", (32, 16))
        gradient.putdata([(x * 255 // 31, y * 255 // 15, 128) for y in range(16) for x in range(32)])
        gradient.save(self.gradient)
        Image.new("RGB", (32, 16), "white").save(self.clipped)
        Image.new("RGB", (32, 16), "black").save(self.crushed)
        Image.new("RGB", (32, 16), (32, 96, 160)).save(self.identical_a)
        Image.new("RGB", (32, 16), (32, 96, 160)).save(self.identical_b)
        checkerboard = Image.new("L", (64, 64))
        checkerboard.putdata([255 if (x + y) % 2 else 0 for y in range(64) for x in range(64)])
        checkerboard.save(self.sharp_checkerboard)
        checkerboard.filter(ImageFilter.GaussianBlur(radius=2)).save(self.blurred_checkerboard)
        Image.new("L", (64, 64), 128).save(self.uniform)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_preview_metrics_are_stable_and_json_serializable(self) -> None:
        first = analyze_preview(str(self.gradient))
        second = analyze_preview(str(self.gradient))

        self.assertEqual(first, second)
        self.assertEqual(first["width"], 32)
        self.assertEqual(first["height"], 16)
        self.assertEqual(first["dimensions"], [32, 16])
        self.assertIsInstance(first["perceptual_hash"], str)
        json.dumps(first, sort_keys=True)

    def test_large_preview_keeps_source_dimensions_but_bounds_analysis_pixels(self) -> None:
        large = self.directory / "large.png"
        Image.new("RGB", (3200, 1800), (80, 120, 160)).save(large)

        metrics = analyze_preview(str(large))

        self.assertEqual(metrics["dimensions"], [3200, 1800])
        self.assertLessEqual(max(metrics["analysis_dimensions"]), 1600)
        self.assertLess(metrics["analysis_pixel_count"], metrics["pixel_count"])
        json.dumps(metrics, sort_keys=True)

    def test_clipped_white_has_more_highlight_clipping_than_gradient(self) -> None:
        self.assertGreater(
            analyze_preview(str(self.clipped))["highlight_clipping"],
            analyze_preview(str(self.gradient))["highlight_clipping"],
        )

    def test_crushed_black_has_more_shadow_crush_than_gradient(self) -> None:
        self.assertGreater(
            analyze_preview(str(self.crushed))["shadow_crush"],
            analyze_preview(str(self.gradient))["shadow_crush"],
        )

    def test_blur_proxy_ranks_sharp_checkerboard_below_its_gaussian_blur(self) -> None:
        sharp_metrics = analyze_preview(str(self.sharp_checkerboard))
        blurred_metrics = analyze_preview(str(self.blurred_checkerboard))
        uniform_metrics = analyze_preview(str(self.uniform))
        sharp = sharp_metrics["blur_proxy"]
        blurred = blurred_metrics["blur_proxy"]
        uniform = uniform_metrics["blur_proxy"]

        self.assertGreaterEqual(sharp, 0.0)
        self.assertLessEqual(uniform, 1.0)
        self.assertLess(sharp, blurred)
        self.assertLess(sharp, uniform)
        self.assertGreater(sharp_metrics["edge_p95"], blurred_metrics["edge_p95"])
        self.assertGreater(blurred_metrics["edge_p95"], uniform_metrics["edge_p95"])

    def test_identical_images_form_one_duplicate_cluster(self) -> None:
        records = analyze_paths([str(self.identical_a), str(self.identical_b)])

        self.assertEqual(cluster_near_duplicates(records), [[str(self.identical_a), str(self.identical_b)]])

    def test_unreadable_photo_is_reported_without_blocking_other_photos(self) -> None:
        missing = self.directory / "missing.png"
        records = analyze_paths([str(self.gradient), str(missing)])

        self.assertEqual(records[0]["state"], "analyzed")
        self.assertEqual(records[1]["state"], "failed")
        self.assertIn("error", records[1])

    def test_rendered_quality_comes_from_pixels_not_a_pre_score(self) -> None:
        better = evaluate_rendered_candidate(str(self.gradient), str(self.sharp_checkerboard))
        worse = evaluate_rendered_candidate(str(self.gradient), str(self.crushed))

        self.assertEqual(better["source"], "post-render-pixels")
        self.assertEqual(better["status"], "evaluated")
        self.assertEqual(worse["status"], "evaluated")
        self.assertNotEqual(better["final_score"], worse["final_score"])
        self.assertLess(worse["technical_score"], better["technical_score"])

    def test_material_change_gate_rejects_identity_and_accepts_crop_or_tone_change(self) -> None:
        identity = compare_rendered_pixels(str(self.identical_a), str(self.identical_b))
        changed = compare_rendered_pixels(str(self.identical_a), str(self.gradient))

        self.assertEqual(identity["status"], "evaluated")
        self.assertFalse(identity["material_change"])
        self.assertTrue(changed["material_change"])


if __name__ == "__main__":
    unittest.main()
