# Photo Post-Production Skill Design

**Date:** 2026-08-14  
**Status:** Reviewed; local implementation integrated and live Adobe path verified on 2026-08-15
**Scope:** Local Codex Skill for RAW photo selection, artistic post-production, feedback memory, and high-fidelity export.

**Implementation status (2026-08-15):** Local analysis, category-weighted scoring, animal classification, asset grouping, project calibration, series anchors, operation graphs, adapter protocol, resumable queue, quality gates, semantic checks, export contracts, candidate comparison, chat-window image_gen result checkpoints, post-render pixel scoring, bounded three-iteration retries, multi-profile export evidence, Photoshop tool coverage, and repeated feedback-to-parameter guidance are integrated into the main pipeline. The installed local adapters have been exercised against Lightroom MCP 0.9.0 and Photoshop Bridge 0.2.0 with a real RAW: Lightroom catalog lookup/state restore passed, Photoshop produced layered PSD and validated sRGB JPEG profiles, and the report/queue synchronization path passed. Unavailable tools, unhealthy generation, or failed quality gates still leave `paused`/`global-only`/rejected evidence rather than claiming a finished image.

## Goal

Create a local Skill that reads a mixed photo folder, scores photographs for both photographic quality and post-production potential, processes only the strongest images, and exports shareable or competition-standard results without overwriting original files.

“Shareable” and “competition-standard” describe finished image quality, not actual contest eligibility and not merely JPEG settings. The Skill must judge whether the result has convincing composition, exposure, visual hierarchy, color intent, detail, and photographic character. With the user’s full authority, it may make large crops, substantial exposure and color changes, local corrections, remove or add elements, and use generative reconstruction, while still rejecting images whose underlying moment or composition cannot become a credible work through editing.

### Post-production brief and creative controls

Start every run with a short director brief covering project goal, subject priority, target use, mood, photographer intent, allowed transformation types, and expected outputs. Convert natural-language constraints into structured parameters and show the assumptions being used before execution. The user may add why the photograph was made or what feeling must remain; treat this as a current-project constraint.

In addition to editing intent, record two independent 0–100 parameters:

- `creative_intensity`: how strongly the result may depart from the source through stylization and reconstruction;
- `source_fidelity`: how strongly the result should preserve the source scene structure, lighting logic, and content relationships.

These are not simple prohibitions. Even at high `source_fidelity`, the user has authorized generative operations; the value only influences operation choice, magnitude, and candidate ranking. Also record `subject_priority`, `target_use`, `transformation_disclosure`, and `allowed_operations`.

### Batch calibration

When the user has no stable project style or the current batch differs materially from historical material, first select 3–5 representative photos across categories and asset groups for a small calibration run. Generate a few candidate directions and summarize the editing assumptions. The user may approve, modify, or skip calibration; auto mode may continue with the current default style but must record that calibration was skipped. Calibration applies to the current project unless explicitly saved as a long-term style.

## Non-goals

- Do not silently invent, add, or remove meaningful photographic content. User-authorized generative and transformative edits are allowed, but every such change must be recorded in the report and operation manifest.
- Do not imitate a named photographer’s exact signature or copy a reference image. Convert references into high-level attributes such as contrast structure, color temperature, density, and mood.
- Do not overwrite RAW files or silently replace existing exports.
- Treat “competition-standard” as an internal quality target for strong photographic finish, not as a claim of eligibility for a real competition; no contest-rule lookup is required for this label.
- Do not require cloud storage for style memory, run history, or feedback.

## Delivery phases

To prevent scope creep, deliver in three phases: Phase 1 implements classification, scoring, asset grouping, Lightroom global edits, quality validation, and high-quality export; Phase 2 adds Photoshop local retouching, generative operations, operation graphs, and multi-candidate exploration; Phase 3 adds style learning, batch calibration, queue optimization, and locked regression testing. Later phases must not block a usable Phase 1.

## Architecture

The Skill has three execution layers and one local memory layer.

### 1. AI post-production director

The Skill analyzes previews and metadata, classifies the image as landscape, urban, street/documentary, portrait, or mixed, and produces a structured edit plan. It scores:

