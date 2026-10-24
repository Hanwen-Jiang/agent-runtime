# Task Loop 与 Temporal：Session、Task 工作流与 Stage Runner

> 学习笔记，2026-10-24。对应代码：Go 的 Session 工作流 `services/go-task-service/internal/session/`；Python 的 Task 工作流
> `services/py-worker/src/agentruntime_worker/workflows/task.py`、Stage Runner `workflows/stage_runner.py`、展开与校验 `workflows/stage_plan.py`、
> 版本分支 `workflows/patches.py`、Verdict 去向 `workflows/verdicts.py`；重放工具 `agentruntime_worker.replay`、`task-service replay`。
> 设计依据：设计书 2.2、3.1.1、3.2.1、3.2.3、3.6，附录 B、E、K；实施计划 S-03、S-04。

## 1. 要解决的问题

一个 Task 可能跑几分钟到几天：中间要调模型、调工具、等人审批，Worker 随时可能被杀、被升级。要求是：

1. **不丢进度**：Worker 崩溃后从断点继续，已经做完的步骤不重做（尤其是写操作）。
2. **可审计、可回放**：每一步做了什么、为什么，都能从事件历史里还原。
3. **固定的循环**：不管什么任务，都按同一个 Task Loop 走，Planner 与 Reviewer 分开，方便评测与改进。

Temporal 解决 1 和 2：工作流代码的每个决定都记进事件历史，Worker 重启后“重放”历史恢复内存状态，再接着往下走。代价是工作流代码必须**确定**。

## 2. 两层工作流

| 工作流 | 语言 | 一个实例对应 | 做什么 |
| --- | --- | --- | --- |
| Session 工作流 | Go（task-service 里的 Session Worker） | 一个会话 | 收 Task 信号；会写 Session 状态的 Task 串行，只读 Task 并行（有上限）；事件历史过长时 Continue-As-New；推进度事件（`publish_task_event`） |
| Task 工作流 | Python（py-worker） | 一个 Task（工作流 ID 就是 task_id） | Round 循环：Planner → Stage → Reviewer → 按 Verdict 走；Task State 是它的状态，经 query 读 |
| SubAgentWorkflow、SubgraphWorkflow | Python | sub_agent 的独立上下文；大批量 template 的一批条目 | 控制单个工作流的事件历史长度 |

**为什么写 Task 串行**：同一个 Session 里两个写 Task 可能改同一份产物。把串行放在 Session 工作流里，比分布式锁可靠（锁会过期，工作流状态不会）。

**Continue-As-New 的坑**：续接前要把还没处理的信号排空带进新运行；续接之后，上一次运行启动的子工作流只能靠它发来的 `task_finished` 信号通知结束，
所以两条通知（future 与信号）都处理、按 task_id 去重。夜间任务里遇到过：续接时恰好收到重复的 task_finished，服务端以 UNHANDLED_COMMAND 拒绝这次结束命令，
工作流处理完信号后重试成功——这是正常行为，事件历史检查放行“重试成功的 UNHANDLED_COMMAND”。

## 3. Round 循环（Task 工作流）

```
加载 StagePolicy（租户策略与 Recipe 声明的 Hook）→ on_task_received、on_task_start Hook
循环（最多 20 个 Round）：
  round_started 事件
  Planner（decision activity）→ Stage（可能是 replan 后的剩余部分）
  Stage Runner 跑完 Stage → 每个顶层 Node 的 StageResult
  Reviewer（decision activity）→ Verdict
  按 Verdict 走：done（before_finish 复核，驳回最多 2 次后问人）/ continue / repair（原地重跑或换方法）/ replan /
              rollback（恢复检查点）/ reroute（换 Recipe）/ escalate（问人或换更强的 Tier）/ abort
收尾：先通知 Session（交付），再异步写回（finalize_task，不阻塞交付）
```

- **Reviewer 的输入只有** Stage 结果（含本轮执行的 Stage）、Task State 与验收条件，不传 Planner 的规划过程——在代码里限制，不靠提示。
- **Round 的成本**（2026-10-24 起）= 这一轮的规划 + Stage 里所有执行（含 tool_loop 每一步与子工作流）+ 评审。budget_check 看的“已花的钱”就是它。

## 4. Stage Runner：在工作流里跑一个 DAG

- **调度**：ready 集合排序后启动，结果按 Node id 合并；并发上限 `max_parallel`，超过的排队（`_slot`）。
- **单一写者**：会写的 Node 用工作流内的锁（`_writer`）串行，不用分布式锁。
- **展开**：quality（revise、ensemble、search）与 Goal Node（template、tool_loop、sub_agent、sub_task）。子图的 Node id 是“父 id + 序号”（`fix.1`、`fix.2`），
  重放与 trace 才对得上。写操作不能挂 quality（重做就是再写一次）。
