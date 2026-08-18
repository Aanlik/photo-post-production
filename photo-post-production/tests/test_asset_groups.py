"""Focused tests for grouping related capture and export assets."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from asset_groups import group_related_assets  # noqa: E402


class AssetGroupTests(unittest.TestCase):
    def test_groups_raw_jpeg_and_xmp_sidecars(self) -> None:
        paths = ["/shoot/DSC_0100.ARW", "/shoot/DSC_0100.JPG", "/shoot/DSC_0100.xmp"]

        groups = group_related_assets(paths)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["relationship"], "sidecars")
        self.assertEqual(groups[0]["primary_path"], "/shoot/DSC_0100.ARW")
        self.assertEqual(groups[0]["paths"], paths)

    def test_groups_contiguous_burst_bracket_and_panorama_sequences(self) -> None:
        paths = [
            "/shoot/DSC_0200.ARW", "/shoot/DSC_0201.ARW", "/shoot/DSC_0202.ARW",
            "/shoot/DSC_0300_-2EV.ARW", "/shoot/DSC_0300_0EV.ARW", "/shoot/DSC_0300_+2EV.ARW",
            "/shoot/DSC_0400_PANO_01.ARW", "/shoot/DSC_0400_PANO_02.ARW",
        ]

        by_relationship = {group["relationship"]: group for group in group_related_assets(paths)}

        self.assertEqual(by_relationship["burst"]["paths"], paths[:3])
        self.assertEqual(by_relationship["bracket"]["paths"], paths[3:6])
        self.assertEqual(by_relationship["panorama"]["paths"], paths[6:])

    def test_groups_historical_exports_with_their_source_family(self) -> None:
        paths = ["/shoot/DSC_0500.ARW", "/shoot/DSC_0500-edit.jpg", "/shoot/DSC_0500_final-export.tif"]

        groups = group_related_assets(paths)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["relationship"], "historical-export")
        self.assertEqual(groups[0]["primary_path"], "/shoot/DSC_0500.ARW")
        self.assertEqual(groups[0]["paths"], paths)


if __name__ == "__main__":
    unittest.main()
