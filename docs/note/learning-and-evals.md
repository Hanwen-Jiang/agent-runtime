# 评测与 Learning Pipeline：评测集、门槛、观测式评测与发布

> 学习笔记，2026-10-24。对应代码：评测 harness `services/py-learning/src/agentruntime_learning/evals/`（`runner.py`、`scoring.py`、`gates.py`、`repeat.py`、
> `context.py` 压缩评测、`worker_subject.py` 被测对象、`provider_gateway.py` 不经网关的直连对照）；评测集 `services/py-learning/evalsets/task-loop/`；
> Learning Pipeline `services/py-learning/src/agentruntime_learning/pipeline/`（`traces.py`、`candidates.py`、`evaluation.py`、`attribution.py`、`budget.py`、`run.py`）。
> 设计依据：设计书 3.5、3.5.1，附录 J、H.2；实施计划 S-08 到 S-11；细化 `docs/plan/learning-pipeline.md`；评审 R-06 与另一家复审。

## 1. 先有评测集，再写提示

Planner、Reviewer、压缩提示都靠评测迭代。评测集（S-08）64 条，覆盖六类任务、四个 Triage 级别与主要的 Verdict，每条带输入、期望的 Triage 级别、合格 Stage 的形状特征、
期望的 Verdict；另有 Reviewer 专用样本（给定结果与 Task State 标注正确的 Verdict，含附录 B.7 的症状）与 7 条长任务压缩样本。标注全部经人确认。

**评分器**：结构校验（schema 必须 100% 通过）、规则检查（风险上升必须走 full、write_irreversible 前必须有 gate……）、rubric 评分（用与被测模型不同家的模型打分）。

**防过拟合**：开发集与留出集分开，提示只在开发集上调，留出集只给汇总、记看过几次。真模型有随机性：`--repeat 3` 跑三遍，门槛按中位数判，单次报告都保留；
报告写出失败的模型调用次数（供应商额度用完时 Planner、Reviewer 悄悄回退、评测“照样跑完”，2026-10-22 遇到过）。

## 2. 门槛与当前的数字

| 套 | 门槛 | 结果（3 遍中位数） |
| --- | --- | --- |
| S-09 Planner | Triage ≥ 90%、schema 100%、规则违例 0、规划成本 ≤ 5% | Triage 88.5%（开发）/ 85.7%（留出）；规划成本最大一条 17%–37% ✗ |
| S-10 Reviewer | Verdict 准确率 ≥ 85%、该升级却原地重试 0 | 留出集 75%、1 次 ✗（开发集 50%） |
| S-11 压缩 | full 的 Probe 通过率与重复操作率不劣于 mask_only | 开发 96% 对 70%，留出 83% 对 50%，重复操作率都是 0 ✓ |

## 3. Learning Pipeline：从 trace 学一个 Recipe，过门槛才发布

```
读 trace（Collector 的 OTLP JSON 或控制台 Telemetry 的 JSON 行）：Task 级 invoke_workflow 与 Round 级 runtime.round
→ 只取 --tenant 的 span（tenant_id 由 Telemetry 绑定时加上，调用方伪造不了）；跨租户读取在平台作用域留审计（含读了哪些文件）
→ 归纳候选：该任务族、完整规划的成功 Task，按“做法签名”（各 Round 的 Stage 形状，只看结构）分组，成功次数最多且不少于 5 次的做成 Recipe
→ 观测式评测：候选组（同签名的全部 Task，含失败与中途失败的）对现行组（该任务族默认 Recipe 的 Task；没有就是全部完整规划的）
→ 非劣效门槛（J.3）：候选成功率的 Wilson 95% 下界 ≥ 现行成功率 − δ（2 个百分点）；
                      候选每次成功成本的 95% 上界 ≤ 现行的 ×（1 + ε）（5%）；每组至少 100 条
→ 过了才 Publish（租户作用域、影子）再带证据 Promote；没过不调 Registry 的任何写方法
```

- **每次成功成本** = 组内全部 Task 的成本 ÷ 成功数（失败的花费也算，与 H.2 一致；R-06 评审修过：之前只取成功 Task 的均值）。上界用比率估计量的 delta 方法，确定、不抽样。
- **选择偏差**：只看成功的 Task，成功率恒为 100%；候选组必须包括按这条路失败的。
- **失败归因**（J.2 的 MAST 三类：规格问题、协作失配、验证缺失）：小模型判断，提示只给结局、verdict 与 Stage 的结构（不给 Planner 写的文字），输出按 schema 完整校验，
  不合格记 unattributed；第一期只进报告，不反哺。
- **单独的预算**：一次运行一个预算，按网关返回的成本扣，用完就不再调模型；调用不带 task_id，不进任何 Task 的成本。
- 部署：KEDA ScaledJob，同一租户同一任务族只许一个 Job 同时跑（两次并发时后一次的 Promote 覆盖前一次）。

还没做：从 Redpanda 消费、offline 工作流、影子流量、灰度与自动回滚、归因反哺、Recipe 统计、离线搜索（登记在风险文档 3.6 节）。

## 4. 面试速查

**Q：为什么用非劣效检验，而不是“比现行更好才发布”？**
A：学到的 Recipe 主要价值是省钱、省时间（跳过完整规划），只要成功率不明显变差、成本不变贵就值得用。非劣效检验正好表达“不比现行差超过 δ”；
用置信下界与上界，而不是点估计，避免样本小时被运气骗。

**Q：为什么每组至少 100 条？**
A：样本小时置信区间很宽，Wilson 下界会很低，门槛自然过不了；硬性要求样本量，避免“两组都很小、差异不显著”被当成没有变差。

**Q：只看成功的 Task 学 Recipe 有什么问题？**
A：选择偏差：走这条路失败的 Task 被丢掉，成功率看起来是 100%。候选组要包括同样做法的全部 Task，以及走到一半就失败的。

**Q：每次成功成本为什么要算上失败的？**
A：失败也花钱。成功的都便宜、失败的很贵的做法，只看成功 Task 的均值会显得很便宜。要用总成本除以成功数，这也是路由选模型时的同一个口径。

**Q：评测里怎么防止被模型的随机性误导？**
A：每个门槛跑三遍取中位数，报告保留单次结果与范围；单次差一两题不下结论；统计模型调用失败，避免回退路径让结果看起来正常。

**Q：Learning Pipeline 会不会影响正在跑的 Task？**
A：它只经 Registry 发布，新版本先是影子，晋升后只有之后提交的 Task 固定到它。现在还有一个缺口：Planner 每轮列 Recipe 取的是当前默认版本，已登记。