- technical condition: focus, motion blur, noise, exposure latitude, highlight recovery;
- composition: subject placement, balance, crop potential, visual flow;
- photographic value: decisive moment, atmosphere, narrative, originality, and subject clarity;
- post-production potential: whether crop, tonal work, color work, or local shaping can materially improve the image;
- final-result confidence: whether the proposed edit is likely to produce a credible shareable image.

Scores are 0–100 with reasons and warnings. Near-duplicates are clustered so a burst does not fill the selected set. A hard rejection is used for severe blur, unusable exposure, duplicate frames, or a weak image whose editability score is below the configured threshold.

Before model judgment, a deterministic local analyzer measures image dimensions, histogram and clipping, blur/focus proxies, noise, color cast, duplicate similarity, and available EXIF/camera-profile data. The AI then combines those measurements with visual and narrative judgment. A deterministic failure must not be overridden by a high aesthetic score without an explicit warning; automatic mode conservatively skips or waits on low-confidence records, while only explicit `review` mode waits for a human decision.

### Classification and scoring standard

Classification is multi-label. The Skill selects one primary category, optional secondary tags, and a confidence score:

- **Landscape/nature:** mountains, coast, forests, sky, weather, and natural light;
- **Urban landscape/cityscape:** skyline, waterfront, streets as spatial scenes, and night city views;
- **Architecture/urban space:** geometry, facades, structures, lines, and designed spaces;
- **Street/documentary:** people, events, daily life, social context, and decisive moments;
- **Portrait/environmental portrait:** a person is the primary subject, with expression, gesture, and relationship to place;
- **Animal/wildlife:** mammals, birds, pets, and wildlife, with a dedicated category and treatment strategy;
- **Other/unsupported:** macro, product, or specialist genres that require a dedicated profile and lack enough evidence.

Cross-cutting tags include `night/low-light`, `backlight`, `motion`, `rain/reflection`, `crowd`, `minimal`, and `high-contrast`. The category is selected from visual evidence and EXIF context; a low-confidence classification must lower automation confidence, and automatic mode must conservatively skip or wait while review mode can route the image to a human queue.

The Skill must keep three decision scores separate instead of hiding everything inside one number:

- **Keep value:** whether the original photograph is worth retaining regardless of editing;
- **Editability:** whether responsible post-production can materially improve it;
- **Candidate potential:** the predicted quality after the proposed edit.

Before rendering, candidate potential is estimated as:

`candidate_potential = keep_value × 0.65 + editability × 0.20 + expected_gain × 0.15`

After rendering, the quality gate produces an independent **final score** from the actual image. The Skill must report all four values and never use editability alone to justify selecting a weak photograph.

Photographic value is composed of five dimensions, each scored 0–100:

- **Technical quality — 20%:** focus or intentional motion, exposure latitude, noise, color artifacts, and lens/rendering condition;
- **Composition — 25%:** subject clarity, balance, geometry, depth, visual flow, and crop potential;
- **Light and color — 20%:** quality and direction of light, tonal separation, color relationships, and atmosphere;
- **Moment/story/originality — 20%:** decisive moment, gesture, narrative context, emotional or conceptual force, and distinctiveness;
- **Photographic coherence — 15%:** whether the frame already communicates a deliberate point of view rather than merely recording a scene.

Category-specific weighting adjusts the general rubric without changing its meaning:

| Category | Technical | Composition | Light/color | Moment/story | Coherence |
|---|---:|---:|---:|---:|---:|
| Landscape/nature | 20 | 30 | 25 | 10 | 15 |
| Urban landscape/cityscape | 20 | 30 | 25 | 10 | 15 |
| Architecture/urban space | 20 | 35 | 25 | 5 | 15 |
| Street/documentary | 20 | 20 | 15 | 35 | 15 |
| Portrait/environmental portrait | 25 | 20 | 20 | 20 | 20 |
| Other/unsupported | 20 | 25 | 20 | 20 | 15 |

Editability is scored separately on RAW latitude, recoverability of important highlights/shadows, usefulness of crop and perspective changes, and expected benefit from global or local adjustments. It must not rescue a photograph whose subject, moment, or composition is fundamentally weak.

