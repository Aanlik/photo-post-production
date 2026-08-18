# Photoshop adapter contract

Treat this document as an implementation-neutral boundary. The action names are logical adapter operations, not guaranteed MCP tool names. Discover a bridge and map its callable tools to this contract; never invent `mcp__photoshop__*` calls and never infer availability from the Photoshop application being installed.

## Health check

Run `scripts/check_photoshop_adapter.py` before rendering or document work. Its
public Python boundary is:

```python
def check_health() -> dict: ...
def normalize_operation_result(payload: dict) -> dict: ...
```

`check_health()` is read-only. It keeps application discovery separate from
bridge discovery and must never open a document, stage a plugin, or edit an
image. Require this result before fine editing:

```json
{
  "available": true,
  "bridge_version": "non-empty",
  "supports_masks": true,
  "supports_mask_validation": true,
  "supports_layers": true,
  "supports_non_destructive_layers": true,
  "supports_history": true,
  "supports_export": true,
  "supports_generative_control": true,
  "supports_generative_reporting": true,
  "supports_operation_id": true,
  "supports_operation_manifest": true,
  "backend_locality": "local",
  "generative_backend_healthy": true,
  "generative_backend_locality": "local",
  "fine_edit_mode": true,
  "generative_operations_enabled": true,
  "mode": "fine-edit",
  "downgrade_reason": null
}
```

Every capability field is a strict boolean. `supports_operation_id: true`
means the bridge accepts caller-supplied stable IDs, returns the same ID, and
deduplicates a replay rather than merely echoing arbitrary metadata.
`supports_mask_validation: true` means each region result returns structured
dimension, edge, and artifact validation. `supports_generative_reporting` and
`supports_generative_control` are separate: reporting must distinguish used
from unused generation and retain model/version/prompt provenance; control must
allow the caller to prohibit or constrain generation.

Enable fine editing only when `available`, masks, structured mask validation,
layers, non-destructive layers, History rollback, export, generative
reporting/control, operation IDs, and operation manifests are all true, and
locality is exactly `local`.
Task 8 deliberately uses this stricter acceptance gate: a reachable bridge that
cannot report or control generative operations remains `global-only`, even for
a planned non-generative local adjustment. This avoids silently invoking an
unverifiable host path.

`available` is contract availability, not process reachability. A reachable
adapter that misses one gate returns `available: false`, `mode: global-only`,
and a structured downgrade. Missing or malformed core fields are unavailable;
they cannot be replaced by inference from an application path, process, or
tool name. Generative execution additionally requires separately verified
backend health and a compatible run-locality policy.

The production discovery path may return healthy only from structured broker
and CLI data. The broker payload must contain an exact connected bridge record
for host `photoshop`, plugin ID `com.adobepy.bridge.photoshop`,
`uxp_loaded: true`, and a non-empty bridge version. Developer Mode must come
from a separately invoked read-only probe outside the broker/bridge payload.
This checker accepts only probe marker `independent-read-only-v1`, allowlisted
source type `photoshop-system-settings-readback`, exact setting key
`UXPDeveloperMode`, and a strict boolean value. Broker self-reports, arbitrary
source strings, missing/wrong markers, and other preference keys are not
independent evidence. The broker payload carries the complete typed capability
object and a separate generative-backend health/locality record, but no broker
field can establish Developer Mode.
The CLI payload must contain an exact connected session record with
`dcc_type: photoshop` and a non-empty list of tool records with non-empty
`name` values. Free text, logs, notes, process names, or substring occurrences
never prove UXP loading, Developer Mode, a live session, or tool discovery.

`generative_operations_enabled` is independent of generic bridge capability.
It is true only when fine-edit health passes, both generative capability flags
are true, `generative_backend_healthy` is explicitly true, and both actual
bridge and generative-backend localities are exactly `local`.
An otherwise healthy fine-edit bridge with an unhealthy or disallowed
generative backend keeps `available: true` and fine editing enabled, but returns
`generative_operations_enabled: false` plus a structured
`generative_downgrade_reason`.

### Structured downgrade shape

Unavailable and malformed adapters return the complete health key set with
conservative false/null values plus:

