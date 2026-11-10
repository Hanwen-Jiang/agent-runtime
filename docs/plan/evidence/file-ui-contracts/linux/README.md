# Actual Linux UI-contract engine acceptance

Date: 2026-11-10. Host: `cloudnium_cloud`, isolated validation directory
`/root/proj/agent-projs/.file-ui-validation-20261110`. No production services,
credentials or user files were changed. Inputs are non-private generated fixtures.

## Image and source identity

- Image tag: `fi-20261110-engines:ui-parity`.
- Image ID: `sha256:130437f1ff960777f485b3993c53c8859edafb85ca950efa3cd29fd084aa159b`.
- Docker-reported size: 1,846,999,401 bytes.
- `engine-module-sha256.json` and `source-comparison.json` show that all eight
  standalone processing modules match the local implementation under review.
- Recalculation module SHA remains
  `1328f465c069df579a25ecfd4df784cbd31b51c363d6ed728426e216b1d47998`, identical
  to the previously certified real UNO implementation. That suite was not
  misreported as rerun here; only UI-dependent changes were revalidated.
- `build.log` is the actual cached source-layer image build log. The immutable
  dependencies/models/fonts are unchanged; the parser/renderer changes are
  included in this tag, rather than falsely attributed to the older final tag.

## Actual checks

1. `office-preview-results.json`: real LibreOffice export plus Poppler page
   rendering. DOCX page 2, PPTX slide 3, and the first ten slides produce the
   corresponding PNG selectors while preserving the full PDF export. More than
   ten slides, out-of-range slides and conflicting page/slide selectors fail.
   All original hashes remain unchanged (six checks).
2. `selector-results.json`: paragraph/table/paragraph DOCX preserves the mixed
   block index while separately exposing the correct paragraph/table indices.
   Real RapidOCR recognizes `REGION 42` from PNG and PDF crops and returns boxes
   inside the original normalized source region (three checks).
3. `ocr-identity-results.json`: the full OCR block identity is recomputed from
   its final original-source anchor; PNG and PDF IDs agree and original hashes
   are unchanged (two additional checks).
4. `fi-ui-20261110-query-options.log`: the trusted bridge binds one selector and
   header per authorized input. Invalid lengths, non-boolean headers, paths in
   selectors and a mismatched primary selector are rejected. Delegated children
   cannot add `input_selectors` or `input_headers` to widen their scope.
5. `query-selector-result.json`: the bridge-generated request is executed by
   the image-owned DuckDB implementation. It selects `Public` in the first
   workbook and `Lookup` in the second, avoiding deliberately wrong first
   sheets, and returns exactly `[[26]]`. Both input workbook hashes remain
   unchanged. The actual CSV artifact digest is retained in the result.

The query adapter check ran with the existing full Worker dependencies in a
separate `agentruntime-ci:3` container. Its output request was then executed in
this file-engine image. The standalone parser image intentionally does not
provide the complete Worker/Temporal SDK; this record does not claim otherwise.

`execution.json` records exact container image IDs, commands and limits. All
three containers exited 0 without OOM, with networking disabled and a read-only
root filesystem. Parser/compute containers used four CPUs and 8 GiB; the bridge
check used two CPUs and 3 GiB. Only the evidence output and private tmpfs were
writable. Completed containers were removed after their logs/inspect metadata
were preserved; images and evidence remain available.

## Reproduction

Mount the parent `file-ui-contracts` directory read-only as `/probes`, and an
empty directory writable by UID 10001 as `/evidence`. Run `linux/engine_probe.py`
using `/opt/file-engines/bin/python` in the exact image. It refuses an engine
import outside `/opt/engine`.

Then run `query_selector_probe.py` from the matching full Worker environment
with `FILE_UI_SELECTOR_ROOT=/evidence/selector-work` and the same evidence mount.
Finally, run `linux/query_result_probe.py` in the exact image. The existing
probe scripts now accept environment variables for result paths; their original
local default paths remain unchanged.

Source data, source code, and expected results stay separate. No permanent new
pytest/Go test module or new processing service was introduced.