`expected_gain` estimates the likely improvement from the proposed edit and is based on visible defects that are correctable, not on the hope that stronger sliders will make the image better.

Decision bands:

- **90–100:** exceptional; process and consider for competition review;
- **80–89:** strong; process as a shareable or competition-standard candidate depending on quality-gate results;
- **70–79:** usable; process only when editability is high or the user requests broader coverage;
- **60–69:** borderline; place in review, do not auto-process by default;
- **0–59:** reject unless the user explicitly asks to process it.

Default selection is `candidate_potential >= 75`, with the top candidates selected per category and near-duplicate cluster. The Skill must keep a small quota of high-confidence alternatives so the result is not dominated by one visual type.

Critical rejection flags are severe focus failure on the intended subject, unrecoverable clipping over important content, accidental obstruction, exact/near duplicate with a clearly stronger frame, or no identifiable subject/intent. Intentional motion blur, silhouette, grain, or underexposure must not be rejected automatically when they support the category and visual intent.

### Score confidence and technical gates

Every score must include a score version, evidence sources, and confidence. Below the current confidence threshold, automatic mode may only conservatively skip or wait and cannot auto-process merely because its composite score is high; explicit review mode may route the image to a human queue. Objective photographic quality, personal preference, and style fit remain separate; style fit may affect ranking but cannot override a technical gate or promote a rejected image.

Auto mode runs technical gates before aesthetic and style ranking. Gates cover source readability, irrecoverable defects, output artifacts, original-path write protection, and metadata validation. Each gate returns `pass`, `warn`, or `fail` with a readable reason. A `fail` cannot be offset by a composite score, and a `warn` must be shown in review or the final report.

AI scores are ranking and selection signals, not objective aesthetic truth. Reports must also show score confidence, style fit, performance against annotated human fixtures, and historical user correction rate; a high score alone must not claim universal artistic value or guaranteed professional quality.

Every score record must contain:

```json
{
  "primary_category": "urban-landscape",
  "secondary_tags": ["night", "reflection"],
  "classification_confidence": 0.91,
  "score_version": "1.1",
  "evidence": ["histogram", "duplicate-cluster", "visual-review"],
  "score_confidence": 0.84,
  "style_fit": 72,
  "technical_gates": {"source_readable": "pass", "irrecoverable_defect": "pass"},
  "technical": 78,
  "composition": 84,
  "light_color": 80,
  "moment_story": 62,
  "coherence": 82,
  "photographic_value": 78,
  "editability": 88,
  "expected_gain": 14,
  "keep_value": 78,
  "candidate_potential": 81,
  "final_score": null,
  "decision": "selected",
  "strengths": ["central tower", "layered reflections"],
  "risks": ["bright signs", "dark lower buildings"],
  "recommended_treatment": ["recover shadows", "control signage highlights"]
}
```

### Region-level edit plan

Fine post-production is expressed as a region-aware plan, not only as global sliders. Each region has a semantic label or geometry, an intended visual purpose, allowed adjustments, confidence, and forbidden changes:

```json
{
  "regions": [
    {
      "id": "main-building",
      "label": "subject architecture",
      "mask_type": "semantic",
      "purpose": "make the central tower readable without flattening the night",
      "adjustments": {"exposure": 0.25, "texture": 12, "clarity": 8},
      "confidence": 0.86,
      "forbidden_changes": ["do not alter building geometry"]
    },
    {
      "id": "bright-signs",
      "label": "signage highlights",
      "mask_type": "semantic",
      "purpose": "reduce distracting brightness",
      "adjustments": {"highlights": -35, "saturation": -8},
      "confidence": 0.82,
      "forbidden_changes": ["do not erase signs by default"]
    }
  ]
}
```

Lightroom handles global operations and simple gradients. Photoshop handles semantic masks, local dodge/burn, selective color, cleanup, and complex geometry. If a requested region cannot be identified with sufficient confidence, the Skill must either ask for review or skip that local edit; it must not apply a blind adjustment to the whole frame.

### Fine-edit safety budgets and change detection

