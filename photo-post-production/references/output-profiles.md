# Output profiles and validation contract

## Delivery profiles

Use the exact profile names accepted by `validate_export(...)`:

| Profile | Required format | Long edge accepted by validator | Delivery intent |
| --- | --- | --- | --- |
| `web-share` | JPEG | 1200–4096 px | sRGB sharing copy; target JPEG quality 90–95 |
| `competition-quality` | JPEG | 3000–8000 px | Full-resolution sRGB judging/submission copy; quality up to 100 |
| `print-master` | TIFF | At least 3600 px; no validator maximum | High-bit-depth print/edit handoff |

Use profile-specific output directories and never overwrite a source or prior immutable snapshot. The Lightroom MCP export supports JPEG, PNG, TIFF, and original plus quality/width/height, but it does not prove ICC embedding or metadata compliance; validate those locally.

## Editable-master requirement

For every high-potential transformed photo, save a layered PSD or TIFF with non-destructive layers and mask data before delivery export. Record path, SHA-256, format, dimensions, bit depth, ICC profile, layer/mask IDs, source render hash, operation-manifest hash, and `editable: true`. If a healthy adapter cannot produce that evidence, keep the Lightroom result as `global-only` and do not claim a fully refined competition-standard result.

## Validation policy

Call:

```python
validate_export(path, source_path, profile, metadata_policy)
```

Populate `metadata_policy` with:

- `expected_source_path` and `source_checksum` from the sealed manifest;
- `result_snapshot_path` set to the same canonical export path passed as `path`, plus `result_checksum` and `result_snapshot_sha256` computed from those same finished bytes;
- `expected_icc_profile` (normally sRGB for JPEG delivery);
- `required_fields` and `required_xmp_markers` chosen by the project’s metadata/disclosure policy;
- `semantic_artifacts` findings for `architecture`, `faces`, `text`, `reflections`, and `removed_object_edges`;
- explicit source transformation status/provenance when the input is JPEG;
- for `print-master`, `editable_master` evidence containing the same local path/SHA-256, `editable: true`, `layered: true`, and non-empty layer and mask ID lists.

The validator treats the export itself as the immutable result snapshot: `result_snapshot_path` must resolve to the validated export path. It checks existence/readability, source identity, source/result/snapshot hashes, format, dimensions, bit depth, JPEG compression estimate, embedded ICC evidence, metadata via macOS `sips` with XMP fallback, required XMP markers, transformed-JPEG status, semantic-artifact flags, and print-master editable/layer/mask evidence. It reports unavailable metadata as missing; do not invent it.

## Release decision

1. Stop immediately when `valid` is false. Preserve the prior checkpoint and report every error.
2. Treat returned `release_blockers`—semantic artifacts, wrong ICC profile, missing required disclosure markers, and project-required metadata—as `valid: false` until corrected and revalidated. JPEG compression remains a nonblocking warning when no profile error is present.
3. After any correction, export to a new path, freeze that export as the new immutable snapshot, and recompute both result hashes; never overwrite the failed export/snapshot record.
4. Mark the photo completed only after delivery validation, editable-master validation when required, semantic/technical gates, and Lightroom restoration in export-only mode all pass.
5. Record final profile, dimensions, format/mode, quality estimate, ICC and metadata source, warnings, checksums, snapshot link, transformation disclosure, source lineage, operation manifest, final label, and stopping reason.

Do not confuse the `competition-quality` output profile with the `competition-standard` edit intent/variant. A profile-compliant JPEG is not automatically a competition-standard edit.
