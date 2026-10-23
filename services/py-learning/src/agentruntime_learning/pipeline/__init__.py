"""Learning Pipeline 的最小闭环（设计书 3.5.1、附录 J；TMP 计划 T-16，细化见 docs/plan/learning-pipeline.md）。

读 trace（traces）→ 按租户过滤并留审计 → 归纳候选 Recipe（candidates）→ 观测式评测与非劣效门槛（evaluation）
→ 过门槛才经 Registry 发布（run）。失败归因（attribution）用小模型，单独记预算（budget）。
命令行：python -m agentruntime_learning.pipeline run --tenant <租户> --family <任务族> --traces <文件>…
"""