Each editing intent defines an adjustment budget. The user has authorized `edit_authority=full`, so the Skill may use large crops, remove or add elements, reshape geometry, and use generative operations. Intent determines whether the result should favor restraint, natural enhancement, or strong expression rather than disabling those capabilities. Natural enhancement prefers the smallest effective change, editorial expression and competition-standard targets may use a wider transformation budget, and commercial/creative allows the broadest freedom. Generative operations are not silently prohibited, but must be separately marked in the run manifest with what changed and why it serves the target.

Every local operation records its type, parameters, mask bounds, input layer, output layer, and whether it is generative. After rendering, automatically check mask edges, halos, color spill, texture loss, over-smoothed skin, building distortion, and local noise, and compare before/after differences at appropriate zoom levels. On failure, skip the operation or route it to review; do not deliver an untrusted local result as a fully refined image.

### Reversible post-production graph

Represent the edit plan as a dependency graph rather than one monolithic prompt. Each operation includes `operation_id`, `depends_on`, backend, reason, affected region, parameters, risk, checkpoint, generative flag, input layer, and output layer. The graph supports single-step reruns, undo, backend replacement, and resume from an intermediate checkpoint.

Supported operations include `remove-element`, `add-element`, `generative-fill`, `generative-expand`, `replace-sky-or-background`, `reshape-geometry`, `large-crop`, `relight-subject`, and `style-reconstruct`. These are normal post-production capabilities rather than automatic failures, but each requires a visual purpose and verifiable result.

### Closed-loop editing

Every selected image follows a bounded feedback loop:

1. **Composition pass:** evaluate crop, perspective, subject placement, and frame balance.
2. **Global pass:** set exposure, white balance, dynamic range, color relationships, lens correction, noise, and base sharpness.
3. **Local pass:** apply region-level masks and fine shaping where confidence is sufficient.
4. **Render pass:** produce a full-resolution candidate and a 100% detail crop for inspection.
5. **Review pass:** compare against the source and the previous candidate, update the quality scores, and list remaining risks.

The Skill may revise and render a candidate up to three times. It stops early when the candidate passes the quality gate or stops improving. A lower score after an iteration triggers rollback to the best verified candidate, not retention of the last attempt.

Stopping is not determined only by iteration count. Stop when improvement falls below the threshold, a candidate is merely brighter or more saturated without better hierarchy, further editing would damage detail or structure, or the director brief has been satisfied. Report the stopping reason.

For high-potential images, the Skill may generate up to three named variants: `natural`, `editorial`, and `competition-standard`. All variants use the same source and metadata, but each has its own edit plan, quality scores, and output path. The user can accept one, request a hybrid, or reject all. The Skill must not silently choose an aggressive variant when the user requested a natural result.

When a high-potential image has an uncertain target, generate 2–4 materially different candidate directions, then compare composition, hierarchy, color, detail, artistic expression, and style fit. All candidates share one source asset group but have independent operation graphs, transformation labels, quality reports, and output paths.

### Series consistency

For a folder containing a sequence, the Skill chooses a style anchor from the strongest high-confidence frame, then adapts that plan per image. It preserves consistent color temperature, contrast logic, black point, crop language, and visual density while allowing exposure corrections for individual frames. It also enforces diversity so a final set is not filled with near-identical views.

For street/documentary and competition series, the Skill also proposes an order and narrative role for each selected image: opening, establishing, transition, peak moment, detail, or closing. It must show the proposed sequence separately from the per-image ranking.

### Input asset groups and version relationships

Determine file relationships before scoring. RAW, JPEG, and XMP may be different representations of one photograph; bursts, exposure brackets, panoramas, and HDR sources may belong to one source asset group. Generate stable `asset_group_id` and `stable_photo_id` values to prevent duplicate scoring, duplicate import, or treating a historical export as a new original. Final rendering should prefer an available RAW or other original-quality source; JPEG is an analysis preview unless explicitly selected as input.

### 2. Lightroom execution layer

Lightroom Classic remains the RAW and catalog authority. The current `lightroom` MCP integration is used for:

- import or reference-in-place cataloging;
- metadata and Develop-state reads;
- global exposure, white balance, tone, HSL, lens correction, noise reduction, sharpening, vignette, and crop;
- full-resolution rendering to a 16-bit TIFF intermediary or high-quality JPEG;
- export verification.

