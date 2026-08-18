"""Focused tests for local, metadata-only style memory."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import math
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from style_memory import (  # noqa: E402
    get_profile,
    init_store,
    record_feedback,
    record_pairwise_feedback,
    reset_profile,
)
from reference_style import derive_style_recipe, register_reference  # noqa: E402
from PIL import Image  # noqa: E402


class StyleMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temporary_directory.name) / "memory.sqlite3")
        init_store(self.db_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_feedback_is_aggregated_with_region_text_weight_and_source_run(self) -> None:
        positive_id = record_feedback(self.db_path, "run-1", "photo-a", "positive", "主体更亮", "subject")
        negative_id = record_feedback(self.db_path, "run-2", "photo-b", "negative", "不喜欢一眼黑", "background", 2.0)

        profile = get_profile(self.db_path)

        self.assertEqual(positive_id, 1)
        self.assertEqual(negative_id, 2)
        self.assertEqual(profile["version"], 2)
        self.assertEqual(profile["feedback"]["positive"][0]["text"], "主体更亮")
        self.assertEqual(profile["feedback"]["positive"][0]["photo_id"], "photo-a")
        self.assertEqual(profile["feedback"]["positive"][0]["region"], "subject")
        self.assertEqual(profile["feedback"]["positive"][0]["source_run_id"], "run-1")
        self.assertEqual(profile["feedback"]["negative"][0]["weight"], 2.0)
        self.assertEqual(profile["feedback"]["negative"][0]["region"], "background")

    def test_pairwise_feedback_preserves_aspect_and_source_run(self) -> None:
        event_id = record_pairwise_feedback(self.db_path, "run-pair", "photo-a", "photo-b", "color", 1.5)

        pairwise = get_profile(self.db_path)["pairwise"]

        self.assertEqual(event_id, 1)
        self.assertEqual(pairwise[0]["better_photo_id"], "photo-a")
        self.assertEqual(pairwise[0]["worse_photo_id"], "photo-b")
        self.assertEqual(pairwise[0]["aspect"], "color")
        self.assertEqual(pairwise[0]["source_run_id"], "run-pair")

    def test_one_correction_is_retained_but_not_promoted_to_guidance(self) -> None:
        record_feedback(self.db_path, "run-1", "photo-a", "negative", "主体太暗", "subject")

        profile = get_profile(self.db_path)

        self.assertEqual(len(profile["feedback"]["negative"]), 1)
        self.assertEqual(profile["guidance"], [])

    def test_repeated_matching_feedback_promotes_bounded_guidance(self) -> None:
        for run_id in ("run-1", "run-2", "run-3"):
            record_feedback(self.db_path, run_id, "photo-a", "negative", "主体太暗", "subject")

        guidance = get_profile(self.db_path)["guidance"]

        self.assertEqual(len(guidance), 1)
        self.assertEqual(guidance[0]["kind"], "negative")
        self.assertEqual(guidance[0]["region"], "subject")
        self.assertEqual(guidance[0]["supporting_events"], 3)
        self.assertLessEqual(guidance[0]["strength"], 1.0)

    def test_repeated_feedback_changes_the_derived_recipe(self) -> None:
        image = Path(self.temporary_directory.name) / "reference.jpg"
        Image.new("RGB", (400, 300), "#445566").save(image)
        register_reference(self.db_path, str(image), "授权参考", "https://example.test/reference", "CC0", category="landscape-nature")
        for run_id in ("run-1", "run-2"):
            record_feedback(self.db_path, run_id, "photo-a", "negative", "不要一眼黑，主体太暗", "subject")

        recipe = derive_style_recipe(self.db_path, category="landscape-nature")

        self.assertGreater(recipe["recipe"]["lightroom"]["shadow_lift"], 0)
        self.assertIn("feedback_guidance", recipe["recipe"])

    def test_project_scope_is_separate_from_long_term_scope(self) -> None:
        record_feedback(self.db_path, "run-long", "photo-a", "positive", "自然肤色", "color")
        record_feedback(self.db_path, "project:portraits:run-project", "photo-b", "negative", "背景太亮", "background")

        long_term = get_profile(self.db_path)
        project = get_profile(self.db_path, "project:portraits")

        self.assertEqual(len(long_term["feedback"]["positive"]), 1)
        self.assertEqual(long_term["feedback"]["negative"], [])
        self.assertEqual(len(project["feedback"]["negative"]), 1)
        self.assertEqual(project["feedback"]["positive"], [])

    def test_reset_deactivates_old_events_and_starts_a_new_profile_version(self) -> None:
        record_feedback(self.db_path, "run-1", "photo-a", "positive", "主体更亮", "subject")
        before_reset = get_profile(self.db_path)

        reset_profile(self.db_path)
        after_reset = get_profile(self.db_path)

        self.assertEqual(after_reset["feedback"], {"positive": [], "negative": []})
        self.assertEqual(after_reset["pairwise"], [])
        self.assertEqual(after_reset["version"], before_reset["version"] + 1)
        self.assertEqual(after_reset["reset_count"], 1)

        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM preference_events WHERE active = 0").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs WHERE run_id = 'run-1'").fetchone()[0], 1)

    def test_store_has_metadata_tables_and_no_image_blob_column_or_bytes(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            columns = {row[1] for row in connection.execute("PRAGMA table_info(preference_events)")}

        self.assertTrue({"profiles", "preference_events", "runs"}.issubset(tables))
        self.assertNotIn("image", columns)
        self.assertNotIn("image_bytes", columns)

    def test_feedback_rejects_non_finite_weights(self) -> None:
        for weight in (math.nan, math.inf, -math.inf):
            with self.subTest(weight=weight):
                with self.assertRaises(ValueError):
                    record_feedback(self.db_path, "run-invalid", "photo-a", "positive", "主体更亮", "subject", weight)

    def test_pairwise_feedback_rejects_non_finite_weights(self) -> None:
        for weight in (math.nan, math.inf, -math.inf):
            with self.subTest(weight=weight):
                with self.assertRaises(ValueError):
                    record_pairwise_feedback(self.db_path, "run-invalid", "photo-a", "photo-b", "color", weight)


if __name__ == "__main__":
    unittest.main()