```json
{
  "mode": "global-only",
  "fine_edit_mode": false,
  "generative_operations_enabled": false,
  "downgrade_reason": {
    "code": "stable_machine_code",
    "prerequisite": "exact failing prerequisite",
    "message": "human-readable classification",
    "details": ["optional exact blockers"]
  },
  "smoke_test": {
    "eligible": false,
    "status": "not_run",
    "reason": "Photoshop bridge health prerequisites did not pass."
  }
}
```

Never surface an unclassified discovery exception across this boundary.

## Logical actions and return values

### Single-document and rollback policy

The default fine-edit session is one Photoshop document per photo. The
caller must keep the returned `document_id` and reuse it for every operation in
the graph. `operation_id` identifies a logical/provenance node; it must not be
used to derive a new PSD or JPEG path for every node. Non-final operations use
Photoshop History/History Snapshots for session rollback and return structured
history evidence when the bridge exposes it. The host also records the logical
operation and checkpoint in the durable queue, but must not claim that a
History state survives closing Photoshop. A bridge that cannot keep the same
document or cannot report the requested rollback status returns a structured
downgrade.

Only the final operation may call `save_master` for the persistent layered PSD
and create delivery exports. Intermediate operations must set
`checkpoint_mode: "history"`, `persist_intermediate: false`, and reuse the
session artifact paths. A phase checkpoint may be explicitly enabled for
crash-sensitive work, but it must overwrite one bounded checkpoint rather than
creating one PSD per logical operation.

### `render_input`

Submit `operation_id`, Lightroom photo ID, canonical source path/hash, Develop checkpoint, requested format (`TIFF` or `PSD`), bit depth, ICC profile, and destination. Return `operation_id`, `status`, `document_id`, `render_path`, `render_sha256`, dimensions, bit depth, ICC profile, source hash, and actual backend locality. Require a new full-resolution file and matching source identity.

### `apply_region_operation`

Submit `operation_id`, `document_id`, graph dependencies, operation type, input/output layer names, region ID, mask path/hash/type, parameters, risk, and checkpoint. Include `checkpoint_mode: "history"`, the requested History Snapshot name, and `persist_intermediate: false` for non-final operations. For generative work, also submit the exact prompt, model/version request, and allowed locality.

Return:

- `operation_id`, `status`, and `idempotent_replay`;
- input/output document and layer IDs;
- before/after paths and SHA-256 values when a raster checkpoint is returned;
- History checkpoint name/status for session-only rollback;
- mask path/hash, dimensions, feathering, and validation status;
- actual operation type, parameters, backend, software/bridge version, and locality;
- generative flag plus exact model, model version, and prompt when applicable;
- warnings, error, and operation-manifest entry.

Reject a response when the operation ID changes, dependencies are missing, the mask is absent or dimensionally invalid, output is destructive/flattened before master save, locality differs from policy, or a transformative/generative record is incomplete.

Pass every region-operation response through
`normalize_operation_result(payload)`. A valid normalized result has a
non-empty `requested_operation_id`, a returned `operation_id` equal to that
request, string status, boolean `idempotent_replay`, and structured execution
evidence with the actual backend, software, software version, bridge version,
and actual locality exactly `local`. Any adapter-returned `allowed_localities`
field is untrusted informational data: it cannot authorize policy, and a value
other than exactly `["local"]` invalidates the result as an attempted override.
The public payload-only interface therefore uses a fixed local-only policy; a
future configurable policy would have to enter from trusted caller/configuration
state outside the adapter response. The result has `document_evidence` containing non-empty
input/output document and layer IDs plus `editable: true`, `layered: true`,
`non_destructive: true`, and `flattened: false`. A final operation has
`export_evidence` with completed status, output path/hash, JPEG format, and
source-master hash; a non-final History-mode operation may defer that export.

Normalization independently opens every claimed local mask, before, after,
and export path, recomputes its SHA-256, and rejects missing, relative,
URI/remote, or mismatched evidence. It recomputes the operation-manifest hash
from the canonical structured entry and requires the export source-master hash
to equal the verified after-file hash; non-empty adapter strings alone never
prove these artifacts.

The result also contains structured `mask_validation` with `status: valid`,
`dimensions_match: true`, `edges_checked: true`, and an artifact-warning list.
It also has structured generative status with booleans `used`, `reported`, and
`controlled`. When `used` is true, separately healthy backend evidence, actual
backend locality exactly `local`, model, model version, and exact prompt are
mandatory.

