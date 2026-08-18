"""Tests for analysis-only RAW/JPEG batch preparation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from prepare_batch import prepare_batch  # noqa: E402


class PrepareBatchTests(unittest.TestCase):
    def test_creates_external_manifest_and_keeps_semantic_scoring_for_ai(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            output = root / "output"
            source.mkdir()
            Image.new("RGB", (320, 200), "#234").save(source / "DSC00001.jpg", quality=92)

            manifest = prepare_batch(str(source), str(output), "test-run")

            self.assertTrue((output / "batch-manifest.json").is_file())
            self.assertTrue(manifest["source_policy"]["originals_untouched"])
            self.assertEqual(manifest["classification"]["status"], "ai_review_required")
            self.assertTrue(manifest["classification"]["animal_category_enabled"])
            self.assertEqual(len(manifest["asset_groups"]), 1)
            self.assertEqual(manifest["preview_records"][0]["state"], "preview-ready")
            self.assertTrue(Path(manifest["preparation"]["progress_path"]).is_file())
            self.assertEqual(manifest["preparation"]["failed_sources"], 0)
            saved = json.loads((output / "batch-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["run_id"], "test-run")
            self.assertEqual(Path(saved["manifest_path"]).resolve(), (output / "batch-manifest.json").resolve())

    def test_rejects_output_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            source.mkdir()
            with self.assertRaises(ValueError):
                prepare_batch(str(source), str(source / "output"))

    def test_accepts_one_photo_as_input_without_touching_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DSC00002.jpg"
            output = root / "output"
            Image.new("RGB", (320, 200), "#456").save(source, quality=92)
            before = source.read_bytes()

            manifest = prepare_batch(str(source), str(output), "single-file-run")

            self.assertEqual(manifest["input_kind"], "file")
            self.assertEqual(len(manifest["asset_groups"]), 1)
            self.assertEqual(manifest["source_snapshot"]["assets"][0]["path"], str(source.resolve()))
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
