# File processing sandbox

This image runs the existing sandbox-agent. The Worker invokes
`python3 -m agentruntime_worker.files.cli` through Sandbox Service; there is no
new parsing HTTP server or workflow engine. System Python 3.12 and Ubuntu's
`python3-uno` match LibreOffice's bindings; the file-engine virtual environment
uses `--system-site-packages`. `AR_UNO_PYTHON=/usr/bin/python3` selects those
bindings for isolated Calc operations.

## Build and fixed assets

Build from the repository root, for the same platform as the existing static
sandbox-agent (the current Dockerfile builds its x86_64 musl target):

```sh
docker build --platform linux/amd64 \
  --build-arg BASE_IMAGE='ubuntu:24.04@sha256:<reviewed-base-digest>' \
  --build-arg AGENT_RUST_IMAGE='rust:1.99.1-slim-bookworm@sha256:<reviewed-build-digest>' \
  -f deploy/images/file-engines/Dockerfile -t sandbox-file-engines:m1 .
```

The package set is pinned in `requirements-file-engines.lock`; Docling layout
and TableFormer repositories are pinned to exact HF commits in
`models.lock.json`. RapidOCR's bundled PP-OCRv6 detection/recognition and
PP-OCRv4 classification bytes are verified by SHA256. The build explicitly
prefetches 18 model files (732,963,687 bytes); the request path never downloads
models. Ubuntu dependencies use the frozen snapshot timestamp, and the image
records `/opt/system-packages.lock` and `/opt/fonts.lock`. These include Poppler,
LibreOffice and the actual Noto CJK/core/extra, Liberation and DejaVu fonts.
Release must record the resulting image digest. The tag defaults are development
conveniences, not a claim of an immutable production build.

Runtime: deny network, read-only image filesystem, writable private `/tmp` and
operation workspace, CPU/memory/pids/time quotas. The process uses UID 10001.
Required environment is passed through sandbox-agent: `PYTHONPATH`, model path,
offline flags, UNO/LibreOffice executable paths, locale/timezone. Original input
and output directories must be separate; raw bytes remain unchanged.

## CLI contract

Send one JSON object on stdin; stdout contains exactly one JSON response. Engine
logs are redirected to stderr. Paths may be absolute within the controlled
sandbox or relative to its cwd; relative paths cannot escape cwd.

```json
{"operation":"read","input_root":"inputs","path":"inputs/report.pdf","output_dir":"outputs/read-1","selector":{"pages":[0,1]},"options":{"layout":"complex","ocr":true}}
```

Operations: `inspect`, `read`, `render`, `archive_list`, `extract`; the `file.*`
variants are aliases. M3 supplies `table.*`, `table.recalculate`, `document.*`,
and `notebook.*`. Successful responses include `status`, `blocks`, `outline`,
`warnings`, `coverage`, `artifacts`, deployed `capabilities`, source SHA/size/MIME,
and processor/config digests. Artifact paths are local output paths with
SHA/size/MIME; the Runtime bridge uploads them and owns their authorized object
references. The engine does not mint file IDs, grants or publication state.

Selectors:

- PDF: zero-based `page_index`, `pages`, or `start`/`count` (at most 10).
  Optional normalized top-left `bbox` selects a crop; regional text uses OCR.
- Text: one-based `start_line` and `max_lines` (at most 2000).
- DOCX: zero-based `block_index` or `start`/`count` (at most 500 blocks).
- XLSX: `sheet` (name) or `sheet_id: "sheet-0"`, bounded A1 `range`, at most
  2000 cells. Formula and cached value are separate; reading never recalculates.
- PPTX: zero-based `slide_index`/`slides`, optional integer `shape_id`.
- Image: normalized `bbox`; `options.ocr` enables real RapidOCR.
- Notebook: zero-based `cell_index`/`cells`; read never executes cells.
- Archive extraction: explicit `members` list (1–100), no links/devices/path
  traversal; at most 1000 total members and 200 MiB expanded content.

Responses cap at 500 blocks and 40,000 text characters, reporting `partial` and
`output_truncated`. Not all material is plain text: DOCX inline images and PPTX
pictures are identified separately, not falsely reported as visually understood.
Unsupported types or missing engines return `unsupported`; OCR/model errors are
not treated as completed extraction. Office rendering uses a private profile,
disabled macros and automatic link updates; Calc's stricter UNO checks are in
M3. HTML/SVG source is not executed by these readers.

## Reproducible local acceptance

Only the standalone file engine runs on 3.12; do not install the main Worker
package (which requires 3.13) into this environment.

```sh
uv venv --python 3.12 .file-engines-venv
uv pip install --python .file-engines-venv/bin/python -r requirements-file-engines.lock
.file-engines-venv/bin/python scripts/file-engines/prefetch_models.py \
  --manifest deploy/images/file-engines/models.lock.json --output .file-engines-models
FILE_ENGINES_MODEL_DIR="$PWD/.file-engines-models" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .file-engines-venv/bin/python scripts/file-engines/acceptance.py \
  --evidence docs/plan/evidence/file-engines-m1/acceptance.json
```

