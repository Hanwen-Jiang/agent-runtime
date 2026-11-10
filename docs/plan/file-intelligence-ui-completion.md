# File Intelligence UI completion — 2026-11-10

The previous file-intelligence implementation supplied the main workspace but
left several supported backend capabilities without complete UI paths. This
change completes those paths in Coding Agent's HeroUI Pro React workspace and
adds the small backend metadata/selection bridges needed to make them work.

## Delivered UI paths

The entry remains the conversation's Sources panel. The workspace now has
Files, Collection and Trash views, in-workspace upload/retry, search/type filters,
fixed-version selection, operation status/reconnect/cancel/history, and explicit
permission-aware actions. Existing compact task timeline, subagent panel and
composer continue to use the React components already delivered.

- PDF and Office page previews show actual rendered PNG thumbnails with
  page/slide identifiers. The UI enforces the engine's ten-page operation limit.
- Image/PDF region selection supports pointer drag and numeric percentages.
  Crop and OCR are explicit actions. Low-confidence results stay visibly partial.
- Structured readers expose document paragraph/table, worksheet/range,
  slide/shape, Notebook cell and archive member controls. Notebook reading never
  executes code; selected-cell execution requires explicit confirmation.
- SQL supports up to ten authorized source versions with separate table aliases,
  worksheets/ranges/headers, parameters and real CSV output links.
- Editing has forms for XLSX values/formulas, DOCX paragraphs/runs/tables, PPTX
  text/images and Notebook source. Typed expected values support real Calc
  verification. L1/L2 requirements and permitted visual regions are explicit.
- Candidate review shows before/after values, validation results, downloads and
  a review checkbox before publishing. Conflict/failure never becomes success.
- Collections can select multiple read representations of one file, display
  coverage, query with source citations, and be selected for the next task.
- Version/access controls include historical downloads, metadata comparison,
  current-user grants/revocation, trash and private restore. Candidate review lists
  the current actor’s processing records, matching the job authorization boundary. Selecting a prior
  version for a task carries `source_versions`; newly submitted task history
  records every actually resolved source version.

## Necessary backend completion

- `GET /v1/files/{file_id}/versions/{version_id}` exposes an authenticated
  immutable-version metadata record, preserving current ACL and logical head.
- Job/detail projections include pinned inputs, request selectors and one stable
  active consumer belonging to the current actor. Cancelling it leaves other
  consumers running. History is filtered for the file before taking 100 rows.
- Office conversion is followed by bounded Poppler page rendering while keeping
  the original exported PDF available. Public artifacts keep original-source
  anchors; no derived PDF is promoted to an unrestricted source.
- Trusted multi-input table mapping validates selector/header arrays against the
  already-authorized immutable input order. Client paths and extra child scopes
  remain rejected.
- DOCX read anchors distinguish body paragraph/table indexes from mixed block
  order. OCR coordinate conversion occurs before computing the block identity.

## Evidence and bounds

The Coding repository's `docs/ui/file-intelligence-ui-coverage.md` maps every
path to implementation and evidence. Real Chromium loaded a frozen bundle for
28 full-workspace checks, 18 preview/structure checks, 12 edit/review checks and
six assertions covering four asynchronous state races. Screenshots explicitly
identify their synthetic HTTP fixture; they are not claims of real model or
engine execution. Final server regression results are recorded separately in
that repository's `docs/ui/evidence/file-ui-parity/`.

Runtime evidence under `docs/plan/evidence/file-ui-contracts/` uses actual
LibreOffice, Poppler, RapidOCR, DuckDB and authenticated HTTP/catalog behavior.
It includes Office page selection, original hashes, mixed DOCX paragraph/table
positions, region-coordinate anchors, cross-workbook SQL returning 26, resumed
consumer isolation and historical-version selection. The `linux/` supplement
records the newly built UI-parity image and actual server execution; previous
file-engine image tags do not contain these source updates.

This completes UI access to the supported file-intelligence operations, not a
claim of an online Office editor or unrestricted file-format support. DOCX
header/footer references identify the original version but do not pretend to
support exact body-index editing or automatic pixel-perfect jumps. Version
comparison is explicitly metadata comparison; candidate changes have their own
before/after verification view. Unknown formats/renderer failures stay visible.

No production deployment is part of this UI completion. Runtime, Coding and the
new processing image must be deployed together to expose these controls against
real services. The prior corpus's manual-review and semantic-retrieval quality
limits remain unchanged by UI work.

The final processing image for these changes is
`fi-20261110-engines:ui-parity` at
`sha256:130437f1ff960777f485b3993c53c8859edafb85ca950efa3cd29fd084aa159b`.
All eight standalone processing modules match the current source. Linux
Office/PDF rendering, region OCR and the cross-workbook query were executed
with no network, read-only root filesystem and bounded CPU/memory; detailed
commands and hashes are in the `linux/` evidence supplement.

## Final regression result

The frozen Coding implementation passed the project server's complete
`go test -json ./...` and `go vet ./...`: 257 top-level tests, 335 passing events
including subtests, zero failures. The existing opt-in public-discovery check
was not enabled in this run (its preceding live acceptance is retained). All
140 compiled Coding Go/static inputs matched local source before and after the
run. Latest Runtime file/task API packages also passed the race detector and
vet; all 35 compiled Go inputs matched the local implementation.

Initial whole-suite testing exposed a pre-existing timing hole where a forced
composer refresh detached an open settings popup's anchor. A one-condition fix
defers that redraw until the popup closes. The deterministic failure was
reproduced before the fix, then passed five times after; the original unchanged
unlimited-settings test passed ten repetitions. No test expectation, timeout or
network permission was relaxed. Evidence and losslessly compressed logs are in
Coding `docs/ui/evidence/file-ui-parity/server/`.

The final component bundle was checked again in clean Chromium profiles:
28 workspace interaction cases passed, with byte-identical served assets. The
separate Notebook preview boundary check passed ten assertions for small and
oversized header/stream bodies, selected cells and inert markup. Explicit
parentheses in its byte-limit comparison improve readability; JavaScript's
original shift precedence was already correct, so this is not described as a
functional fix. A local whole-suite attempt was interrupted by temporary-disk
exhaustion; the complete server run above, not that attempt, is the final result.
