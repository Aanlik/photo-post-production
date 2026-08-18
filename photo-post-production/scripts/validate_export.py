"""Validate local image exports without fabricating metadata or provenance."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import xml.etree.ElementTree as element_tree
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms


_PROFILES = {
    "web-share": {"formats": {"JPEG"}, "min_long_edge": 1200, "max_long_edge": 4096, "max_jpeg_quality": 95},
    "competition-quality": {"formats": {"JPEG"}, "min_long_edge": 3000, "max_long_edge": 8000, "max_jpeg_quality": 100},
    "print-master": {"formats": {"TIFF"}, "min_long_edge": 3600, "max_long_edge": None, "max_jpeg_quality": None},
}
_SEMANTIC_ARTIFACTS = ("architecture", "faces", "text", "reflections", "removed_object_edges")
_RELEASE_BLOCKING_WARNING_PREFIXES = (
    "wrong_color_profile",
    "missing_metadata:",
    "missing_xmp_marker:",
    "semantic_artifact:",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sips_metadata(path: Path) -> tuple[dict[str, str | None], bool]:
    metadata = {"profile": None, "make": None, "model": None, "software": None}
    executable = shutil.which("sips")
    if not executable:
        return metadata, False
    completed = subprocess.run(
        [executable, "-g", "profile", "-g", "make", "-g", "model", "-g", "software", str(path)],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if completed.returncode:
        return metadata, True
    for line in completed.stdout.splitlines():
        match = re.match(r"\s*(profile|make|model|software):\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            value = match.group(2)
            metadata[match.group(1).casefold()] = None if value.casefold() == "<nil>" else value
    return metadata, True


def _xmp_metadata(path: Path) -> dict[str, str | None]:
    metadata = {"make": None, "model": None, "software": None}
    candidates = [path.with_suffix(".xmp")]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            root = element_tree.fromstring(candidate.read_bytes())
        except (OSError, element_tree.ParseError):
            continue
        for element in root.iter():
            name = element.tag.rsplit("}", 1)[-1].casefold()
            if name in metadata and element.text and element.text.strip():
                metadata[name] = element.text.strip()
            for attribute, value in element.attrib.items():
                name = attribute.rsplit("}", 1)[-1].casefold()
                if name in metadata and value.strip():
                    metadata[name] = value.strip()
    return metadata


def _jpeg_quality(image: Image.Image) -> int | None:
    tables = getattr(image, "quantization", None)
    if not tables:
        return None
    values = [value for table in tables.values() for value in table]
    if not values:
        return None
    if max(values) <= 1:
        return 100
    # Standard JPEG encoders scale the luminance table approximately linearly.
    average = sum(values) / len(values)
    return max(1, min(99, round(100 - (average - 1) * 2.2)))


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def _xmp_markers(path: Path) -> str:
    """Read an adjacent XMP sidecar only; an absent marker is never guessed."""
    sidecar = path.with_suffix(".xmp")
    if not sidecar.is_file():
        return ""


def _embedded_xmp(path: Path) -> bool:
    """Detect an embedded JPEG XMP packet without treating arbitrary text as XMP."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return b"http://ns.adobe.com/xap/1.0/" in data or b"<x:xmpmeta" in data


def _embedded_icc(path: Path) -> bool:
    try:
        return b"ICC_PROFILE" in path.read_bytes()
    except OSError:
        return False


def _icc_profile_name(icc: bytes | None) -> str | None:
    if not icc:
        return None
    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.getProfileDescription(profile).strip() or ImageCms.getProfileName(profile).strip()
    except Exception:
        return None
    try:
        return sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _source_is_transformed(policy: dict) -> bool:
    if policy.get("source_is_transformed") is True:
        return True
    level = policy.get("source_transformation_level", policy.get("transformation_level"))
    if level not in (None, "", "none", "original", 0):
        return True
    provenance = policy.get("source_provenance")
    return isinstance(provenance, dict) and (
        provenance.get("transformed") is True or bool(provenance.get("operation_records"))
    )


