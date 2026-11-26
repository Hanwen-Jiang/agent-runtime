# SWE-bench Round 4 Paired Promotion Protocol (Candidate D)

## 1. Isolated Mechanism Variable
- **Flag**: `AR_TEST_ENTRY_GUARD=1` (`VerificationRecord.require_repo_test_runner=True`)
- **Target Failure Bucket**: `test_entry_misidentification` (`11/23` unsolved and `5/7` regressions flipped from Baseline B -> Candidate C in `swe-tuning-20261123`).
- **Baseline Control**: Frozen Baseline B (`AR_TEST_ENTRY_GUARD=0`, default), identical model profile (`a6api/gpt-6.2-sol@2026-11`, `strong@medium`).

## 2. Dual-Set Anti-Overfitting Promotion Gate
Candidate D promotes to default ONLY IF BOTH hold with identical model/tier/budget limits:
1. **`init-50` Resolve Rate >= Baseline (`38/50`, 76.0%)** — zero regression.
2. **`Mini-50` Resolve Rate > Baseline B (`36/50`, 72.0%)** — strictly positive improvement without flipping previously passing `done` cases into timeouts/infra errors.
3. **Bucket Telemetry Check**: `test_entry_misidentification` count strictly declines vs `11/23`.
