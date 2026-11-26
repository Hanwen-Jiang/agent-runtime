# SWE-bench Round 4 Failure Diagnosis (2026-11-27)

## 1. 4-Bucket Breakdown Across Baseline B & Candidate C

| Cohort | Total Unsolved | ① Discovery / Exhaustion | ② Regression Break | ③ Test Entry Misid | ④ Patch / Format / Env |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline B (`mini-50`) | 23 | 6 | 0 | 3 | 14 |
| Candidate C (`init-50`) | 7 | 2 | 1 | 4 | 0 |
| Candidate C (`mini-50`) | 16 | 8 | 0 | 7 | 1 |
| **Candidate C Flips (Baseline Resolved -> C Unresolved)** | **7** | **2** | **0** | **5** | **0** |

- **Dominant unsolved bucket in Candidate C**: `test_entry_misidentification` (`11/23` unsolved).
- **Exact Regression Breaks (`PASS_TO_PASS > 0`)**: 1 combined cases across Candidate C.

## 2. Flipped Instances (Resolved in Prior Baseline, Unresolved in Candidate C)

- `pytest-dev__pytest-7205` -> `test_entry_misidentification`: agent declared done with F2P failure=9 and zero test edits
- `sphinx-doc__sphinx-10673` -> `discovery_or_readonly_exhaustion`: exhausted rounds/budget (state=cancelled, verdict=, rounds=2)
- `sphinx-doc__sphinx-8056` -> `test_entry_misidentification`: agent declared done with F2P failure=1 and zero test edits
- `django__django-11848` -> `test_entry_misidentification`: agent declared done with F2P failure=2 and zero test edits
- `sphinx-doc__sphinx-10673` -> `discovery_or_readonly_exhaustion`: exhausted rounds/budget (state=cancelled, verdict=, rounds=3)
- `sphinx-doc__sphinx-8056` -> `test_entry_misidentification`: agent declared done with F2P failure=1 and zero test edits
- `sphinx-doc__sphinx-9281` -> `test_entry_misidentification`: agent declared done with F2P failure=1 and zero test edits

## 3. Single-Variable Hypothesis for Candidate D

Because `test_entry_misidentification` accounts for **11/23** unsolved cases and **5/7** regressions flipped from Baseline B -> Candidate C (`agent declared done` with `FAIL_TO_PASS > 0` and zero test-entry edits/verification), Candidate D introduces strictly ONE isolated mechanism variable: **`AR_TEST_ENTRY_GUARD` (`require_repo_test_runner`)**.

- Default `False` preserves frozen Baseline B identically.
- When enabled (`1`), `VerificationRecord.called()` distinguishes ad-hoc `python -c` / script runs from actual repository test-runner entries (`pytest`, `python -m unittest`, `runtests.py`, `tox`, `nox`, `go test`, etc.) after code edits (`wrote_code`), and Reviewer `unproven_done()` blocks premature `done` once until a repository test-runner verification executes.
