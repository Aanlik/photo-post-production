"""Contracts for profile-driven export validation."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from validate_export import validate_export  # noqa: E402


def make_jpeg(path: Path, size: tuple[int, int] = (2400, 1600), quality: int = 92) -> None:
    Image.new("RGB", size, "navy").save(path, "JPEG", quality=quality, icc_profile=b"sRGB IEC61966-2.1")


class ExportValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "source.RAW"
        self.source.write_bytes(b"raw-source-bytes")
        self.export = self.root / "export.jpg"
        make_jpeg(self.export)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def policy(self, **changes: object) -> dict:
        policy = {
            "required_fields": [],
            "source_checksum": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "result_snapshot_path": str(self.export),
            "result_checksum": hashlib.sha256(self.export.read_bytes()).hexdigest(),
            "result_snapshot_sha256": hashlib.sha256(self.export.read_bytes()).hexdigest(),
            "expected_source_path": str(self.source),
            "expected_icc_profile": "sRGB",
        }
        policy.update(changes)
        return policy

    def test_rejects_transformed_jpeg_as_source(self) -> None:
        checksum = hashlib.sha256(self.export.read_bytes()).hexdigest()
        result = validate_export(str(self.export), str(self.export), "web-share", self.policy(
            source_checksum=checksum,
            expected_source_path=str(self.export),
            source_is_transformed=True,
        ))

        self.assertFalse(result["valid"])
        self.assertIn("transformed_jpeg_source", result["errors"])

    def test_accepts_an_original_jpeg_source_without_transform_provenance(self) -> None:
        original = self.root / "camera-original.jpg"
        make_jpeg(original)
        result = validate_export(str(self.export), str(original), "web-share", self.policy(
            source_checksum=hashlib.sha256(original.read_bytes()).hexdigest(),
            expected_source_path=str(original),
            source_is_transformed=False,
        ))

        self.assertTrue(result["valid"])
        self.assertNotIn("transformed_jpeg_source", result["errors"])

    def test_warns_for_semantic_artifacts(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(
            semantic_artifacts={"architecture": True, "faces": True, "text": True, "reflections": True, "removed_object_edges": True},
        ))

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [
            "semantic_artifact:architecture", "semantic_artifact:faces", "semantic_artifact:text",
            "semantic_artifact:reflections", "semantic_artifact:removed_object_edges",
        ])

    def test_reports_missing_metadata_without_inventing_it(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(required_fields=["make", "model", "software"]))

        self.assertTrue({"missing_metadata:make", "missing_metadata:model", "missing_metadata:software"}.issubset(result["warnings"]))
        self.assertIsNone(result["metadata"]["make"])

    def test_warns_when_icc_profile_is_wrong_or_missing(self) -> None:
        no_profile = self.root / "no-profile.jpg"
        Image.new("RGB", (2400, 1600), "navy").save(no_profile, "JPEG", quality=92)
        result = validate_export(str(no_profile), str(self.source), "web-share", self.policy())

        self.assertIn("wrong_color_profile", result["warnings"])

    def test_web_share_limits_dimensions_and_compression(self) -> None:
        oversized = self.root / "oversized.jpg"
        make_jpeg(oversized, size=(5000, 3333), quality=100)
        result = validate_export(str(oversized), str(self.source), "web-share", self.policy(
            result_snapshot_path=str(oversized), result_checksum=hashlib.sha256(oversized.read_bytes()).hexdigest(),
        ))

        self.assertIn("profile_dimensions", result["errors"])
        self.assertIn("profile_compression", result["warnings"])

    def test_competition_quality_requires_large_jpeg(self) -> None:
        tiny = self.root / "tiny.jpg"
        make_jpeg(tiny, size=(1000, 700))
        result = validate_export(str(tiny), str(self.source), "competition-quality", self.policy(
            result_snapshot_path=str(tiny), result_checksum=hashlib.sha256(tiny.read_bytes()).hexdigest(),
        ))

        self.assertIn("profile_dimensions", result["errors"])

    def test_print_master_requires_tiff_and_large_dimensions(self) -> None:
        result = validate_export(str(self.export), str(self.source), "print-master", self.policy())

        self.assertIn("profile_format", result["errors"])
        self.assertIn("profile_dimensions", result["errors"])

    def test_checksums_and_snapshot_link_are_verified(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(
            source_checksum="wrong", result_checksum="wrong", result_snapshot_path="/different/snapshot.jpg",
        ))

        self.assertIn("source_checksum_mismatch", result["errors"])
        self.assertIn("result_checksum_mismatch", result["errors"])
        self.assertIn("result_snapshot_mismatch", result["errors"])

    def test_checksums_and_snapshot_hash_are_required(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(
            source_checksum=None,
            result_checksum=None,
            result_snapshot_sha256=None,
        ))

        self.assertIn("missing_source_checksum", result["errors"])
        self.assertIn("missing_result_checksum", result["errors"])
        self.assertIn("missing_result_snapshot_sha256", result["errors"])

    def test_snapshot_hash_must_match_immutable_snapshot_content(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(
            result_snapshot_sha256="not-the-export-content",
        ))

        self.assertIn("result_snapshot_checksum_mismatch", result["errors"])

    def test_required_snapshot_and_xmp_markers_are_checked(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(
            result_snapshot_path=None,
            required_xmp_markers=["provenance:run-7"],
        ))

        self.assertIn("missing_result_snapshot_link", result["errors"])
        self.assertIn("missing_xmp_marker:provenance:run-7", result["warnings"])

    def test_source_path_mismatch_is_rejected(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(expected_source_path="/other/source.RAW"))

        self.assertIn("source_path_mismatch", result["errors"])

    def test_optional_exif_and_embedded_xmp_requirements_fail_closed(self) -> None:
        result = validate_export(str(self.export), str(self.source), "web-share", self.policy(
            require_exif=True,
            require_embedded_xmp=True,
        ))

        self.assertIn("missing_exif", result["errors"])
        self.assertIn("missing_embedded_xmp", result["errors"])


if __name__ == "__main__":
    unittest.main()
