# Linux file-engines image and UNO validation — 2026-11-10

The image was actually built and run on the authorized `cloudnium_cloud`
validation host. No production services or existing containers were modified.
The isolated source/evidence root was
`/root/proj/agent-projs/.file-intelligence-validation-20261110`.

## Artifact and environment

- Final image: `fi-20261110-engines:validation`
- Image ID: `sha256:fb638071d954329c16dba47b9292e4efec3dddd9075fe7fa35cfc2b18c3229d7`
- Docker-reported image size: 1,846,998,431 bytes.
- Base source commit: `fdac343dae9a893cb80692fa7c0da15538b40902` plus the
  reviewed Dockerfile/model-list/platform-marker/recalculation fixes in this
  change. This is not an assertion that the unpatched base commit passed.
- Recalculation module SHA256:
  `1328f465c069df579a25ecfd4df784cbd31b51c363d6ed728426e216b1d47998`.
- Ubuntu 24.04 packages from snapshot `20261110T000000Z`; LibreOffice
  `24.2.7.2 420(Build:2)`; matching system Python 3.12/pyuno.
- Runs: 4 CPU, 8 GiB memory, no network, read-only root filesystem, bounded
  `/tmp` tmpfs, synthetic fixtures and writable output only under the validation
  evidence mount. Final UNO container exited 0 and was not OOM-killed.
- `pip check`, system `import uno`, and build-time verification of all 18 model
  files against the checked-in SHA256/size manifest passed.

The static sandbox-agent was built with four Cargo jobs and exported to the
validation `bin/` directory for the other test suite. Its SHA256 is
`795c96f868594cdc9bf62d0379dc11d2cf767bac09c17cb8a7dfcfd037fce7a5`.

## Findings fixed before final acceptance

1. The Rust musl build stage lacked `musl-tools`.
2. The Ubuntu base needed CA certificates before accessing the HTTPS snapshot.
3. The Python lock contained macOS-only `appnope` without a platform marker.
4. The model manifest listed Heron ONNX files but omitted that repository from
   the download list. Its verified revision is now explicitly pinned.
5. Real UNO returned a valid empty-string formula cache as `<c t="str"><f>…</f><v/></c>`.
   `openpyxl` exposes that as `None`, the same as a missing cache. Verification
   now accepts only this explicit empty-string representation; genuinely
   missing/error caches still fail.
6. A private soffice process group is terminated on deadline/error, including
   launcher children. Macro workbooks, external workbook references and data
   service formulas fail before UNO is opened.

`uno-run1.log` records the detected empty-cache failure. `uno-run2.log` records the corrected successful run. `uno-run3.log` and
`uno-results.json` repeat the full suite and additionally verify a real local
`Table1[Amount]` structured table reference. Build/rebuild logs,
`system-packages.lock`, and `fonts.lock` record the actual runtime inputs.

## What was verified

The executable fixture is `scripts/file-engines/uno_acceptance.py`.

- 22 formulas under **each** of the 1900 and 1904 date systems. All formula
  caches were intentionally poisoned to 999 before execution. The code called
  UNO `calculateAll()` then `storeToURL()`; the reopened output matched expected
  numeric, Boolean, date and string results.
- SUM, AVERAGE, COUNTIF/COUNTIFS, SUMIFS, IFERROR, ROUND, INDEX/MATCH, VLOOKUP,
  IF, LEN, percentage arithmetic, YEAR/DATE/DAY, SUMPRODUCT, TEXTJOIN, named
  ranges, cross-sheet dependency chains and VALUE.
- A real XLSX table with `SUM(Table1[Amount])` recalculated to 12; its brackets
  were not confused with an external workbook reference.
- Original source SHA stayed unchanged. Hidden worksheet, selected number
  format/bold styling, merged cells and a chart/series survived the round trip.
  This remains L1 evidence, not universal or pixel-identical Excel fidelity.
- Divide-by-zero, circular reference, unknown function and deliberately wrong
  expected value produced failed verification, so no passing publication.
- Macro/external-link fixtures and external/data-service formulas were rejected.
- A deliberately stalled UNO helper hit its five-second deadline; no private
  soffice process remained afterwards.

LibreOffice 24.2.7 **does not support XLOOKUP** in the tested OOXML form. The
fixture confirms an explicit formula error rather than silent success. No claim
of complete Microsoft Excel compatibility is made. These are synthetic local
server tests; no paid model or external workbook connector was invoked.

## Replay

Build from the repository root with the reviewed base image and Rust image,
then run the fixture in an isolated writable evidence directory:

```sh
docker build --build-arg AGENT_RUST_IMAGE=rust:1.99.1-bookworm \
  --build-arg CARGO_BUILD_JOBS=4 -f deploy/images/file-engines/Dockerfile \
  -t fi-20261110-engines:validation .
docker run --rm --cpus=4 --memory=8g --network none --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=1g \
  -v "$PWD/scripts/file-engines:/validation:ro" \
  -v "$EVIDENCE_DIR:/evidence" \
  --entrypoint /opt/file-engines/bin/python fi-20261110-engines:validation \
  /validation/uno_acceptance.py --output /evidence/uno-replay
```

The evidence directory must be writable by UID 10001. Use a new output directory
for each run; candidates deliberately do not overwrite previous artifacts.

## Final OCR warning image

The subsequent bounded warning fix was built as `fi-20261110-engines:final`,
image ID `sha256:b3a9cdcdda52b573259ebeaf00b9fb9a2b61a6d6232566c21ccb907c04f09f5e`.
Comparing every Python module embedded in the two images confirms **only
`models.py` changed**; the accepted UNO module hash remains exactly
`1328f465c069df579a25ecfd4df784cbd31b51c363d6ed728426e216b1d47998`.
The new `models.py` SHA is
`578fe341a335c416bea8125a4602cdc06ac0398233bbb47a78048db0049532b7`.

`ocr-warning-final/` contains a dedicated four-case regression:

- The two frozen OCR-mismatch cases (`image_02.webp`, `scan_pdf_17.pdf`) were
  reread in the actual new image. All block fields, including text, IDs,
  confidence scores and anchors, remain identical to the benchmark baseline;
  original bytes and SHA remain unchanged. Both now return `partial` with
  `ocr_low_confidence` and the count in coverage, so the uncertain content is
  visible to clients instead of silently appearing fully ready.
- Frozen multi-sheet `xlsx_00` retains its prior `partial` status and no warning.
- An independent normal single-`Public`-sheet XLSX is `ready`, warning-free and
  block-identical when processed by old and new images.

The frozen golden manifest, corpus and previous parser results were mounted
read-only. No benchmark/retrieval score was recomputed or changed by this check.
The final container exited 0 without OOM, with four CPUs, 8 GiB RAM, disabled
network and a read-only root filesystem. The first harness attempt incorrectly
assumed the existing two-sheet XLSX was `ready`; its failure log is preserved,
and the final harness distinguishes that already-partial fixture from the
separate ready control rather than changing parser behavior to pass the check.