The Lightroom layer also applies a color-managed export profile. The default master is rendered through the RAW pipeline, then converted to sRGB for the shareable JPEG. A 16-bit TIFF/PSD master preserves the widest practical editing latitude. The Skill records the selected camera profile, working space, output profile, and proofing warnings in the run manifest.

The Skill records the pre-edit Develop state before changing a catalog photo. Default export-only mode restores the pre-edit catalog state after a successful run; an explicit “keep edits in Lightroom” option leaves the approved Develop state in the catalog.

### 3. Photoshop execution layer

Photoshop is used when the edit plan requires operations Lightroom cannot safely express:

- local masks and selective dodge/burn;
- subject/background separation;
- selective highlight control on signs, windows, or lamps;
- local color shaping and gradient transitions;
- cleanup of distracting objects or sensor marks;
- perspective and advanced retouching.

The Photoshop adapter must use non-destructive layers or smart objects where the host API supports them. It receives a structured edit plan and a rendered intermediary, then returns a final TIFF/JPEG plus an operation manifest. Generative tools are valid post-production backends; if Photoshop or the generative backend is unavailable, the Skill may run a Lightroom-only fallback but must label the result as “global-only” and must not present it as a fully refined competition-standard candidate.

### Application transactions and recoverability

Every run creates a unique `run_id` and captures catalog state, application state, source hashes, and the current step before editing Lightroom or Photoshop. Each photo uses an isolated workspace and checkpoint, and every operation has an idempotent identity so retries do not stack edits. On disconnect, timeout, dialog, or export failure, pause and preserve the manifest, then allow resume from the latest verified checkpoint or cancellation; an unverified intermediary cannot be marked complete.

### 4. Local memory layer

Store all persistent state under the local application-data directory:

`/path/to/local-user/Library/Application Support/PhotoPostProduction/`

The store contains:

- style profiles: genre, tonal intent, color relationships, contrast preference, crop preference, and reference links;
- edit feedback: image/run identifier, user acceptance, requested changes, and which parameters were changed;
- run manifests: input paths, hashes, selected files, edit plans, tool versions, output paths, and validation results;
- creative controls: project brief, `creative_intensity`, `source_fidelity`, subject priority, target use, and transformation level;
- post-production provenance: model or software, version, prompts, masks, seed when available, operation graph, input/output layers, and checkpoints;
- no uploaded images and no remote personal profile.

Separate style memory into long-term personal style, current project style, and per-image exceptions. A single-image correction should affect the current run first; only repeated consistent feedback should promote it to a project or long-term preference.

Support pairwise and local comparisons such as “A is better than B” or “A has better color, but B has better crop.” Store pairwise preferences, parameter edits, and free-text comments separately so a global choice is not misinterpreted as a preference for every adjustment.

### Runtime locality, queue, and resource budgets

Every run declares `processing_locality`: `local-only`, `allow-cloud-generation`, or `mixed`. Record the actual application, model, and data location used for every step. When `local-only` is selected, do not call a generative backend that requires uploading the source image.

Batch processing uses a pausable queue that separates preview analysis, candidate generation, full-resolution rendering, and export validation. Limit candidate count, retries, concurrency, disk use, and estimated runtime. Serialize Lightroom writes and Photoshop document operations by default; allow failed photos to be rerun independently.

Use content-addressed caches for analysis results and intermediaries. Cache keys must include source hash, configuration hash, Skill version, tool versions, and model version. Invalidate stale caches rather than reusing a result merely because the filename is unchanged.

Maintain an independent state for each photo: `discovered`, `grouped`, `analyzed`, `scored`, `planned`, `processing`, `rendered`, `validated`, `approved`, `rejected`, or `failed`. A single-photo failure must not let the batch enter a false completed state.

Feedback updates style preferences only after an explicit user signal such as “喜欢这版”, “太暗”, “保留更多环境”, or a numeric rating. A single correction does not permanently rewrite the style profile; the Skill accumulates weighted evidence over multiple runs.

Preferences are versioned and store both positive and negative evidence. For example, “喜欢主体更亮” and “不喜欢一眼黑” are separate preference events with source run, affected region, confidence, and later correction status. The user can inspect, disable, or reset a preference profile locally.