- **tool_loop**：每一步一个 activity（`tool_loop_step`），工具调用另一个（`call_tool`）；有最大迭代次数，同一错误连续 3 次就打断；调用不可逆工具前逐次审批（D16）；
  2026-10-24 起每一步之前查 Task 的预算。
- **sub_task**：嵌套完整的 Task Loop，深度不超过 2；**sub_agent** 默认只读。
- **大批量**：template 逐条目展开超过内联上限时，按批拆成 SubgraphWorkflow 子工作流；会写的批次一个接一个跑。

## 5. 确定性：工作流代码的规矩

| 不能做 | 改用 |
| --- | --- |
| 读系统时间、`random`、`uuid4()` | `workflow.now()`、`workflow.random()`、`workflow.uuid4()` |
| I/O、调模型、调工具 | activity（Hook 是 local activity） |
| 依赖 set、map 的遍历顺序做决定 | 先排序 |
| Go 里 `time.Now`、goroutine、channel、select | `workflow.Now`、`workflow.Go`、`workflow.Channel`、`workflow.Selector` |

**改变命令序列的修改要加版本分支**：`workflow.patched("<id>")`（Go 是 `workflow.GetVersion`）。新运行在历史里记一个标记、走新路径；旧历史没有标记、按旧路径回放。
项目里的版本分支：`s04-stage-runner`、`s05-human-channel`、`s05-reconcile`、`d16-tool-loop-approval`、`d16-expect-reply-before-ask`、`s10-reviewer`、
`t09-policy-engine`、`t11b-progress-events`、`r01-audit-and-budget`。

**一个踩过的坑（S-04 缺陷修复）**：Hook 是 local activity。它正在跑时来了 Temporal 查询（客户端轮询 QueryTask），SDK 只带着 local activity 的 marker
先提交工作流任务，后面的代码落进下一个工作流任务；回放时结果却在上一个工作流任务里就交给工作流，紧接着第一次问的版本分支看不到下一个任务里的标记，走了旧路径，
回放失败。修法：所有版本分支定义在 `patches.py`，新运行在第一个工作流任务里、任何 local activity 之前按 `AT_START` 一次问完（只能在末尾追加），之后在原位置问
拿到的是同一个值。

## 6. 重放是硬门槛

- **golden 事件历史**（`services/py-worker/tests/histories/`、Session 的在 `internal/session/testdata/histories`）：每条展开路径、每个 Verdict 去向、每个版本分支都有一份，
  当前代码必须都能回放。`AR_UPDATE_GOLDEN_HISTORIES=1` 只补缺的、不覆盖已有的（已有的证明旧版本的历史还能回放）。
- **测试自己要有效**：把 Stage Runner 里标了 `replay-mutation:A/B`（C/D）的两行调换，重放必须失败。
- 测试会话里跑出来的每一份历史也都要能回放；版本标记的检查保证“新路径的历史有标记、旧的没有”。

## 7. 面试速查

**Q：为什么用 Temporal，而不是自己用队列加数据库做状态机？**
A：Agent 任务长、步骤多、外部副作用多，最难的是崩溃恢复与“恰好一次”的语义。Temporal 把每个决定记进事件历史、activity 有重试与心跳、定时器与信号是持久的，
Worker 挂了另一台接着跑。自己做要重新发明这些，而且很难测。

**Q：工作流代码为什么必须确定？**
A：恢复靠重放：用同一份事件历史重新执行工作流代码，代码每次产生的命令（调度 activity、启动子工作流、定时器）必须与历史一致。读了系统时间或随机数，重放时值不一样，
命令就对不上，SDK 报 nondeterminism。

**Q：上线新代码会不会让运行中的 Task 卡死？**
A：改变命令序列的修改都加版本分支，旧历史走旧路径；每次改动都用 golden 历史回放验证；新的版本分支在第一个工作流任务里一次问完，避免 local activity 与查询交错导致问的时机不同。

**Q：Planner 和 Reviewer 为什么要分开？**
A：同一个模型既规划又评审，容易为自己的计划找理由。分开之后 Reviewer 只看产出物、Task State 与验收条件，不看规划过程，判断更客观，也能分别评测、分别改进。

**Q：多个节点并行时，结果合并怎么保证确定？**
A：ready 集合排序后启动，结果按 Node id 排序后合并；子图 Node id 由“父 id + 序号”生成，不用随机 ID。

**Q：长时间等人审批，会不会占着 Worker？**
A：不会。问题经 HumanChannel 发出后，工作流用持久定时器等信号，Worker 不占线程；Worker 全部重启也不影响，回复到了工作流就继续（S-07 场景 5 验证过）。

**Q：子工作流什么时候用？**
A：事件历史有长度上限（约 5 万事件、50 MB），单个工作流里塞几千个节点会撑爆。sub_agent 要独立上下文、大批量 template 要分批，都放进子工作流；Session 工作流自己也会 Continue-As-New。
