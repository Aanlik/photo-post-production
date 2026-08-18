"""Focused tests for cache identity and isolated photo state transitions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from cache_store import cache_key  # noqa: E402
from photo_state import transition_photo_state  # noqa: E402


class CacheStoreTests(unittest.TestCase):
    def test_cache_key_changes_when_any_input_version_changes(self) -> None:
        baseline = cache_key("source", "config", "1.0", {"pillow": "10.0"}, "model-a")
        variants = [
            cache_key("source-2", "config", "1.0", {"pillow": "10.0"}, "model-a"),
            cache_key("source", "config-2", "1.0", {"pillow": "10.0"}, "model-a"),
            cache_key("source", "config", "1.1", {"pillow": "10.0"}, "model-a"),
            cache_key("source", "config", "1.0", {"pillow": "10.1"}, "model-a"),
            cache_key("source", "config", "1.0", {"pillow": "10.0"}, "model-b"),
        ]

        self.assertTrue(all(key != baseline for key in variants))
        self.assertEqual(baseline, cache_key("source", "config", "1.0", {"pillow": "10.0"}, "model-a"))


class PhotoStateTests(unittest.TestCase):
    def test_valid_transition_returns_explicit_new_state(self) -> None:
        transition = transition_photo_state("photo-1", "queued", "analyzing")

        self.assertEqual(transition["photo_id"], "photo-1")
        self.assertEqual(transition["previous_state"], "queued")
        self.assertEqual(transition["state"], "analyzing")
        self.assertTrue(transition["accepted"])

    def test_failed_photo_is_isolated_and_cannot_advance_without_retry(self) -> None:
        failed = transition_photo_state("broken", "analyzing", "failed")
        rejected = transition_photo_state("broken", "failed", "analyzed")

        self.assertTrue(failed["accepted"])
        self.assertEqual(failed["state"], "failed")
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["state"], "failed")
        self.assertIn("error", rejected)


if __name__ == "__main__":
    unittest.main()