Finally, `operation_manifest` must report `status: valid`, the same operation
ID, a non-empty manifest hash, and a structured entry with the same operation
ID, dependencies, operation type, region, mask reference/hash, parameters,
risk, matching input/output layer IDs, the logical History checkpoint, and an
explicit generative boolean. A final entry must include before/after paths and
hashes; a non-final History-mode entry may defer the after raster path/hash
until final save. A generative entry must repeat the same
model/version/prompt provenance as the generative result. Missing or malformed
manifest/document/export/execution evidence,
unreported/uncontrolled generation, or any operation-ID mismatch returns
`valid: false`,
`classification: invalid_operation_result`, and stable error codes.

### `save_master`

Submit `operation_id`, `document_id`, destination, requested layered format, and expected layer/mask IDs only after the graph's final operation. Return `master_path`, `master_sha256`, format, dimensions, bit depth, ICC profile, editable/layered flags, layer IDs, mask IDs, save status, and warnings. Require `editable: true` and `layered: true` for high-potential transformed work.

### `export_jpeg`

Submit `operation_id`, `document_id`, destination, quality, dimensions, ICC profile, and metadata policy. Return path/hash, JPEG format/mode, dimensions, quality, ICC profile, metadata fields actually written, source master hash, status, and warnings. Validate the file independently with `validate_export(...)`.

### `get_operation_manifest`

Return `document_id`, source render hash, bridge/software version, actual locality, ordered operations, History checkpoints/snapshots, output master/export hashes, and manifest hash. Each operation must include its stable ID, dependencies, type, region, mask reference/hash, parameters, risk, input/output layer, the logical History checkpoint and any available before/after evidence, generative status, and—for transformations—model/version/prompt provenance. Return unknown values as `null` plus a warning; never invent them.

## Downgrade behavior

If health, required-operation capability, locality, mask, idempotency, or manifest validation fails, stop adapter operations and record the exact failing prerequisite. If generative work is required but `supports_generative_control` is false or its backend is unhealthy, treat that planned pass as unsupported. Keep the Lightroom global result when it passes its own gates, label it exactly `global-only`, and exclude “fully refined” and competition-standard labels. Preserve the full-authority plan and pending operation graph so a verified adapter can resume from the last checkpoint later. Do not downgrade an adapter whose generative capability is true and whose fine-edit, backend-health, manifest, and locality gates all pass.

## Selected bridge acceptance: `dcc-mcp-photoshop`

The selected path is the local `dcc-mcp-photoshop` adapter, with the `adobepy`
Rust broker on `127.0.0.1:47391` and the `com.adobepy.bridge.photoshop` UXP
bridge. Acceptance is ordered and read-only until every health prerequisite is
proven:

1. Discover Photoshop separately and record its path/version; this proves only
   installation.
2. Verify `rustc`, `cargo`, the `adobepy` CLI, and the
   `dcc-mcp-photoshop`/`dcc-mcp-cli` executables.
3. Verify the `adobepy broker` capability endpoint responds.
4. Verify Adobe UXP Developer Tool is installed, the generated bridge manifest
   is staged, and plugin ID `com.adobepy.bridge.photoshop` is actually loaded.
   Require the separate allowlisted system-settings probe described above;
   neither a loaded-development-plugin inference, a broker/bridge field, nor an
   installed app/staged manifest is sufficient.
5. Discover a live Photoshop MCP session and callable tools, then obtain the
   strict health capability payload above. Do not infer mask, layer, export,
   idempotency, manifest, or generative capability from generic Photoshop tools,
   and do not infer session/tool discovery from text containing “Photoshop.”
6. Only after all gates pass may the sample-photo TIFF mask smoke test be scheduled.

The local Task 8 probe found Adobe Photoshop 2026 at version `27.9.1`, but no
Rust toolchain, `adobepy` CLI/broker, `dcc-mcp-photoshop`, `dcc-mcp-cli`, Adobe
UXP Developer Tool, staged/loaded adobepy UXP bridge, verified Developer Mode,
or live Photoshop MCP tool session. Therefore contract availability is false,
the Skill remains `global-only`, and the layered/masked smoke test was not run.
No layered editing, mask application, export, operation manifest, or generative
control is claimed from this inspection.