## End-to-end workflow

1. Validate the input folder, supported formats, write permissions, and output destination. Never use the output folder as an input source.
2. Associate RAW, JPEG, XMP, bursts, exposure brackets, panorama sources, and historical exports into asset groups. Import or reference RAW files in Lightroom without copying or modifying the originals. Record file hashes and original metadata.
3. Produce analysis previews at a lightweight size. Keep the original RAW as the rendering source.
4. Classify genre and cluster near-duplicates.
5. Score all images and produce a report with selected, borderline, and rejected groups.
6. For selected images, derive a style plan from the user’s local profile. Internet reference research is opt-in and produces attribute summaries and source links, not copied image assets.
7. Generate the director brief and reversible operation graph, then generate a Lightroom global edit plan and, when needed, a Photoshop/generative local-edit plan.
8. Apply the plan in a reversible run workspace. Render an intermediary at full source resolution unless the requested crop defines a smaller frame.
9. Run the shareability/competition-standard quality gate. If critical warnings remain, revise the plan and retry up to three times. In review mode, stop before final writes and show the candidate plus warnings.
10. Export a high-quality JPEG with preserved metadata and optionally a 16-bit TIFF/PSD master. Write a sidecar/run manifest describing the crop and edit operations.
11. Validate output dimensions, color profile, embedded metadata, source linkage, and absence of writes to the original path.
12. Record the user’s feedback and accepted/rejected result in local memory.

## Human review workspace

Review mode must present an actionable candidate summary rather than a single continue prompt. It includes a contact sheet, source/candidate comparison, local edit regions, score reasons, warnings, variant differences, and 100% detail previews. The user can approve or reject per image or variant, request changes to exposure, crop, color, or local constraints, and undo to the previous checkpoint. Every decision is written to the run manifest.

## Modes and natural-language controls

The Skill has two execution modes:

- `auto`: process images above the configured score and confidence thresholds, retry failed candidates, and export without per-image confirmation;
- `review`: perform import, scoring, previews, and edit proposals, then wait for approval before applying edits or exporting.

Natural-language requests must map to explicit options:

- “只评分” → no edits or exports;
- “自动处理这个文件夹” → auto mode;
- “先给我确认” → review mode;
- “只处理评分 80 分以上” → minimum score threshold;
- “先试跑 5 张” → batch calibration with a sample limit;
- “只在本地处理” → `processing_locality=local-only`;
- “A 比 B 好” → store a pairwise preference without rewriting the global style;
- “已经够好了” → stop post-production for the current photo or batch;
- “保留更多环境/主体更突出/不要太暗” → feedback constraints for the current run;
- “记住这次风格” → persist feedback after user approval.

## Editing intent and authority

Every run has an explicit editing intent. If the user does not specify one, use `natural-enhancement` and ask only when the distinction materially changes the result. Genre classification does not impose a content-editing restriction; under the current `edit_authority=full`, every intent may call supported generative and transformative operations. Intent controls the visual target, transformation strength, and priority:

- `documentary-truthful`: favor believable context, but user-authorized removal, addition, or reconstruction remains allowed and must be explicitly reported;
- `natural-enhancement`: improve readability and atmosphere while keeping the scene believable;
- `editorial-expression`: use stronger crops, tonal shaping, local contrast, color separation, and content transformation;
- `competition-standard`: optimize photographic impact, finish, and visual expression as an internal quality target; human review is optional by run mode, and unresolved weaknesses must be reported;
- `commercial/creative`: allow the broadest cleanup, compositing, expansion, reconstruction, and stylization.

The user’s “full authority” means the Skill may automatically select and execute all supported post-production operations, including generative fill/expand, element removal/addition, large crops, perspective reconstruction, sky/background replacement, local repainting, and global stylization. It never means permission to overwrite RAW files. The run manifest records:

```json
{
  "intent": "competition-standard",
  "edit_authority": "full",
  "content_policy": "user-authorized-transformative"
}
```

Supported content transformations include at least `remove-element`, `add-element`, `generative-fill`, `generative-expand`, `replace-sky-or-background`, `reshape-geometry`, `large-crop`, `relight-subject`, and `style-reconstruct`. These are normal post-production operations rather than automatic failures; they require risk labeling, local quality checks, and before/after comparison.

