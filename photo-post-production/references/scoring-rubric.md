# Scoring and edit-plan contract

## Score records

Scores are numeric values from 0 through 100. Keep `keep_value`, `editability`,
`expected_gain`, and `candidate_potential` distinct from post-render
`final_score`. Before rendering, report the candidate estimate using:

`candidate_potential = keep_value × 0.65 + editability × 0.20 + expected_gain × 0.15`

The estimate is binding: runtime validation recomputes it and permits an
absolute deterministic rounding tolerance of `0.01`. A `selected` record must
also meet the default automatic-selection threshold of
`candidate_potential >= 75`. Every record includes a non-empty score version,
evidence, and confidence.

### Explain every score during review

The review output must not show a bare number. Derive and display the three
weighted contributions:

```text
保留价值 × 65% + 可编辑性 × 20% + 预期收益 × 15% = 候选潜力
```

For each component, explain its meaning in plain language, show the numeric
contribution, and state what evidence supports it. Also show:

- strengths and risks already present in the score record;
- score confidence and why the record is or is not eligible for automatic selection;
- a bounded post-edit outlook with likely operations, expected visual change,
  and limitations;
- a recommended disposition such as `进入精修候选` or `暂缓`.

The post-edit outlook is a review aid, not a promise that a finished image will
reach a target score. Do not claim recovery of clipped detail, focus, anatomy,
or structure that the available evidence cannot support. Before a user approves
editing, inspect the RAW at full resolution and show the actual before/after
candidate, 100% critical regions, masks, warnings, and provenance.

Scores below `0.75` confidence are review-only: their decision must be
`review`. A `fail` technical gate likewise permits only `review` or `rejected`;
it is never auto-accepted by a high composite score.

Primary categories are `landscape-nature`, `urban-landscape`,
`architecture-urban-space`, `street-documentary`, `portrait-environmental`,
`animal-wildlife`, and `other-unsupported`. Gate states are `pass`, `warn`, and
`fail`.

## Director briefs and edit plans

Every edit plan embeds a director brief with project goal, subject priority,
target use, mood, photographer intent, `creative_intensity`, `source_fidelity`,
transformation disclosure, and allowed operations. The two creative controls
are independent 0–100 values: they steer operation magnitude and ranking, not
whether full-authority transformations are allowed.

Supported intents are `documentary-truthful`, `natural-enhancement`,
`editorial-expression`, `competition-standard`, and `commercial/creative`.
Under `edit_authority: full`, the required `content_policy` is exactly
`user-authorized-transformative`. This authorizes, but does not hide,
transformative operations.

Each region needs an ID, semantic or geometry mask, purpose, adjustment map,
confidence, and forbidden changes. A plan must define an adjustment budget.

## Operation provenance

The operation graph is reversible: every operation has a unique ID and only
depends on another operation in the same graph. Self-dependencies and cycles
are invalid. Operation types are limited to
`remove-element`, `add-element`, `generative-fill`, `generative-expand`,
`replace-sky-or-background`, `reshape-geometry`, `large-crop`,
`relight-subject`, and `style-reconstruct`.

Every transformative operation, and every operation marked `generative`, needs
an operation record with before/after paths and hashes, model/version, software, prompt, and mask path/hash
reference. Records are unique and cannot refer to an undeclared operation.
Named variants are `natural`, `editorial`, and
`competition-standard`.
