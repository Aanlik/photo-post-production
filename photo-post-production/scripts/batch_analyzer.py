"""Batch-safe preview analysis and deterministic duplicate clustering."""

from __future__ import annotations

from image_metrics import analyze_preview


def analyze_paths(paths: list[str]) -> list[dict]:
    """Analyze paths independently so one unreadable preview cannot stop a batch."""
    records: list[dict] = []
    for path in paths:
        try:
            record = analyze_preview(path)
            record["state"] = "analyzed"
        except Exception as error:  # Pillow reports several source-specific exception types.
            record = {
                "path": path,
                "state": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
        records.append(record)
    return records


def _hash_similarity(left: str, right: str) -> float:
    try:
        left_value = int(left, 16)
        right_value = int(right, 16)
    except (TypeError, ValueError):
        return 0.0
    bit_count = max(len(left), len(right)) * 4
    if not bit_count:
        return 0.0
    differing_bits = bin(left_value ^ right_value).count("1")
    return 1.0 - (differing_bits / bit_count)


def cluster_near_duplicates(records: list[dict], threshold: float = 0.92) -> list[list[str]]:
    """Return connected clusters of readable previews at or above a hash threshold."""
    readable = [record for record in records if isinstance(record.get("path"), str) and isinstance(record.get("perceptual_hash"), str)]
    parents = list(range(len(readable)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(readable)):
        for right in range(left + 1, len(readable)):
            if _hash_similarity(readable[left]["perceptual_hash"], readable[right]["perceptual_hash"]) >= threshold:
                union(left, right)

    clusters: dict[int, list[str]] = {}
    for index, record in enumerate(readable):
        clusters.setdefault(find(index), []).append(record["path"])
    return [paths for paths in clusters.values() if len(paths) > 1]