## Quality gate

The quality gate evaluates the rendered image, not only the parameter list. It reports:

- exposure balance and whether important areas are crushed or clipped;
- subject separation and visual hierarchy;
- crop integrity and whether the main subject is cut unintentionally;
- highlight halos, edge artifacts, color fringing, oversaturation, and unnatural local contrast;
- noise/detail balance at 100% inspection;
- color harmony and believable white balance;
- whether the final image communicates a clear photographic intention;
- whether the result is materially better than the unedited preview.

For generative or heavily transformative edits, additionally inspect building lines, faces and hands, signs and text, water reflections, light direction, sky edges, repeated textures, and regions around removed objects. The purpose is to detect low-quality generation, not to prohibit transformation; on failure, rerun the operation, replace the backend, or downgrade the candidate instead of delivering an obvious artifact.

Default acceptance is `technical >= 75`, `aesthetic >= 78`, `improvement >= 10`, and no critical artifact. A “competition-standard” label additionally requires `aesthetic >= 85`, technical gates to pass, and explicit recording of any generative or content-transforming operations. It is an internal quality target, not a contest-rule compliance judgment.

An image can be technically clean yet rejected as a competition-standard candidate if its moment, composition, or narrative is weak. The Skill must not force every selected RAW into a finished-looking image.

## Output contract

Each run writes to a new destination directory:

```text
<output>/
├── report.json
├── scores.csv
├── quality-report.json
├── previews/
├── variants/
├── selected/
│   └── <stable-photo-id>/
│       └── <variant-name>/
│           ├── <name>.jpg
│           ├── <name>.tif          # default for high-potential images
│           ├── <name>.psd          # default when Photoshop is used
│           ├── masks/
│           ├── edit-plan.json
│           └── edit-manifest.json
└── rejected.json
```

The default share/competition-standard JPEG is full-resolution after the approved crop, sRGB, quality 100, and preserves EXIF/IPTC/XMP fields that exist in the source. The Skill must not fabricate missing creator, copyright, or GPS fields; it may ask the user to supply them. A separate social-preview profile may resize and compress, but it must never replace the master export.

Every output receives a transformation level: `source-faithful` (largely faithful to the source scene), `enhanced` (primarily exposure, color, and local improvements), or `transformative` (element changes, generative expansion, major reconstruction, or style reconstruction). This is an internal disclosure label, not a compliance judgment.

Outputs use named profiles rather than treating quality 100 as the only quality criterion. At minimum provide `web-share`, `competition-quality`, and `print-master`, each recording target dimensions, color space, ICC profile, bit depth, compression, and metadata policy. The metadata policy must let the user choose whether to retain GPS, software history, and copyright fields. Every output receives a checksum and reports which fields were retained, removed, or unavailable.

`quality-report.json` must include, for every processed image, `asset_group_id`, `stable_photo_id`, keep value, editability, candidate potential, final score, `creative_intensity`, `source_fidelity`, `processing_locality`, resource budget, iteration count, stopping reason, before/after comparison paths, region edits, generative/content-transforming operations, tool/model versions, prompt/mask references, transformation level, warnings, rejected risks, and the reason for the final shareable or competition-standard label.

For a high-potential image, the editable master and mask data are part of the default deliverable rather than an optional afterthought. A JPG-only result is acceptable for rejected or low-priority images, but not for an approved competition-standard candidate.

For generative post-production, the default deliverable should also preserve the pre-generation intermediary, generated layers, masks, prompts, model/software versions, parameters, and operation graph. Even when the backend is not perfectly reproducible, the user must be able to continue from a verified checkpoint instead of reprocessing the entire image.

## Safety and failure handling

