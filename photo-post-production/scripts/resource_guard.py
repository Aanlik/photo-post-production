"""Estimate peak disk usage before starting a full-resolution Adobe job."""

from __future__ import annotations

from typing import Any


def estimate_peak_disk_bytes(
    source_bytes: int,
    width: int,
    height: int,
    bit_depth: int = 16,
    photoshop_layers: int = 8,
    keep_working_tiff: bool = True,
) -> int:
    channels = 3
    bytes_per_channel = 2 if int(bit_depth) > 8 else 1
    pixels = max(1, int(width)) * max(1, int(height))
    raster_bytes = pixels * channels * bytes_per_channel
    # PSD layers are compressed in practice, but the guard intentionally uses
    # a conservative working-set estimate so a RAW batch cannot explode disk.
    psd_working_set = raster_bytes * max(2, min(32, int(photoshop_layers)))
    tiff_bytes = raster_bytes if keep_working_tiff else 0
    preview_and_metadata = max(25_000_000, int(source_bytes) // 2)
    return int(max(0, source_bytes) + psd_working_set + tiff_bytes + preview_and_metadata)


def check_disk_budget(current_bytes: int, projected_peak_bytes: int, budget_bytes: int) -> dict[str, Any]:
    projected_total = max(0, int(current_bytes)) + max(0, int(projected_peak_bytes))
    allowed = int(budget_bytes) <= 0 or projected_total <= int(budget_bytes)
    return {
        "allowed": allowed,
        "reason": "within_disk_budget" if allowed else "projected_disk_budget_exceeded",
        "current_bytes": max(0, int(current_bytes)),
        "projected_peak_bytes": max(0, int(projected_peak_bytes)),
        "projected_total_bytes": projected_total,
        "budget_bytes": max(0, int(budget_bytes)),
    }
