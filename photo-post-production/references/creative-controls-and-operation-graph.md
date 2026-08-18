# Creative controls and reversible operation graph

## Director controls

Set the intent first, then write numeric controls independently:

- `creative_intensity` controls how visibly the result may depart from a neutral rendering.
- `source_fidelity` controls how strongly original scene structure, moment, and appearance rank against alternatives.
- `photographer_intent` records the visual or narrative reason for the capture; use it to rank candidates and protect significant details.

Do not derive one control as the inverse of the other. High values for both can request a polished but structurally faithful result. Full edit authority grants access to the transformation vocabulary; it does not require every operation.

## Intent and budget contract

Select one intent from the schema. Materialize an explicit finite numeric `adjustment_budget` before candidate generation. At minimum include limits for global adjustments, local adjustments, transformative operations, exposure delta, crop fraction, temperature delta, sharpening, geometry delta, and candidate count. Add operation-specific limits when the brief allows removal, addition, expansion, replacement, reshaping, relighting, or reconstruction.

Use these priorities:

| Intent | Default priority | Budget posture |
| --- | --- | --- |
| `documentary-truthful` | Moment and factual scene fidelity | Conservative; transformations only when explicitly supported by the brief and disclosed |
| `natural-enhancement` | Plausible light, color, and composition | Restrained local shaping; minimal visible transformation |
| `editorial-expression` | Clear authored mood and hierarchy | Broader color, crop, relight, and disclosed transformation budget |
| `competition-standard` | Strong visual resolution plus strict technical/semantic quality | Permit decisive work, but require editable master and complete evidence |
| `commercial/creative` | Brief satisfaction and controlled construction | Broadest brief-authorized operation budget and explicit disclosure |

The sealed run budget is the ceiling. Reject any candidate-supplied replacement or expansion.

## Region contract

For each planned region record `id`, `label`, `mask_type` (`semantic` or `geometry`), `purpose`, numeric `adjustments`, confidence, and `forbidden_changes`. Reference a stored mask artifact by path and hash when an operation executes. Split regions when purposes, risk, or forbidden changes differ.

## Graph contract

Represent every transformative operation as a node containing:

`operation_id`, `type`, `depends_on`, `backend`, `reason`, `affected_region`, `parameters`, `risk`, `checkpoint`, `generative`, `input_layer`, and `output_layer`.

Use only the schema vocabulary: `remove-element`, `add-element`, `generative-fill`, `generative-expand`, `replace-sky-or-background`, `reshape-geometry`, `large-crop`, `relight-subject`, and `style-reconstruct`.

Enforce these invariants:

1. Generate a stable idempotent ID from run, asset, candidate branch, and logical operation identity—not from retry count.
2. Depend only on nodes in the same candidate graph. Reject missing dependencies, self-dependencies, and cycles.
3. Keep each candidate branch isolated; never reuse an output layer from another branch as an implicit input.
4. Checkpoint before medium/high-risk operations and after every verified render.
5. Preserve prior layers and masks so each node can be disabled or its branch abandoned.
6. Add one matching operation record for every transformative or generative node. Include before/after paths and SHA-256 values, model/version, software, exact prompt, and mask path/hash even when the model is a non-generative software engine.

Validate the complete object with `validate_edit_plan(...)` before execution and revalidate the operation manifest before candidate scoring.

## Candidate exploration

Choose one materially distinct direction before execution. Use named directions such as `natural`, `editorial`, and `competition-standard`; if alternate looks are useful, retain them as layer groups inside the same master PSD rather than creating separate PSD/TIFF branches. Compare the selected direction against the same source snapshot and director brief, and stop before damage to structure or detail.
