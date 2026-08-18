# Runtime calibration and queue contract

## Optional project calibration

Run calibration when the requested look is uncertain, the batch spans visibly different lighting/subjects, or the style profile conflicts with the director brief.

1. Select 3–5 representative groups across lighting, subject, and technical difficulty; exclude near-duplicates.
2. Create bounded candidate plans using the same intent, creative controls, locality, and authority as the batch.
3. Present a compact contact sheet in review mode or evaluate automatically only when confidence and gates permit.
4. Record pairwise, local-region, and free-text feedback separately.
5. Store calibration under a project run ID such as `project:<project-name>:<run-id>`. Do not promote it to the long-term `default` profile without explicit user direction.
6. Freeze the selected calibration profile version and series anchors in the run manifest. Do not let later batch feedback silently change already queued plans.

Use `init_store`, `get_profile`, `record_feedback`, and `record_pairwise_feedback` from `style_memory.py`. Store the SQLite database locally under `/path/to/local-user/Library/Application Support/PhotoPostProduction/`; never store image bytes in it.

## Resource budget

Before queueing, record:

- maximum selected candidates and variants per photo;
- maximum three evaluated iterations per candidate;
- retry count and retryable error classes;
- Lightroom, Photoshop, analysis, and export concurrency;
- disk-space ceiling and minimum reserve;
- wall-clock or per-photo time ceiling;
- local/cloud model allowance and cost ceiling when applicable.

Serialize Lightroom writes and Photoshop document operations by default. Parallelize read-only preview analysis within memory limits. Stop scheduling new work before exceeding disk, time, locality, or cost limits; checkpoint active work first.

## Per-photo queue state

Use `transition_photo_state(...)` and only these transitions:

| Current state | Allowed target states |
| --- | --- |
| `queued` | `analyzing`, `failed` |
| `analyzing` | `analyzed`, `failed` |
| `analyzed` | `clustered`, `failed` |
| `clustered` | `scored`, `failed` |
| `scored` | `selected`, `review`, `rejected`, `failed` |
| `selected` | `editing`, `completed`, `failed` |
| `review` | `selected`, `rejected`, `failed` |
| `rejected` | `completed` |
| `editing` | `edited`, `failed` |
| `edited` | `completed` |
| `completed` | none |
| `failed` | `queued` |

Reject every transition not listed in the table. Retry only through `failed -> queued`; do not move `failed` directly to an active later state. A reviewed photo must return through `review -> selected` before editing or finish through `review -> rejected -> completed`. A selected photo may finish through `selected -> completed` without editing, while a rejected photo may only move to `completed`. A failure belongs to one photo and must not mark siblings complete.

## Checkpoints and resume

Persist checkpoints before catalog writes, before medium/high-risk local operations, after each render, after master save, after export validation, and after Lightroom restoration. Include source/trusted-context hashes, photo state, operation IDs, completed graph nodes, adapter health/version, Lightroom Develop snapshot, output paths/hashes, budget consumption, and next action.

Resume only when all checkpoint fields verify:

1. Rehash source assets and match the sealed source snapshot.
2. Match director brief, locality, intent budget, skill/tool/model versions, and operation graph.
3. Verify prior output and mask hashes and editable-master presence.
4. Recheck adapter health and capabilities; downgrade unsupported pending work instead of replaying it blindly.
5. Verify the expected Lightroom state or complete the recorded restore before applying another write.
6. Reuse stable operation IDs so replay is idempotent. If idempotency cannot be proven, branch from the last verified checkpoint.

Mark completion only after output validation and any required Lightroom restore succeed.
