# Edit safety and review contract

## Non-negotiable gates

- Keep every original RAW and original-capture JPEG read-only. Write only to the run workspace, Lightroom catalog state, or a new output directory.
- Preserve `edit_authority: full` with `content_policy: user-authorized-transformative`. Use intent, director brief, locality, and budgets to control expression; do not silently replace authorization with a global “no transformations” rule.
- Reject failed technical gates, critical warnings, source/snapshot identity mismatches, locality violations, budget overruns, broken/cyclic graphs, duplicate or blank operation IDs, and missing transformative provenance.
- Route `score_confidence < 0.75`, uncertain masks, ambiguous source identity, and noncritical semantic warnings to review.
- Stop release when export validation returns `valid: false`. Retain the prior verified History/layer checkpoint when the Photoshop session is alive; otherwise retain the single bounded phase checkpoint and report the exact errors.

## Semantic quality gate

Inspect source and candidate at fit view and 100% detail. Check and record:

| Surface | Reject or review evidence |
| --- | --- |
| Architecture | Bent lines, duplicated windows, impossible joins, melted edges, broken perspective |
| Faces/bodies | Identity drift, anatomy errors, skin texture loss, halos, eye/teeth artifacts |
| Visible text/signage | Changed meaning, invented glyphs, malformed characters, partial duplication |
| Reflections/shadows | Missing causal object, inconsistent direction/color, impossible continuity |
| Removed-object edges | Repetition, seams, texture smears, halos, orphaned shadows/reflections |
| Global rendering | Clipping, crushed detail, banding, oversharpening, noise amplification, color-profile mismatch |

Treat architecture, faces, text, reflections, and removed-object edges as named `semantic_artifacts` in export validation. Mark severe identity/meaning/structure changes as critical; never accept them through a higher aesthetic score.

## Review-mode evidence

At each required pause, present a contact sheet that identifies the source, asset group, duplicate cluster, candidate, variant, and checkpoint. For each candidate include:

- fit-view source and result;
- 100% crops for critical regions and mask edges;
- before/after region views and editable-layer/mask status;
- Photoshop History/Snapshot checkpoint name and whether it is session-only or persisted;
- the three pre-render selection scores, final score, confidence, and score delta;
- technical gates, semantic findings, budget use, and warnings;
- transformation disclosure, operation IDs, model/version/prompt/mask provenance, and actual locality;
- output profile, dimensions, ICC/metadata result, and checksums.

Pause in review mode after culling, before high-risk or low-confidence operations, and before final release. In auto mode, create the same evidence but pause only for mandatory review conditions. A user rejection selects or creates a new graph branch; it does not overwrite a prior candidate.

## Labels and disclosure

- Use `global-only` whenever Photoshop or a required generative backend is not healthy, even if the Lightroom result is strong.
- Use competition-standard labeling only when the requested intent, technical and semantic gates, editable master, transformation disclosure, provenance, and output validation all pass and the result is not `global-only`.
- Distinguish an original capture from a transformed JPEG. Never feed a transformed JPEG into a later run as the source original.
- Preserve the best verified candidate when an iteration regresses. Record rollback target and stopping reason.
