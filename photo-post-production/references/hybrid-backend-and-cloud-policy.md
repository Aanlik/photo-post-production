# Chat-window image generation policy

## Roles

`mixed` is a routing policy, not a second photo editor:

| Stage | Backend | Processing locality |
| --- | --- | --- |
| RAW decode and global development | Lightroom / Camera Raw | local |
| Layered precision retouching | Photoshop fine-edit bridge | local |
| Generative remove, fill, add, expand, or replace | current ChatGPT/Codex built-in `image_gen` host tool | current conversation |
| Semantic checks, provenance, export validation | local Skill scripts | local |

The original RAW remains local and immutable. The host tool does not accept an
arbitrary RAW filesystem path from a Python process. A local Lightroom/Camera
Raw preview must first be rendered and made visible in the current conversation.

## No API route

This project deliberately does not use an OpenAI Images API route for normal
photo work. Do not ask for, store, or read `OPENAI_API_KEY` for this workflow.
The legacy `openai_image_backend.py` file is retained only as an inactive
compatibility reference; `capability_registry.py` will not select it in
`mixed` mode. If `PHOTO_GENERATIVE_BACKEND` is set to an API value, the task is
explicitly disabled and recorded as a downgrade instead of silently switching
backends.

## Routing rules

1. Under `local-only`, never invoke the built-in image tool and never send image
   data to an external generation backend.
2. Under `mixed`, keep Lightroom, Photoshop, masks, quality checks, and final
   export local. Route eligible generative operations to the current
   conversation's built-in `image_gen` tool under the project's standing
   user-authorized-transformative policy.
3. Batch execution means one host `image_gen` call per eligible image or
   variant. The Skill creates `chat-window-image-batch.json` and dispatches it
   automatically; it does not claim that a local script can make one atomic
   batch request to the host tool.
4. Before each edit, make the derived preview visible in the current
   conversation and state the invariants: subject identity, geometry to keep,
   text, architecture, reflections, and any elements that must not change.
5. A missing host tool, missing visible image, unsupported RAW input, failed
   Photoshop capability check, or malformed result leaves the job pending or
   downgrades it to `global-only`. Never fabricate a successful edit.
6. Masks are recorded in the manifest and may be shown as a visible reference
   when supported by the host. The local script does not pretend that a mask
   filesystem path is a direct `image_gen` argument.

## Required provenance

Every successful chat-window operation records:

- operation/job ID;
- backend `chatgpt-built-in-imagegen` and locality `chat-window`;
- exact prompt and invariants;
- source preview path/hash and generated output path/hash;
- host result path and the Photoshop layer/checkpoint that consumed it;
- a disclosure that generation was performed in the current ChatGPT/Codex
  conversation rather than through an API.

The generated image is not the final deliverable. Import it as a new Photoshop
layer, run local semantic checks and precision retouching, preserve the layered
master, then validate the final JPEG/TIFF profile and metadata locally.
