"""Tests for the local visual evidence and one-command analysis pipeline."""

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

from durable_queue import claim_next, enqueue, get_item, transition, update_checkpoint  # noqa: E402
from reference_style import analyze_reference, derive_style_recipe, register_reference  # noqa: E402
from run_pipeline import run_pipeline  # noqa: E402
from schema_validator import validate_score_record  # noqa: E402
from style_memory import get_profile, init_store  # noqa: E402
from visual_analysis import analyze_visual_record, as_score_record  # noqa: E402


class VisualEvidenceTests(unittest.TestCase):
    def _record(self, path: Path) -> dict:
        return {"source_path": str(path), "preview_path": str(path), "photo_id": "photo-1", "technical_analysis": {
            "dimensions": [400, 300], "mean_luma": 0.42, "mean_chroma": 0.24,
            "highlight_clipping": 0.01, "shadow_crush": 0.02, "blur_proxy": 0.25, "noise_proxy": 0.12,
        }}

    def test_local_vision_evidence_maps_animals_and_schema_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "animal.jpg"
            Image.new("RGB", (400, 300), "#647f38").save(path)
            record = analyze_visual_record(self._record(path), vision={
                "available": True,
                "backend": "test-vision",
                "classifications": [{"identifier": "animal", "confidence": 0.94}],
                "animals": [{"identifier": "dog", "confidence": 0.96}],
                "faces": [],
                "text": [],
            })
            self.assertEqual(record["primary_category"], "animal-wildlife")
            self.assertEqual(validate_score_record(as_score_record(record)), [])
            self.assertIn("animal-wildlife", record["primary_category"])

    def test_reference_learning_stores_attributes_not_image_bytes_and_derives_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "reference.jpg"
            image_bytes = b"reference-image-bytes-never-written-to-sqlite"
            Image.new("RGB", (400, 300), "#a06038").save(image)
            db = root / "memory.sqlite3"
            init_store(str(db))
            register_reference(str(db), str(image), "授权样片", "https://example.test/photo/1", "CC0", category="landscape-nature")
            recipe = derive_style_recipe(str(db), category="landscape-nature")
            profile = get_profile(str(db))
            self.assertEqual(recipe["status"], "derived")
            self.assertEqual(len(profile["references"]), 1)
            self.assertNotIn("image_bytes", json.dumps(profile, ensure_ascii=False))
            self.assertNotIn(image_bytes, db.read_bytes())

    def test_durable_queue_claim_checkpoint_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = str(Path(directory) / "queue.sqlite3")
            item_id = enqueue(queue, "run-1", "photo-1", {"stage": "analysis"})
            claimed = claim_next(queue, "run-1")
            self.assertEqual(claimed["state"], "processing")
            update_checkpoint(queue, item_id, {"path": "/tmp/checkpoint.psd", "sha256": "abc"})
            transition(queue, item_id, "paused")
            transition(queue, item_id, "queued")
            resumed = claim_next(queue, "run-1")
            self.assertEqual(resumed["attempts"], 2)
            self.assertEqual(get_item(queue, item_id)["checkpoint"]["sha256"], "abc")

    def test_one_command_pipeline_writes_review_board_scores_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            output = root / "output"
            source.mkdir()
            Image.new("RGB", (320, 200), "#234567").save(source / "DSC00001.jpg", quality=92)
            report = run_pipeline(str(source), str(output), run_id="pipeline-test", mode="review", use_vision=False, style_db=str(root / "memory.sqlite3"))
            self.assertEqual(report["run_id"], "pipeline-test")
            self.assertTrue((output / "report.json").is_file())
            self.assertTrue((output / "review-board.md").is_file())
            self.assertTrue((output / "scores.csv").is_file())
            self.assertTrue((output / "run-manifest.json").is_file())
            self.assertTrue(report["counts"]["review"] >= 1)

    def test_default_pipeline_skips_chat_batch_without_required_generative_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            output = root / "output"
            source.mkdir()
            image = source / "DSC00002.jpg"
            Image.new("RGB", (640, 480), "#4678a0").save(image, quality=95)

            def fake_analysis(records, use_vision=True):
                item = self._record(Path(records[0]["preview_path"]))
                item.update({
                    "source_path": records[0]["source_path"],
                    "preview_path": records[0]["preview_path"],
                    "photo_id": "photo-auto",
                    "primary_category": "landscape-nature",
                    "candidate_potential": 88.1,
                    "score_confidence": 0.92,
                    "decision": "selected",
                    "technical_gates": {"source_readable": "pass", "irrecoverable_defect": "pass", "preview_quality": "pass"},
                    "recommended_treatment": ["平衡天空与地面层次"],
                    "score_record": {
                        "primary_category": "landscape-nature",
                        "secondary_tags": [],
                        "classification_confidence": 0.92,
                        "score_version": "test",
                        "evidence": ["test"],
                        "score_confidence": 0.92,
                        "style_fit": 50.0,
                        "technical_gates": {"source_readable": "pass", "irrecoverable_defect": "pass", "preview_quality": "pass"},
                        "technical": 88.0,
                        "composition": 88.0,
                        "light_color": 88.0,
                        "moment_story": 88.0,
                        "coherence": 88.0,
                        "photographic_value": 88.0,
                        "editability": 88.0,
                        "expected_gain": 80.0,
                        "keep_value": 90.0,
                        "candidate_potential": 88.1,
                        "final_score": None,
                        "decision": "selected",
                        "strengths": ["测试高潜力照片"],
                        "risks": [],
                        "recommended_treatment": ["平衡天空与地面层次"],
                    },
                })
                return [item]

            with patch("run_pipeline.analyze_visual_paths", fake_analysis):
                report = run_pipeline(str(source), str(output), run_id="auto-chat-test", style_db=str(root / "memory.sqlite3"))
            self.assertEqual(report["mode"], "auto")
            self.assertEqual(report["chat_window_batch"]["status"], "not-created")
            self.assertEqual(report["chat_window_batch"]["reason"], "no-required-generative-operations")
            self.assertFalse((output / "chat-window-results" / "chat-window-image-batch.json").is_file())


if __name__ == "__main__":
    unittest.main()
