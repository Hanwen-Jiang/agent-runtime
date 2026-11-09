# Native image input v1

Implementation scope and exit checklist: [bounded plan](../plan/native-image-input-v1.md).

## Configuration and deployment order

1. Model Gateway must read the same ObjectStore as task-service and Worker. Development uses explicit `AR_OBJECT_STORE_DIR`; production uses the existing `AR_S3_*` configuration. Gateway does not expose file bytes through HTTP.
2. Configure a model profile only after checking that the actual upstream endpoint accepts OpenAI-compatible `image_url` content parts with tools and streaming. Add `image_input` to its capabilities and `constraints.image_input_tokens` as a conservative per-image input token reservation for that model and the accepted dimensions/detail. This is a budget bound, not the billed number. Keep actual prices in the existing cost fields. Do not copy an arbitrary token/price number from another model.
3. Deploy the updated Gateway and generated contract before the updated Worker. The Worker negotiates `GetCapabilities` for image tasks. An old Gateway, missing store, or no eligible image model fails explicitly; a pure text task still works. Image responses also require the `x-ar-image-input: v1` acknowledgement, including before the first streamed delta, to catch mixed-version peers behind a load balancer.
4. Keep task-service's authenticated uploads and task manifest pinning enabled. Submit images through the existing source picker/reuse controls.

The Helm gateway now includes the existing object-store environment helper, and the dev stack passes its shared object directory. No live profile is automatically marked vision-capable. Tenant allowlists and required tier/tools/stream/structured capabilities continue to apply to all candidates.

## Data flow

Internal messages carry typed `ContentPart` entries, including immutable object URI, MIME type, SHA256 and detail. Existing text-only messages retain their old wire shape. Text and parts cannot both supply a message body. Images are user content; file content is never promoted into system instructions.

Gateway reads `task-inputs/<task_id>/manifest.json` using the request's tenant-bound store. It requires the exact source reference in that manifest and the matching workspace. Object metadata, byte length, SHA256, actual image format and dimensions are checked. Only then are in-memory bytes attached to the Provider request. The OpenAI-compatible adapter turns them into a Base64 data URI at the outbound boundary. The browser preview URL is never sent to the model.

Current-task image references occupy a stable context partition and are not flattened into compacted text. The same task can continue using tools and images together. Model nodes with pinned attachments bypass the legacy template/input-only result cache in v1, preventing answers for different images from colliding. Provider prompt caching still uses stable ordered image parts. A new task uses the existing explicit source reuse action; implicit cross-task/child-agent delegation is outside this version.

## Limits and budget

Accepted formats: PNG, JPEG, static WebP. Limits: 10 images, 10 MiB each, 20 MiB total, 20 million decoded pixels per image and 8192 pixels per edge. No automatic resizing or OCR. Unsupported formats, invalid/truncated container headers, animation and dimension conflicts fail explicitly. This preflight validates headers and bounds rather than fully decoding every pixel; remaining malformed pixel streams are rejected by the upstream decoder. PDF/GIF/audio/video retain the existing attachment tool path and are not native image parts.

Image bytes are excluded from JSON token estimates and never logged. Per candidate, the Gateway adds `image_count * constraints.image_input_tokens` to text reservation before output allowance and price calculations; supplier-reported usage remains authoritative at settlement. Worker uses the negotiated conservative reservation for context capacity. This avoids treating Base64 characters as millions of text tokens.

## Validation boundary

The local acceptance probe uses an uploaded synthetic PNG, real ObjectStore, Gateway, HTTP adapter and a capturing upstream stub. It verifies byte identity, continuation after a tool result, streaming, vision-only fallback and access rejection. Such a probe establishes transport and control behavior, not visual recognition quality. A paid/live visual-quality check requires an enabled, verified endpoint and is reported separately; it is not implied by unit tests.

External protocol reference: [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision?api-mode=chat). Existing Chat Completions transport is retained in this version.

## Validation record (2026-11-10)

- Buf format/lint and breaking checks against both HEAD and the unchanged baseline passed; original pinned plugins generated Go/Python bindings. 432 targeted contract cases and 7 toolchain checks passed.
- Model Gateway all packages and Go vet passed; OpenAI-compatible adapter, Profile and Router suites passed.
- Worker existing suites passed (154-case and 189-case runs overlap; do not sum them). The last gRPC/stream run passed 88 cases. Eight isolated checks cover retained refs after compaction/tool calls, Planner/final delivery, cache isolation, text compatibility and old/mixed gateway rejection.
- Synthetic HTTP upstream probe passed image byte identity, Generate, continuation after tool output, streaming, vision-only fallback, task authorization and image acknowledgement. Per-model budget probe confirmed a 1 MiB image does not increase the text estimate; small contexts reject before invoking upstream.
- Real PNG/JPEG/WebP headers and crafted WebP ordering/frame/padding failures were checked. No new external image decoding dependency was introduced.
- No paid model call or production deployment was performed. Actual provider vision quality and enabled production profiles remain an explicit activation check. One pre-existing schema enum-parity failure (inline task-event enums already in HEAD) and missing Temporal CLI coverage are outside this change; no workflow commands changed.

The finite implementation/verification scope is closed. Further vision quality tuning, cropping, OCR, PDFs, automatic source inheritance and child image delegation are separate tasks.
