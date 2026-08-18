"""Deterministic grouping of locally named capture, sidecar, and export assets."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


_RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".dng", ".nef", ".orf", ".raf", ".rw2"}
_BRACKET_SUFFIX = re.compile(r"(?:[_-](?:[+-]?\d+(?:\.\d+)?ev|ev[+-]?\d+))$", re.IGNORECASE)
_PANORAMA_SUFFIX = re.compile(r"(?:[_-](?:pano(?:rama)?)(?:[_-]?\d+)?)$", re.IGNORECASE)
_EXPORT_SUFFIX = re.compile(
    r"(?:[_ -](?:edit(?:ed)?|final|export(?:ed)?|copy|processed|retouched|version|v\d+))(?:[_ -]?\d+)?$",
    re.IGNORECASE,
)
_NUMBERED_CAPTURE = re.compile(r"^(.*?)(\d+)$")


def _strip_relation_suffixes(stem: str) -> tuple[str, str | None]:
    for relationship, pattern in (("bracket", _BRACKET_SUFFIX), ("panorama", _PANORAMA_SUFFIX)):
        match = pattern.search(stem)
        if match:
            return stem[:match.start()], relationship
    cleaned = stem
    changed = False
    while (match := _EXPORT_SUFFIX.search(cleaned)) is not None:
        cleaned = cleaned[:match.start()]
        changed = True
    return cleaned, "historical-export" if changed else None


def _asset_info(path: str, index: int) -> dict:
    parsed = Path(path)
    family_stem, marker = _strip_relation_suffixes(parsed.stem)
    return {
        "path": path,
        "index": index,
        "directory": str(parsed.parent).casefold(),
        "extension": parsed.suffix.casefold(),
        "family": family_stem.casefold(),
        "marker": marker,
    }


def _primary_path(members: list[dict]) -> str:
    raw = next((member["path"] for member in members if member["extension"] in _RAW_EXTENSIONS), None)
    return raw or next((member["path"] for member in members if member["extension"] != ".xmp"), members[0]["path"])


def _asset_group_id(paths: list[str], source_hashes: dict[str, str]) -> str:
    identities = []
    for path in sorted(paths, key=lambda value: str(Path(value).expanduser().resolve()).casefold()):
        canonical = str(Path(path).expanduser().resolve())
        identities.append({"path": canonical, "sha256": source_hashes.get(path, source_hashes.get(canonical, ""))})
    payload = json.dumps(identities, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "ag-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _group(relationship: str, members: list[dict], source_hashes: dict[str, str]) -> dict:
    paths = [member["path"] for member in sorted(members, key=lambda member: member["index"])]
    return {
        "asset_group_id": _asset_group_id(paths, source_hashes),
        "relationship": relationship,
        "paths": paths,
        "members": paths,
        "primary_path": _primary_path(members),
    }


def group_related_assets(paths: list[str], source_hashes: dict[str, str] | None = None) -> list[dict]:
    """Associate related files and emit a deterministic path/hash group identity."""
    hashes = source_hashes if isinstance(source_hashes, dict) else {}
    infos = [_asset_info(path, index) for index, path in enumerate(paths)]
    by_family: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for info in infos:
        by_family[(info["directory"], info["family"])].append(info)

    groups: list[tuple[int, dict]] = []
    assigned: set[int] = set()
    for members in by_family.values():
        markers = {member["marker"] for member in members if member["marker"]}
        extensions = {member["extension"] for member in members}
        if "historical-export" in markers:
            relationship = "historical-export"
        elif "bracket" in markers:
            relationship = "bracket"
        elif "panorama" in markers:
            relationship = "panorama"
        elif len(members) > 1 and (len(extensions) > 1 or ".xmp" in extensions):
            relationship = "sidecars"
        else:
            continue
        groups.append((min(member["index"] for member in members), _group(relationship, members, hashes)))
        assigned.update(member["index"] for member in members)

    burst_candidates: dict[tuple[str, str, int], list[tuple[int, dict]]] = defaultdict(list)
    for info in infos:
        if info["index"] in assigned:
            continue
        match = _NUMBERED_CAPTURE.match(info["family"])
        if match:
            burst_candidates[(info["directory"], match.group(1), len(match.group(2)))].append((int(match.group(2)), info))

    for candidates in burst_candidates.values():
        candidates.sort(key=lambda item: item[0])
        run: list[dict] = []
        previous: int | None = None
        for number, info in candidates + [(None, None)]:
            if info is not None and (previous is None or number == previous + 1):
                run.append(info)
            else:
                if len(run) > 1:
                    groups.append((min(member["index"] for member in run), _group("burst", run, hashes)))
                    assigned.update(member["index"] for member in run)
                run = [] if info is None else [info]
            previous = number

    for info in infos:
        if info["index"] not in assigned:
            groups.append((info["index"], _group("single", [info], hashes)))
    return [group for _, group in sorted(groups, key=lambda item: item[0])]
