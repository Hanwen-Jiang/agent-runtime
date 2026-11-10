# UI contract completion probes — 2026-11-10

- `office_preview_probe.py` ran with the fixed local file-engine Python environment,
  real LibreOffice and Poppler. It generated non-private DOCX/PPTX fixtures and
  verified selected page images, preservation of the original PDF export, correct
  zero-based source page/slide selectors, the ten-page limit and unchanged source
  hashes. Results are in `office-preview-results.json`.
- A temporary Go SQLite `-race` probe verified resumed consumer IDs, complete
  job inputs/request metadata and cancellation of one subscription at a time.
  More than 100 newer jobs for another file did not hide the selected file's
  history. It passed in 1.449 seconds and was removed from the source package;
  the replay copy remains `/tmp/file-ui-detail-probe_test.go` for this workspace.
- The existing `go-task-service/internal/taskapi` suite passed after the changes.

These are backend behavior checks, separate from synthetic browser UI fixtures.
No production services or user files were modified by the probes.

Additional finite UI parameter fixes:

- `selector_probe.py` generated a DOCX containing paragraph/table/paragraph.
  Its second paragraph retains `block_index=2` and now has
  `paragraph_index=1`; selecting that paragraph returns the actual target.
  Table anchors separately expose `table_index` without changing block meaning.
- The same probe used real RapidOCR on a generated image and its PDF form,
  cropping a normalized region. Both recognized `REGION 42`; final OCR boxes
  remain inside the original source selection. Reprojection now happens before
  the block identity is generated, rather than mutating an anchor afterwards.
- `query_selector_probe.py` checks trusted per-input selectors and headers,
  rejects length/type/path and mismatched-primary inputs, and proves delegated
  children cannot add these options. The resulting real DuckDB query joins
  `Public` in the first XLSX with `Lookup` in the second XLSX and returns 26;
  the unrelated first sheets contain deliberately wrong values. The actual
  result is `query-selector-result.json`.

These source changes require the updated file-engines image rather than an
older deployed image. The cached source layer was subsequently rebuilt and
actually tested on the isolated Linux server as
`fi-20261110-engines:ui-parity`, image ID
`sha256:130437f1ff960777f485b3993c53c8859edafb85ca950efa3cd29fd084aa159b`.
All eight standalone engine modules match the reviewed local source; the UNO
module matches the previously certified hash. Actual Office/Poppler, DOCX/OCR,
trusted query-selection and image-owned DuckDB checks all passed. See
[Linux acceptance](linux/README.md) for exact limits, logs, output and the clear
separation between the full Worker adapter environment and standalone image.
No production image reference was changed by this validation.

The root integrator also added an authenticated immutable-version metadata route
for the UI's historical-version selection. See `version-selection/` for real
Runtime and Coding HTTP/race checks. The UI cannot silently substitute the head
when a historical version was selected; mismatched or unauthorized versions fail.

Existing Worker `test_platform_tools.py` and `test_context_read.py` were run with
the project Python environment: 37 passed in 4.09 seconds. An earlier command
named a nonexistent `test_attachments.py` and ran no tests; it is not counted as
validation. The explicit corrected invocation is the reported result.
