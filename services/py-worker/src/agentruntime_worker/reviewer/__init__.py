"""Reviewer（计划 S-10；设计书 3.2.5、附录 B.4–B.9）：Round 的结尾——汇总、诊断、Verdict、收尾。

- signals：硬信号与升级台账，由代码从 Task State、Stage、结果与之前各 Round 的摘要算出（附录 B.6）。
- rules：升级顺序与次数上限（附录 B.8）；代码判定 floor，模型的提议只会被往上抬。
- prompts：Progress Check 的提示；只放 Stage 结果、Task State 与验收条件，不放 Planner 的规划过程。
- reviewer：一次 Progress Check（不合格重问一次，再回退到规则），规则兜底后给出 Verdict 与汇总。
- summary：把汇总写进 Task State（工作流调用，纯函数）。
- wrap_up：收尾写回——归档 Task State、交经验与 Recipe 效果候选给 Memory Service、写 trace 指标。
- activities：review_round 与 finalize_task 两个 decision activity。
"""