This creates synthetic public fixtures, runs real parsers/OCR/layout/renderers,
checks original hashes, archive rejection and JSON CLI framing, then removes the
fixtures. It does not call a paid model. Current evidence was produced on macOS
arm64 using Python 3.12.14, Poppler and an available LibreOffice executable;
Docker image building was not performed on that host because its Docker
executable is a broken symlink. Subsequently, the full image was built and the
explicit UNO calculateAll suite passed on an isolated Linux server; see
[Linux image/UNO evidence](../../../docs/plan/evidence/file-engines-linux/README.md).
That evidence records the exact image/source hashes, two date systems, stale
caches, failure cases and XLOOKUP's explicit unsupported result. The earlier
macOS parser run itself is still not an image-build claim.

## Trusted Worker bridge and Agent tools

Production Worker configuration:

```text
AR_TASK_SERVICE_URL=http://task-service:<public-api-port>
AR_FILE_INTERNAL_TOKEN=<same scoped internal-service secret as task-service>
AR_FILE_ENGINES_RUNTIME=file-engines
AR_PROFILE=production
AR_SANDBOX_SERVICE_ADDRESS=<sandbox-service-address>
```

The existing Worker token signer is a development compatibility path only when
no file-service token is provided. File tools never accept an arbitrary API URL,
file identity, model generation or filesystem path in place of the current
Task's pinned object references. The tools are `file_inspect`, `file_read`,
`file_render`, `table_query`, `file_edit`, `file_search`, `evidence_resolve`.
Long file jobs return their real handle; `file_inspect(job_id=...)` checks it.
Cancelling an in-flight tool removes that consumer, leaving shared-job policy to
the File API. Edits create candidates; publishing remains a separate API action.

The bridge rechecks all pinned root hashes, versions, ACL epochs and actor grants
before running and before publishing. It borrows a fresh Sandbox Service lease
with no workspace/session, `prepare=false`, `project_environment=false`, no
network and no reuse; no user project setup runs. Notebook execution is rejected
in the development local backend and explicit local-Python mode. L2 Office edits
run an independent candidate verification invocation with fixed base/candidate
hashes and allowed visual regions before being marked ready.

Rendered images are attached by the File API to the current Task after checking
its fixed input snapshot. The next model call loads those authorized typed image
references. They do not become arbitrary new input-file tool permissions.

Manual bridge acceptance (Worker Python 3.13 plus engine Python 3.12):

```sh
PYTHONPATH="$PWD/services/py-worker/src:$PWD/libs/py-core/src:$PWD/proto/gen/python" \
AR_SANDBOX_AGENT_IMPL=go <worker-python> -B scripts/file-engines/bridge_acceptance.py \
  --engine-python "$PWD/.file-engines-venv/bin/python" --sandbox-service \
  --evidence docs/plan/evidence/file-engines-m1/bridge-acceptance.json
PYTHONPATH="$PWD/services/py-worker/src:$PWD/libs/py-core/src:$PWD/proto/gen/python" \
<worker-python> -B scripts/file-engines/tools_acceptance.py \
  --evidence docs/plan/evidence/file-engines-m1/tools-acceptance.json
```

The first probe runs real engine subprocesses, SQLite/ObjectStore and optionally
real Sandbox Service with its local Go agent backend. The second uses an explicit
HTTP service double for tool-wire checks and real local authority/object stores;
it is not a claim of an end-to-end production HTTP deployment. Temporary local
mode must be selected explicitly with `AR_FILE_ENGINES_LOCAL_PYTHON`, is blocked
under `AR_PROFILE=production`, and is never an automatic fallback.

## Whole-sheet child delegation

A file sub-agent receives only explicit whole-sheet scopes in its trusted node
inputs, for example `file_scopes: [{path: "obj://…/workbook.xlsx", selector:
{sheet: "Public"}}]`. Only `file_read` and `table_query` may use those scopes;
read/query selectors must match the named sheet exactly. Worksheet-ID aliases,
cross-sheet ranges, extra input tables, arbitrary paths, raw attachment reads,
imports, shell/repository tools and collection search are rejected for that
file-scoped child. Returned worksheet outlines contain only the selected sheet.
The first implementation supports actual XLSX inputs; a text/PDF renamed XLSX
cannot obtain a whole-file read by supplying a worksheet selector.

The workflow pins this scope in internal activity input under
`child-file-whole-sheet-v1`. The Node Executor also identifies an old pending
SubAgentWorkflow call from trusted Temporal activity metadata, so old payloads
cannot bypass the new guard. Missing delegation fails closed when the parent
has direct files **or** pinned knowledge collections. Ordinary coding sub-agents
whose parent has neither keep their existing tools. Parent file access is
unchanged. Scoped children receive neither the parent's file/collection catalog
nor its full conversation transcript; file-scoped children do not inject broader
memory retrieval. A ContextVar carries the trusted scope only during that tool
call and is reset even on failure.

`temporal_acceptance.py --scopes-only` exercises real SubAgentWorkflow → Node
Executor → File API → Sandbox Service → XLSX/DuckDB, with Public/Secret fixtures,
legacy pending inputs, concurrent parent/child calls and history replay. The
recorded server evidence is under `docs/plan/evidence/file-engines-m1/`.
