# Native image input v1 — bounded delivery plan

Status: implementation and bounded local verification complete. Live-provider activation is not claimed. Scope approved in the current conversation.
Work item: S-01 contract extension + S-02 ModelProvider + Model Gateway / Worker integration.

## Outcome and scope

Uploaded PNG/JPEG/WebP sources explicitly referenced by the current task reach a vision-capable model as actual image content. Generate, StreamGenerate and follow-on tool-loop calls preserve the same authorized images. Existing text tasks remain compatible.

This version does NOT include video/audio, GIF animation, PDF rendering, automatic OCR/cropping, provider API migration, cross-task implicit image inheritance, or child-agent image delegation. The existing source reuse action explicitly pins a prior upload into a new task. Do not expand this list during implementation.

## Four deliverables

1. **Contract**: preserve ChatMessage fields 1–5, add typed parts and image object references. Add GetCapabilities so new workers reject an old gateway instead of silently losing images. Add image_input model capability and explicitly configured per-image token reservation. Regenerate Go/Python code and run buf lint/breaking against HEAD.
2. **Gateway**: use the configured ObjectStore to resolve only current-task manifest references, verifying tenant/workspace/hash/MIME and decoded dimensions. Convert to OpenAI-compatible content arrays only at the provider boundary. All routing/fallback candidates must support images. Budget estimates exclude Base64 and reserve declared image tokens; actual usage settles the bill.
3. **Worker**: keep typed references in a stable context partition outside compressed text history. Planner, tool loop and final response receive the current-task images. Capability failure is explicit. No workflow command changes.
4. **Verification and handoff**: existing relevant suites; isolated synthetic end-to-end PNG probe through gateway and captured upstream request (including tools, stream, fallback and denied references); compatibility checks; document configuration and results. No fabricated claim of a live commercial model call.

## Limits

10 images/request; 10 MiB/image; 20 MiB combined bytes; 20 million decoded pixels/image; maximum edge 8192. Images are validated as PNG/JPEG/WebP. Original bytes are immutable; no automatic resize. Bounds are application policy, not supplier promises. Detail is auto/low/high. A model profile must declare image_input and constraints.image_input_tokens (positive conservative per-image token reserve confirmed for that endpoint/model); no capability inferred from the model name.

## Contract rules and rollout

ContentPart is text OR image. A message uses legacy text OR typed parts, never both. Image content is user-role only. Object URIs are not public URLs; the gateway never fetches arbitrary external URLs. Raw bytes/Base64 must not appear in workflow state, traces or metering logs.

Deploy updated gateway with ObjectStore and a verified vision model profile before the updated worker. GetCapabilities is transport/capability negotiation, not proof of model correctness; the real endpoint's image support must be checked before enabling its profile. Text-only deployments remain usable and image requests fail explicitly.

## Exit criteria

- All four deliverables above are implemented and reviewed; required contract checks and targeted tests pass, or an external tool limitation is identified precisely.
- Synthetic PNG bytes at the upstream boundary match the uploaded SHA and remain present after a tool result; text-only requests retain their former wire shape.
- No text-only fallback, unauthorized object read, Base64-as-text budgeting, or silent old-gateway downgrade.
- Documentation names activation steps, what was and was not verified, and remaining out-of-scope work.

Stop after this checklist. Any extra improvement is recorded separately and does not reopen the completed scope. A missing paid endpoint credential or external executable is a verification limitation, not a reason to start unrelated migrations.

## Exit review

- [x] Contract and generated bindings; Buf lint/format/breaking; backward text compatibility.
- [x] Authorized image resolution, input bounds, vision-only routing/fallback and separate budget reservation.
- [x] Worker typed context through planning, compaction, tool continuations and delivery; old/mixed Gateway output rejected.
- [x] Relevant existing suites and isolated end-to-end/format/budget probes; rollout and limitations documented in `docs/reference/native-image-input.md`.

No workflow command changes, new decoder dependencies, provider migration, live profile edits or production deploy. The concrete contract diff is ready for S-01 human review. No further work will be appended to this plan automatically.