def _bit_depth(image: Image.Image) -> int | None:
    tags = getattr(image, "tag_v2", None)
    bits = tags.get(258) if tags is not None else None
    if isinstance(bits, int):
        return bits
    if isinstance(bits, (tuple, list)) and bits and all(isinstance(value, int) for value in bits):
        return min(bits)
    if image.mode in {"I;16", "I;16B", "I;16L"}:
        return 16
    if image.mode in {"1"}:
        return 1
    if image.mode in {"L", "P", "RGB", "RGBA", "CMYK", "LAB"}:
        return 8
    return None


def validate_export(path: str, source_path: str, profile: str, metadata_policy: dict) -> dict:
    """Validate a local result against its source, profile, and metadata policy."""
    policy = metadata_policy if isinstance(metadata_policy, dict) else {}
    errors: list[str] = []
    warnings: list[str] = []
    export = Path(path).expanduser()
    source = Path(source_path).expanduser()
    metadata = {"profile": None, "make": None, "model": None, "software": None}
    result: dict[str, Any] = {
        "path": str(export), "source_path": str(source), "profile": profile,
        "errors": errors, "warnings": warnings, "release_blockers": [],
        "metadata": metadata, "valid": False,
    }
    if profile not in _PROFILES:
        errors.append("unsupported_profile")
        return result
    if not export.is_file():
        errors.append("export_not_found")
        return result
    if not source.is_file():
        errors.append("source_not_found")
        return result
    if source.suffix.casefold() in {".jpg", ".jpeg"} and _source_is_transformed(policy):
        errors.append("transformed_jpeg_source")
    expected_source = policy.get("expected_source_path")
    if expected_source and not _same_path(source, expected_source):
        errors.append("source_path_mismatch")
    snapshot = policy.get("result_snapshot_path")
    if not snapshot:
        errors.append("missing_result_snapshot_link")
    elif not _same_path(export, snapshot):
        errors.append("result_snapshot_mismatch")
    snapshot_checksum = policy.get("result_snapshot_sha256")
    if not snapshot_checksum:
        errors.append("missing_result_snapshot_sha256")
    elif snapshot and Path(snapshot).expanduser().is_file():
        snapshot_content_checksum = _sha256(Path(snapshot).expanduser())
        if snapshot_content_checksum != snapshot_checksum:
            errors.append("result_snapshot_checksum_mismatch")
        if snapshot_content_checksum != _sha256(export):
            errors.append("result_snapshot_content_mismatch")
    elif snapshot:
        errors.append("result_snapshot_not_found")
    source_checksum = policy.get("source_checksum")
    if not source_checksum:
        errors.append("missing_source_checksum")
    elif _sha256(source) != source_checksum:
        errors.append("source_checksum_mismatch")
    result_checksum = policy.get("result_checksum")
    if not result_checksum:
        errors.append("missing_result_checksum")
    elif _sha256(export) != result_checksum:
        errors.append("result_checksum_mismatch")
    try:
        with Image.open(export) as image:
            image.load()
            image_format = image.format
            width, height = image.size
            quality = _jpeg_quality(image) if image_format == "JPEG" else None
            icc = image.info.get("icc_profile")
            exif = image.getexif()
            exif_present = bool(exif)
            bit_depth = _bit_depth(image)
    except OSError:
        errors.append("unreadable_export")
        return result
    export_checksum = _sha256(export)
    result.update({
        "format": image_format, "mode": image.mode, "dimensions": [width, height],
        "bit_depth": bit_depth, "jpeg_quality_estimate": quality,
        "checksum": export_checksum,
        "exif_present": exif_present,
        "icc_embedded": _embedded_icc(export),
        "embedded_xmp": _embedded_xmp(export),
    })
    rules = _PROFILES[profile]
    if image_format not in rules["formats"]:
        errors.append("profile_format")
    long_edge = max(width, height)
    if long_edge < rules["min_long_edge"] or (rules["max_long_edge"] is not None and long_edge > rules["max_long_edge"]):
        errors.append("profile_dimensions")
    if rules["max_jpeg_quality"] is not None and quality is not None and quality > rules["max_jpeg_quality"]:
        warnings.append("profile_compression")
    if profile == "print-master":
        if bit_depth is None or bit_depth < 16:
            errors.append("print_master_bit_depth")
        master = policy.get("editable_master")
        if not isinstance(master, dict):
            errors.extend([
                "missing_editable_master_evidence",
                "print_master_not_editable",
                "print_master_not_layered",
                "print_master_missing_layers",
                "print_master_missing_masks",
            ])
        else:
            if master.get("editable") is not True:
                errors.append("print_master_not_editable")
            if master.get("layered") is not True:
                errors.append("print_master_not_layered")
            if not isinstance(master.get("layer_ids"), list) or not master["layer_ids"] or any(not isinstance(item, str) or not item.strip() for item in master["layer_ids"]):
                errors.append("print_master_missing_layers")
            if not isinstance(master.get("mask_ids"), list) or not master["mask_ids"] or any(not isinstance(item, str) or not item.strip() for item in master["mask_ids"]):
                errors.append("print_master_missing_masks")
            master_path = master.get("path")
            if not isinstance(master_path, str) or not _same_path(export, master_path):
                errors.append("print_master_path_mismatch")
            if master.get("sha256") != export_checksum:
                errors.append("print_master_sha256_mismatch")
    sips, sips_available = _sips_metadata(export)
    metadata.update(sips)
    # sips can report the system-default profile for untagged JPEGs.  Pillow's
    # embedded ICC bytes are the honest source for an export profile.
    metadata["profile"] = None
    if icc:
        metadata["profile"] = _icc_profile_name(icc)
        if not metadata["profile"]:
            try:
                metadata["profile"] = icc.decode("latin-1", "replace")
            except AttributeError:
                metadata["profile"] = str(icc)
    elif result["icc_embedded"] and sips.get("profile"):
        # Some Photoshop JPEGs carry an ICC marker that Pillow cannot expose
        # as an ICC byte string; macOS sips can still identify that embedded
        # profile. An untagged JPEG is never accepted from sips alone.
        metadata["profile"] = sips["profile"]
    xmp = _xmp_metadata(export)
    for field in ("make", "model", "software"):
        if not metadata[field]:
            metadata[field] = xmp[field]
    result["metadata_source"] = "sips" if sips_available else "xmp-fallback"
    expected_profile = policy.get("expected_icc_profile")
    if expected_profile and (
        not result["icc_embedded"]
        or not metadata["profile"]
        or str(expected_profile).casefold() not in str(metadata["profile"]).casefold()
    ):
        warnings.append("wrong_color_profile")
    required_fields = policy.get("required_fields", [])
    if isinstance(required_fields, (str, bytes)):
        required_fields = [required_fields]
    for field in required_fields if isinstance(required_fields, list) else []:
        if field not in metadata or not metadata[field]:
            warnings.append(f"missing_metadata:{field}")
    if policy.get("require_exif") is True and not exif_present:
        errors.append("missing_exif")
    if policy.get("require_embedded_xmp") is True and not result["embedded_xmp"]:
        errors.append("missing_embedded_xmp")
    markers = policy.get("required_xmp_markers", [])
    if isinstance(markers, str):
        markers = [markers]
    available_markers = _xmp_markers(export)
    for marker in markers if isinstance(markers, list) else []:
        if isinstance(marker, str) and marker not in available_markers:
            warnings.append(f"missing_xmp_marker:{marker}")
    artifacts = policy.get("semantic_artifacts", {})
    if isinstance(artifacts, dict):
        for name in _SEMANTIC_ARTIFACTS:
            if artifacts.get(name):
                warnings.append(f"semantic_artifact:{name}")
    release_blockers = [
        warning for warning in warnings
        if any(warning == prefix or warning.startswith(prefix) for prefix in _RELEASE_BLOCKING_WARNING_PREFIXES)
    ]
    result["release_blockers"] = release_blockers
    result["valid"] = not errors and not release_blockers
    return result
