# Final changed-path server verification

Date: 2026-11-10. Isolated validation root on `cloudnium_cloud`:
`/root/proj/agent-projs/.file-intelligence-validation-20261110`.
Production services were not changed.

- `go-final-summary.json` and losslessly compressed original Go event logs:
  latest Task Service 630 tests/subtests in 16 packages and Model Gateway 191 in
  five packages, all passing. `go-final-status.txt`: task=0, vet=0, gateway=0.
- `server-authority-{sqlite,postgres}.log`: real relational backends and race
  detector; no object/ACL/publication failures after fixes.
- `collection-selection-final.log`: explicit collection-only Tasks, retry scope
  fencing, frozen generation, denied scope escalation, raw-access separation and
  GC protection. More context in `../file-collection-selection/`.
- `memory-full-backends.log`: 211 passing tests including real PostgreSQL/Qdrant.
- `contracts.log`: the earlier 440-test subset. The final complete 638-test
  contract result is in `../server-regression-20261110/contracts-final-fixed.log`.
- `deploy-final-verified.log`: 408 passed, 26 opt-in image/kind cases skipped.
  Two old expectation mismatches were reproduced against the unmodified base,
  then assertions were aligned with existing documented behavior: explicit
  subagent `off`, and the bounded `goal-cf-YYYYMMDD` release tag family. No
  deployment settings changed and no failed case was removed.
- `engine-source-sha256.txt`: current eight standalone engine module hashes,
  matching the final reviewed image. The OCR warning change has its own original
  text/anchor-preservation proof under `../file-engines-linux/ocr-warning-final/`.

The original complete Worker run and correction/replay proof are recorded in
`../server-regression-20261110/`. Full Chromium and six settings-save interleaving
cases are in the Coding repository. Benchmark model-quality and runtime limits
are reported separately; test pass counts are not quality claims.
