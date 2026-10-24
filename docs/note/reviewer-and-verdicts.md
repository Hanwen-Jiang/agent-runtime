# Reviewer 与 Verdict：硬信号、规则、Progress Check 与收尾写回

> 学习笔记，2026-10-24。对应代码：`services/py-worker/src/agentruntime_worker/reviewer/`（`signals.py` 台账与硬信号、`rules.py` floor 与 enforce、
> `prompts.py` Progress Check 的材料与提示、`reviewer.py` 主流程、`summary.py` 汇总、`wrap_up.py` 收尾写回、`activities.py`）；
> Task 工作流里的 Verdict 去向 `workflows/verdicts.py` 与 `workflows/task.py` 的 `_rounds`。设计依据：设计书 3.2.5，附录 B.4–B.9；实施计划 S-10。

## 1. Reviewer 判什么

每个 Round 末尾调一次 Reviewer（decision activity `review_round`），给出一个 **Verdict**（附录 A 的八种）：

| Verdict | 意思 | Task 工作流接下来 |
| --- | --- | --- |
| done | 验收条件满足 | before_finish 复核，过了就交付 |
| continue | 进展正常，下一步 | 下一轮回 Planner |
| repair | 局部出错 | 原地重试（带错误重跑出问题的 Node，不经 Planner），或换方法（回 Planner） |
| replan | 计划依赖的假设不成立 | 回 Planner 重做剩余部分，保留已验证的 Node |
| rollback | 状态被弄坏了 | 恢复到某个 Round 的检查点（Task State 的快照） |
| reroute | 应该换一个 Recipe | 固定新的 Recipe，下一轮按它走 |
| escalate | 自己解决不了 | 问人（三种回复），或换更强的 Tier 原地重跑 |
| abort | 做不下去了 | 停止，用现有结果收尾 |

**输入只有**：本轮的 Stage 结果（含本轮执行的 Stage）、Task State、验收条件与之前各 Round 的摘要。不传 Planner 的规划过程——在代码里限制传什么
（`prompts.review_view` 按白名单取材料），不靠提示约束。

## 2. 规则先于模型

```
台账与硬信号（signals.ledger）：每个 Node 失败了几次、上次进展以来 replan 了几次、花了多少钱、预算用掉多少
→ floor（rules.floor）：规则判定的最低 Verdict
     同一 Node 失败 2 次 → 至少换方法；3 次 → replan
     有 Node 被拒绝（denied）→ replan（原样重试没用）
     replan 连续 2 次没有进展 → 升级
     预算用掉 80% 而完成度不到 50% → 缩小范围或问人
→ Progress Check（模型，强档，独立上下文）：给出 Verdict 的提议、完成度与要写进 Task State 的汇总
→ enforce（rules.enforce）：提议里的细节改成合法的；低于 floor 的抬上去；某一级的次数用满了就升一级（防震荡）
```

- Triage 为 fast 的 Round 不调用模型，只按规则判定。
- Progress Check 输出不合格时带上问题重问一次；还不合格或调用失败，就只按规则判定（`fallback`）。
- **完成看产出物**：done 必须指向本轮成功的 deliver 节点；Task 级 before_finish Hook（`acceptance_recheck`）还可以对照 output_schema 复核。
- **judge 用不同家的模型**（附录 H.3，2026-10-24 起）：Node 结果带着实际服务它的 vendor，Reviewer 调用时 `avoid_vendor` 填生成者里最多的那一家。
  T-13 的真模型端到端暴露了一个适配层问题：落到 gpt-5.5 之后每次都是 bad_response（strict 模式不接受 Reviewer 的 schema，聚合网关把拒绝包成 200 的 SSE），已修。

## 3. 问人与防震荡

- escalate 问人时回复限定三种：原样继续（continue_as_is）、带指导重规划（replan_with_guidance）、停止并收尾（stop_and_wrap_up）。问人没有回复按停止处理。
- 每一级升级（原地重试、换方法、replan、换 Tier、问人）都有次数上限；用满了升一级。震荡测试：Stage 永远失败、模型永远说“原地重试”，Task 在上限之内收尾。
- before_finish 驳回最多 2 次，第 3 次问人。

## 4. 收尾写回（finalize_task）

交付先通知 Session，写回再异步做，不阻塞交付：

- Task State 归档进 ObjectStore（`tasks/<task_id>/task_state.json`）。
- 成功或失败的经验、本次 Recipe 的效果作为候选交给 Memory Service（进它的写入流水线，安全过滤与 Promotion 在那边）。
- 成本、升级次数、各 Verdict 的分布写进 trace：`invoke_workflow`（Task）与 `runtime.round`（每个 Round，带 Stage 形状），Learning Pipeline 从这里读。

## 5. 评测（S-10）

Reviewer 样本是给定 Stage 结果与 Task State、标注正确 Verdict 的案例，包括附录 B.7 的典型症状（形状不对、同类错误反复出现、假设被推翻……）。
门槛：Verdict 准确率 ≥ 85%，且“该升级却原地重试”为 0。真模型结果（3 遍中位数）：

| | 开发集（2026-10-22） | 留出集（2026-10-24 重跑） |
| --- | --- | --- |
| Reviewer 样本 Verdict 准确率 | 50.0% | 75.0% |
| 该升级却原地重试 | 2 次 | 1 次 |
| 判定点（Task 用例里的 Round）Verdict 准确率 | 92.3% | 93.8% |

问题集中在症状类样本：该 reroute 判 continue、该 escalate 判 continue、该 replan 判 repair、该 rollback 判 repair。次数类规则已经由代码判定，
症状类（“同类错误反复出现”“假设被推翻”）还靠模型——下一步是把这些症状写进规则。

## 6. 面试速查

**Q：为什么 Reviewer 不能看 Planner 的思考过程？**
A：看了就容易被计划的理由说服，变成“计划说得通，所以执行一定对”。只看产出物、状态和验收条件，判断更独立；这也让 Reviewer 可以单独评测。

**Q：为什么不让模型直接决定 Verdict？**
A：有些规则不能有例外：同一个节点失败三次还原地重试就是在烧钱，被策略拒绝的节点重试也没用。这些由代码判定成 floor，模型的提议低于 floor 就抬上去，
用满次数就升级。模型负责代码判断不了的部分：进展如何、假设还成不成立、该换什么做法。

**Q：怎么防止 repair 和 replan 来回震荡？**
A：每一级升级都有次数上限，并记在台账里；用满了强制升一级，最终到问人或停止。有专门的震荡测试。

**Q：Reviewer 自己出错（模型挂了、输出不合格）怎么办？**
A：重问一次；还不行就只按规则判定，结果仍合法，但质量会下降——评测报告会写出模型调用失败的次数，避免“看起来跑完了”的假结果。

**Q：Reviewer 和生成者用同一家模型有什么问题？**
A：同源模型的偏好一致，容易认可自己家生成的结果（自我偏好）。所以按结果实际服务的供应商避开同一家；只剩一家时网关仍会选它，但在计量和日志里记下“没避开”。

**Q：收尾为什么异步？**
A：用户只关心交付；归档、提交记忆候选、写 trace 都不影响结果，放在交付之后做，失败也只记录，不影响 Task 的结局。
