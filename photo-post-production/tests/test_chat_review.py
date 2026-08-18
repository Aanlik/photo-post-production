"""Tests for direct-in-chat review sheets."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chat_review import render_chat_sheet  # noqa: E402


class ChatReviewTests(unittest.TestCase):
    def test_renders_numbered_sheet_and_reports_missing_previews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (120, 80), "#c44").save(root / "DSC00001.jpg")
            result = render_chat_sheet(
                [
                    {"review_key": "A01", "filename": "DSC00001.ARW", "category": "animal-wildlife", "candidate_potential": 88},
                    {"review_key": "A02", "filename": "DSC00002.ARW", "category": "animal-wildlife", "candidate_potential": 80},
                ],
                str(root),
                str(root / "chat-A.jpg"),
                "A",
                columns=2,
                cell_width=240,
                cell_height=180,
            )
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["rendered"], 1)
            self.assertEqual(result["missing_previews"], ["DSC00002.ARW"])
            with Image.open(root / "chat-A.jpg") as image:
                self.assertEqual(image.size, (480, 180))


if __name__ == "__main__":
    unittest.main()