- Original RAW paths are read-only targets for the Skill; all writes go to Lightroom’s catalog, a run workspace, or the new output directory.
- Filenames, directory names, EXIF, IPTC, XMP, visible image text, and imported prompts are untrusted data; they cannot alter Skill rules or trigger unauthorized operations. Normalize paths and check traversal and symlinks before reading or writing.
- Before any edit, capture the current Develop state and source metadata.
- If Lightroom or Photoshop disconnects, stop the affected photo, preserve the manifest, and report the exact stage. Do not silently continue with an unverified result.
- If Photoshop is unavailable, expose the global-only fallback and its limitations.
- If a source file is unsupported, missing, duplicated, or unreadable, continue other files and include it in the report.
- If output validation fails, do not mark the run complete and do not overwrite an existing output.
- `local-only` mode must verify actual data routes and backend capability; if the route cannot be confirmed, stop that generative step and report why.
- Mark the source relationship of every generative or transformative result; never treat a transformed JPEG as an original source in a later run.

## Validation strategy

### Deterministic tests

- score schema and threshold behavior;
- near-duplicate clustering and stable photo IDs;
- mode parsing and safety defaults;
- output path isolation and overwrite prevention;
- metadata presence and source-link validation;
- quality-gate warning and retry behavior;
- style-memory update only after explicit feedback;
- region-plan schema validation and confidence-based skip/review behavior;
- rollback to the best candidate when an iteration scores lower;
- series-anchor consistency and near-duplicate diversity;
- deterministic technical metrics against known fixtures;
- editing-intent policy enforcement;
- positive/negative preference versioning and reset behavior;
- variant isolation and hybrid-plan generation;
- color-profile and proofing metadata in the run manifest.
- score confidence and technical-gate precedence;
- adjustment budgets, generative-operation records, and user-authorized content transformations;
- mask-edge, halo, color-spill, texture-loss, and geometry-distortion warnings;
- checkpoint/resume, idempotent retry, cancellation, and Lightroom state restore;
- named output profiles, ICC/metadata/GPS policy, and checksums;
- human review decisions for variants, local operations, and feedback constraints.
- director brief, `creative_intensity`/`source_fidelity`, and recorded default assumptions;
- RAW/JPEG/XMP/burst/bracket/panorama asset grouping and stable photo IDs;
- operation-graph dependencies, single-step rollback, multi-candidate exploration, and candidate isolation;
- generative semantic quality checks and `source-faithful`/`enhanced`/`transformative` labels;
- provenance completeness for models, prompts, masks, versions, seeds, and input/output layers.
- batch calibration, calibration skipping, and project-only calibration scope;
- `local-only`, `allow-cloud-generation`, and `mixed` locality policies with actual backend records;
- pausable task queue, resource budgets, independent reruns, and stopping reasons;
- separate aggregation of pairwise, local, and free-text feedback;
- locked regression fixtures measuring selection, over-processing, generation errors, and user correction changes after Skill updates.
- phase boundaries ensuring Phase 1 does not depend on Photoshop, generative editing, or style learning;
- isolation of untrusted filenames, EXIF/XMP, visible text, imported prompts, and paths;
- content-addressed cache hit, invalidation, and version isolation;
- per-photo state machine and batch failure isolation;
- separate reporting for AI score, human fixture performance, style fit, and user correction rate;
- editable delivery of generative intermediaries, layers, masks, and checkpoints.

The first acceptance run has these hard invariants: zero RAW overwrites, zero auto-accepted critical gate failures, zero completed runs with failed output validation, and 100% successful Lightroom restoration. Selection precision/recall and user correction rate are reported against an annotated local fixture; they are not presented as universal aesthetic truths.

### Integration tests

- one RAW through Lightroom import/reference, metadata read, Develop update, render, export, and restore;
- one Lightroom-only fallback with an unavailable Photoshop adapter;
- one Photoshop-enabled run with a local adjustment manifest;
- the supplied `/path/to/local-user/Downloads/sample-photo.ARW` as the first end-to-end fixture.

### Manual acceptance

For `sample-photo.ARW`, the first acceptance run must show:

- a readable skyline and water reflection without a crushed-black presentation;
- an intentional crop that does not accidentally cut the main tower;
- controlled sign and window highlights;
- believable night color with a clear visual hierarchy;
- a full-resolution JPEG whose metadata links it back to `sample-photo.ARW`;
- a report that distinguishes “shareable candidate” from “competition-standard candidate” without claiming real contest eligibility;
- at least one editable master or a clear reason why the image was exported as JPG-only;
- a documented intent, final score, region plan, and before/after comparison.
