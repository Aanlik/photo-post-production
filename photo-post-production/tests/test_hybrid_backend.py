"""Tests for the chat-window image generation routing contract."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from capability_registry import probe_generative_backend  # noqa: E402
from chat_window_image_backend import (  # noqa: E402
    create_batch_manifest,
    probe_chat_window_image_backend,
    record_result,
)
from durable_queue import enqueue, get_item  # noqa: E402
from run_manifest import create_run_manifest, seal_run_manifest, verify_run_manifest  # noqa: E402


class ChatWindowBackendTests(unittest.TestCase):
    def test_probe_is_host_gated_and_never_api_mode(self) -> None:
        record = probe_chat_window_image_backend(tool_available=False)
        self.assertFalse(record["available"])
        self.assertTrue(record["planning_eligible"])
        self.assertFalse(record["api_mode"])
        self.assertEqual(record["locality"], "chat-window")
        self.assertTrue(record["requires_visible_conversation_image"])

        ready = probe_chat_window_image_backend(tool_available=True)
        self.assertTrue(ready["available"])
        self.assertTrue(ready["ready_for_execution"])
        self.assertTrue(ready["verified"])

    def test_mixed_capability_routes_to_chat_window(self) -> None:
        with patch.dict(os.environ, {"PHOTO_GENERATIVE_BACKEND": "chat-window"}, clear=False):
            record = probe_generative_backend(processing_locality="mixed", network=False)
        self.assertEqual(record["backend"], "chatgpt-built-in-imagegen")
        self.assertTrue(record["planning_eligible"])
        self.assertFalse(record["api_mode"])
        self.assertEqual(record["locality"], "chat-window")

    def test_api_backend_is_disabled_in_chat_window_mode(self) -> None:
        with patch.dict(os.environ, {"PHOTO_GENERATIVE_BACKEND": "openai-image"}, clear=False):
            record = probe_generative_backend(processing_locality="mixed", network=False)
        self.assertFalse(record["available"])
        self.assertFalse(record["planning_eligible"])
        self.assertTrue(record["api_mode"])
        self.assertEqual(record["reason"], "api_backend_disabled_by_user_chat_window_mode")

    def test_batch_manifest_is_one_host_call_per_job_and_rejects_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            raw = root / "source.ARW"
            Image.new("RGB", (32, 24), "navy").save(source)
            raw.write_bytes(b"raw")
            manifest = create_batch_manifest(
                str(root / "run"),
                [{"source_path": str(source), "prompt": "自然移除右下角杂物", "invariants": ["保留主体"]}],
                "run-1",
            )
            self.assertEqual(manifest["backend"], "chatgpt-built-in-imagegen")
            self.assertFalse(manifest["api_mode"])
            self.assertEqual(manifest["execution"], "one-built-in-imagegen-call-per-job")
            self.assertEqual(manifest["jobs"][0]["status"], "awaiting-host-imagegen")
            self.assertTrue(manifest["jobs"][0]["requires_visible_conversation_image"])
            with self.assertRaises(ValueError):
                create_batch_manifest(
                    str(root / "run-raw"),
                    [{"source_path": str(raw), "prompt": "不可直接编辑 RAW"}],
                    "run-raw",
                )

    def test_record_result_copies_host_output_and_updates_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            generated = root / "host-output.png"
            Image.new("RGB", (32, 24), "navy").save(source)
            Image.new("RGB", (32, 24), "orange").save(generated)
            manifest = create_batch_manifest(
                str(root / "run"),
                [{"job_id": "job-1", "source_path": str(source), "prompt": "保留主体并移除杂物", "resume": {"queue_path": str(root / "queue.sqlite3"), "queue_item_id": "run-2:photo-1:natural"}}],
                "run-2",
            )
            enqueue(str(root / "queue.sqlite3"), "run-2", "photo-1", {"stage": "chat-imagegen"}, item_id="run-2:photo-1:natural")
            result = record_result(manifest["manifest_path"], "job-1", str(generated), note="host image_gen result")
            self.assertEqual(result["status"], "completed")
            output = Path(result["output_path"])
            self.assertTrue(output.is_file())
            saved = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["completed_jobs"], 1)
            self.assertEqual(saved["pending_jobs"], 0)
            self.assertEqual(saved["jobs"][0]["status"], "completed")
            self.assertTrue(Path(result["resume_request_path"]).is_file())
            self.assertEqual(get_item(str(root / "queue.sqlite3"), "run-2:photo-1:natural")["checkpoint"]["stage"], "chat-window-imagegen-completed")

    def test_locality_is_explicitly_sealed_in_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = create_run_manifest("hybrid-run", directory, "natural-enhancement", "full")
            manifest["processing_locality"] = "mixed"
            manifest["locality_policy"] = "mixed"
            sealed = seal_run_manifest(manifest)
            self.assertTrue(verify_run_manifest(sealed))
            self.assertEqual(sealed["trusted_context"]["locality_policy"], "mixed")


if __name__ == "__main__":
    unittest.main()
