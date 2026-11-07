# Evaluation readiness, 2026-11-07

> 历史盘点记录，费用与完成状态以[全量交付最新记录](goal-full-20-2026-11-07.md)及各次原始账本证据为准。早期报告金额不代表已核实余额；pending既不是实际费用，也不能视为免费。

## Authorization and remaining recorded budget

`all-plans-dag-2026-11-03.md` section 7.1 records the user's authorization and
per-item caps. The current full-20 goal resumes implementation and evaluation;
this audit does not increase those caps or launch paid runs.

| Evaluation | Cap USD | Recorded spend USD | Remaining USD |
| --- | ---: | ---: | ---: |
| Terminal-Bench 40 | 80 | 36.432857 | 43.567143 |
| Capability matrix | 200 | 120.085294 | 79.914706 |
| DeepEval | 10 | 3.022385 | 6.977615 |
| EV-REAL / N16 paired experiment | 40 | No run output found | Reserve at most 40 pending ledger reconciliation |
| LongMemEval | 20 | No run output found | Reserve at most 20 pending ledger reconciliation |

Evidence: `/opt/evals/tb2/{one,one-b,one-c,tb40-a,tb40-b}/report.json`;
`/opt/evals/mx/round1/results.jsonl` (5742 records, sum `cost_usd`);
DeepEval `/opt/evals/{w1/deepeval-b,t18/deepeval,t18/deepeval-2}`.
DeepEval task records are deduplicated by task_id: 16 tasks cost 1.969457;
three report judge totals are 0.557520, 0.170256 and 0.325152. The two t18
reports reuse the same task IDs, so counting task cost twice would be wrong.
The conservative DeepEval figure includes those older t18 costs in the same cap.
These are report totals, not a new reconciliation of the model gateway ledger.

The old Terminal-Bench 23 completed tasks ran on revision 29. Finishing 17 on
current Runtime cannot be presented as a single-version 40-task baseline.
Use separate version-labelled cohorts, or run all 40 with one fixed configuration
within the remaining cap. Before running, check disk, environment readiness,
egress quota, and fixed model/Registry/Memory settings; do not deploy while a
baseline is running. Existing budget adapters can overshoot by an in-flight call,
so leave headroom below the remaining cap.

## Browser smoke

Using Chromium in the temporary `goal-network-check` container, ran:

```sh
uv run --package agentruntime-learning --all-extras pytest services/py-learning/tests/test_eval_site_pages.py -q
```

Result: **5 passed in 11.70s**, none skipped. Actual dashboard server and checked-in
evalsets; homepage, datasets and results pages; Chinese/English switching;
390-pixel mobile overflow checks; dataset grouping/search/expansion. This tests
the locally served current assets, not a deployment or CDN rollout. The existing
results-page probe currently checks loading and script errors, not score accuracy.

## Remaining experimental constraints

- MX-PUB planning anomaly is already traced in capability-matrix-evals section 13
  (S09-DRIFT): all-or-nothing shape scoring and prompt omissions. Twelve all-zero
  tasks do not distinguish candidates. Fix and version scoring/prompt semantics
  before rerunning or publishing that cell; do not hide failures or pool versions.
- DeepEval needs a fresh output directory and freshly captured actual tool events;
  old records cannot be fabricated into complete traces. Regrade the new records.
- LongMemEval's live retriever uses the existing Memory Service internal HMAC
  authenticator. Task API's dev-token refusal does not itself block that Memory
  path. For Task API experiments use the existing operations JWT workflow.
- EV-REAL and N16 use one paired off/rules experiment with fixed model, examples,
  Registry revision, Memory configuration and quality rubric. Do not change the
  global worker default just to run the control arm; isolate workers/queues.
