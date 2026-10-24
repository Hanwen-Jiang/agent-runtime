# R-01 评审：Policy Engine 与默认 Hook

> 评审对象：`52daef8`（T-09）里的 `services/py-worker/src/agentruntime_worker/policy`、Stage Runner 与 Task 工作流里 Hook 的调用点、
> 按 Registry 核对 effect 的路径，以及之后改到这些路径的提交（`9121d32` T-11b、`db72b28` S-04 缺陷修复）。行号按 `db72b28`。
> 依据：实施计划第 5 节 R-01 的评审重点；设计书 3.2.6、附录 D.4、F.1–F.3；细化 `docs/plan/policy-engine.md`；测试结果。按 6.2 的模板，不看实现过程。
> 评审日期 2026-10-24。
>
> **独立性**：评审在新会话里做，没看实现过程；但评审者与实现者是同一家的模型（Claude），不满足实施计划 1.3 第 2 条对强档实现的
> “另一家的强模型或人”。T-09 在计划第 7 节是中档实现、强档评审，按 1.3 第 2 条第 2 款“中档写的由强档以高推理来审”，这份评审够格；
> 实际实现用的是强档模型，严格的写审分离还要另一家的强模型或人复审（同 R-03、R-04、R-05）。

## 测试结果（评审时）

- `services/py-worker` 下 `test_policy_engine.py`、`test_policy_workflow.py`、`test_effect_matrix.py`、`test_registry.py`、
  `test_stage_replay.py`、`test_patches_at_start.py`、`test_tool_loop_approval.py`：124 项通过（不跳过）。
- 没有一条测试覆盖：tool_loop 里每次工具调用的 Hook、执行失败的 Node 的审计、`run_hooks` 这个 local activity 本身失败（不是 Hook 抛异常）、
  预算里算不算 tool_loop 与 Planner、Reviewer 的花费。下面 1.1、1.2、2.2 的结论是读代码得出的。

## 1. 评审重点逐条

### 1.1 write_irreversible 的审批插入不可绕过

| 路径 | 结论 | 位置与说明 |
| --- | --- | --- |
| Atomic Node | **已处理** | 审批是 Stage Runner 的代码路径，不是 Hook：`stage_runner.py:532` `_dispatch` 遇到 write_irreversible 先 `_approval`（`:674`），拿不到通过的 GateRecord 就 denied，超时按拒绝。Hook 的 modify 只改 inputs 与 result（`:505`、`:516`），改不了 effect；审批在 before_node Hook 与输入改写之后，问的是最终要执行的 Node，幂等键按最终输入由代码生成（`:546`）。`test_irreversible_approval_is_not_bypassed_by_hooks`。 |
| effect 写低 | **已处理（接了 Registry）／兜底（没接）** | 接了 Registry 时 `_registered`（`:377`）在 Hook 与审批之前核对登记的模板，写低、executor 不符、没登记都 denied。没接 Registry 时信 Planner 写的 effect；兜底是 Node Executor：program 节点调用的工具 effect 高于 Node 声明的就拒绝（`node_executor.py:486`），write_irreversible 没有 `<id>.1` 的通过记录也拒绝（`:405`）。model 节点没有外部副作用。所以写低最多让审批少问一次、调用被 Executor 拒掉，不会悄悄写出去。 |
| quality 包装 | **已处理** | write_irreversible 与写操作的 Atomic Node 不能挂 quality（`stage_plan.py:161`），候选复制不会绕开审批或多写。 |
| 展开与子工作流 | **已处理** | Recipe 展开出的 Node 走同一个 `_run_node`；SubgraphWorkflow、SubAgentWorkflow 没有人工审批通道（`human=None`），write_irreversible 一律 denied（`:676`）。 |
| tool_loop | **已处理（审批）** | 每次调用 write_irreversible 工具前逐次审批（`:1014`），没登记的工具按 write_irreversible（`:949`）。但见 1.2：这里的工具调用不经过任何 Hook。 |
| 审批的内容 | **风险** | Atomic Node 的审批问题只有 Node id 与模板名（`:680`），不带输入；tool_loop 的审批带参数（`:1062`）。审批人看不到要写什么，审批形同“确认一下”。 |

### 1.2 deny 不重试

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| Hook 拒绝的 Node | **已处理** | `_guarded`（`:502`）before_node deny 即返回 denied，不调执行器；Reviewer 对 denied 判 replan，不原地重试（`reviewer/rules.py:108`），一直被拒受 Reviewer 各级次数上限约束。`test_a_node_denied_by_a_hook_is_neither_executed_nor_retried`、`test_executor_never_sees_a_denied_node`。 |
| Executor 判的 denied | **已处理** | Node Executor 的 denied 是 non_retryable 的 ApplicationError（`node_executor.py:138`），activity 不重试。 |
| `run_hooks` 本身失败 | **未处理** | 设计与细化都说“Hook 执行不了按拒绝”（`stage_runner.py:704`、`task.py:453`），但两处 `execute_local_activity` 都没给 retry_policy，也没有 schedule_to_close：Temporal 的默认是无限重试，`except FailureError` 那一支在持续失败时永远走不到。能让它持续失败的现成路径：设了 `OTEL_EXPORTER_OTLP_ENDPOINT` 时 Policy Engine 用 OpenTelemetry 实现（`worker.py:135`），`_record` 末尾的 `flush()` 在 Collector 不可用时抛 TransientError（`agentruntime_core/adapters/otel.py:236`）。每个 Node 的 after_node 都有 `audit_log`，都要写记录：**Collector 一停，所有 Task 卡在第一个 Node 的 after_node 上**，副作用已经发生，卡住也拦不住什么。 |
| tool_loop 的单次工具调用 | **未处理** | tool_loop 里的工具调用直接执行 `call_tool`（`:1026`），不经过 before_node、after_node：没有 budget_check、幂等键检查、审计、trace_result、pii_redaction。设计书 F.1 把 PreToolUse、PostToolUse、before/after_tool 对应到 before_node/after_node；现在只有整个 Goal Node 前后各一次，而 tool_loop 是真实任务里调用工具最多的地方。 |

### 1.3 驳回类 Hook 有次数上限

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| before_finish | **已处理** | `task.py:368`：驳回第 1、2 次回 Planner（带驳回理由），第 3 次起问人（`MAX_FINISH_REJECTIONS = 2`，`:67`），三种回复各有去向；问人没回复按停止。之后每次 done 被驳回都问人，不会自动循环。`test_finish_rejections_are_capped_then_a_human_decides`。 |
| Node、Stage 级 deny | **已处理** | 由 Reviewer 的次数上限兜住（同 1.2）；after_stage deny 把整个 Stage 的结果改为 failed/denied（`stage_runner.py:287`），也归 Reviewer。 |
| tool_loop 里的审批拒绝 | **已处理** | 同一 Goal Node 被拒 2 次记为 denied（`MAX_TOOL_REJECTIONS`）。 |

### 1.4 Hook 里没有复杂推理

**已处理。** Hook 是纯函数（`policy/hooks.py`），只看 HookContext；引擎以外不碰 I/O；静态检查 `policy` 包不 import 模型与网络
（`test_the_policy_package_does_not_call_models_or_the_network`）；同输入同结果、一千次在秒级以内（`test_results_are_deterministic`、`test_hooks_are_cheap`）。
`acceptance_recheck` 用 jsonschema 校验交付物，开销与交付物大小成正比，可以接受。

## 2. 其他风险

### 2.1 确定性

- Hook 在 local activity 里执行，结果作为 marker 记进历史；新增命令都在 `t09-policy-engine` 之后，并且按 `db72b28` 在第一个工作流任务里问完
  （`patches.AT_START`），Hook 运行时来了查询也能回放。新旧 golden 都回放通过。
- `_asked_templates` 是 set，只做成员判断，不依赖遍历顺序；`_merged` 按出现顺序。没有发现确定性风险。

### 2.2 预算（附录 F.3“所有 Node：预算与配额检查”）

**`budget_check` 看到的“已花费”少算了大头。** 已花费 = 之前各 Round 的 `cost_usd` + 本 Stage 到现在的 `spent_usd`（`stage_runner.py:364`、`task.py:637`），
而 `spent_usd` 只在 Atomic Node 的 `_execute` 里累加（`stage_runner.py:573`）。不算进去的：
- tool_loop 每一步的模型调用（`tool_loop_step` 的成本）——真实长任务里最大的一块；
- Planner 与 Reviewer 的调用（真模型评测里规划成本是 Task 成本的 7% 到 37%，TMP 计划 T-03）；
- SubgraphWorkflow、SubAgentWorkflow 里的花费：子工作流的 `RunContext` 没有 `budget_usd`（`:1220`），批量子图里 `budget_check` 一律放行，花费也不回到父 Stage。

后果：设了预算的 Task 在 tool_loop 里可以远超预算，`budget_check` 照样放行。Reviewer 的预算信号（`reviewer/signals.py`）用的是同一个数。

### 2.3 并发

- 写者串行化：`single_writer_lock` 检查的 `write_serialized` 是 Stage Runner 自己算出来交给 Hook 的（`:497`），Hook 只是复述代码的判断，
  真正的保证是 `_writer`。不算缺陷，但这条 Hook 拦不住 Stage Runner 自己的错。
- 惰性的 Catalog 查询（`_registered` 里对没预取的模板再查一次）有竞态的可能：两个并行 Node 用同一个没预取的模板时，后到的一个看到“已经问过”
  却还没有结果，会被判 `template_not_registered`。目前所有路径（Stage 开始、Recipe 展开、批量子图）都先整体预取，quality 的副本与 judge
  用的模板也在 Stage 开始时取过，实际走不到；以后加新的展开路径时要记得预取。
- Recipe 展开与 tool_loop 的工具检查里调用 `_ensure_catalog_for`（`:885`、`:948`）没有接住 FailureError：Catalog 在这里取不到时整个 Task 工作流失败，
  而不是像 Stage 开始时那样把 Node 记为 denied 交给 Reviewer。仍是 fail-closed（什么都不执行），但去向与细化第 2 节第 5 条不一致。

### 2.4 租户隔离

**已处理。** HookCall 带工作流输入里的租户（服务端确定）；租户策略与 Recipe 按 Task 固定的版本、按租户从 Registry 读，取不到按拒绝；
幂等键按 `ctx.tenant.tenant_id` 算；审计记录按租户 bind 的 Telemetry 写。没有发现由模型或请求决定租户的路径。

### 2.5 审计

- 审计只覆盖成功的 Node：`_guarded` 在结果不是 succeeded 时直接返回（`:511`），after_node 的 `audit_log` 不执行。失败、unknown（写操作结果未知）、
  审批被拒的 Node 没有审计记录；Hook 的 deny 另有 `policy.decision` 记录。附录 F.3 说“所有 Node：审计日志”。
- 审计记录写 Telemetry（开发是标准错误），没有持久的审计存储；Telemetry 丢弃或采样时审计就丢了。生产要单独落库，这里登记。

## 3. 与设计书不一致的地方

1. tool_loop 的单次工具调用不经过 before_node、after_node（F.1 的 PreToolUse、PostToolUse）。见 1.2。
2. 失败的 Node 没有审计（F.3“所有 Node”）。见 2.5。
3. 预算检查不算 tool_loop、Planner、Reviewer 与子工作流的花费（F.3“预算与配额检查”）。见 2.2。
4. 设计书 3.2.6 说 deny“交给 Reviewer 处理”；Task 级挂载点（on_task_received、on_task_start）deny 时 Task 直接 abort，不经 Reviewer。
   这时还没有 Stage，细化第 6 节第 2 条已经定下，算有意的偏离。
5. Planner 写在 Stage 里的 `hooks` 照常执行；设计书说 Hook“Planner 不手写”。细化第 6 节第 4 条已定，留到 S-09 的提示。
6. 未接的部分（细化第 2 节“不做”已登记）：before_compact、提示注入检测、Task 级挂载点的 modify 与 inject。

## 4. 处理建议

**本期修**（小，不修会让“审批不可绕过”“deny 不重试”的结论失真）：

1. `run_hooks` 的两处 local activity 给一个有限的 retry_policy（例如最多 3 次）或 schedule_to_close，让“执行不了按拒绝”真的走得到；
   Policy Engine 写 Telemetry 失败不让整个 activity 失败（记一条日志，Hook 结果照常返回），审计的可靠性另由 2.5 的持久审计解决。
   改了 local activity 的选项不改变命令序列（同一个 marker），不需要版本分支，但要补一条“Telemetry 不可用时 Task 不卡住”的测试。
2. Atomic Node 的审批问题带上输入（截断，与 tool_loop 一样用规范化 JSON），审批人看得到要写什么。改的是 activity 的输入，不改变命令序列。
3. 失败、unknown、审批被拒的 Node 也执行 `audit_log`（只有审计，不跑 after_node 的其他 Hook），结果带上 status。这会多一次 local activity，
   改变命令序列，要走版本分支（加进 `patches.AT_START` 末尾）并补 golden。

**要人定方案**（动 Task Loop 的数据流或核心链路）：

4. 预算口径：tool_loop 每一步的成本累加进 `spent_usd`；Planner、Reviewer 的成本是否计入 Task 的预算（设计书 1.1 的“每次成功成本”是算的）；
   子工作流把花费带回父 Stage、并带上 `budget_usd`。都改变数据流，第 1 项还会让 tool_loop 中途被 budget_check 拦下（要不要在 tool_loop 每一步前查）。
5. tool_loop 的单次工具调用要不要经过 before_node、after_node：经过的话每次调用多两次 local activity（命令序列变，要版本分支），
   tool_loop 的事件历史会明显变长；或者只在工具调用前查预算、调用后写审计，其余 Hook 仍按 Goal Node 一次。

**登记到后续**（写进 `docs/reference/risks-and-open-questions.md`）：持久审计存储；Recipe 展开与工具检查时 Catalog 取不到的去向（2.3）；
惰性 Catalog 查询的竞态（2.3，目前走不到）；`single_writer_lock` 只是复述 Stage Runner 的判断。

人定方案（2026-10-24）：第 1 到 3 条修；第 4 条 Task 的成本与预算全部计入；第 5 条（tool_loop 单次工具调用经 Hook）登记。

## 5. 另一家复审（2026-10-24）

OpenAI gpt-6-sol 按 6.2 的模板复审，原文与材料包见 [reviews/R-01-gpt-6-sol.md](reviews/R-01-gpt-6-sol.md)。满足写审分离，R-01 算评审完成。

- **与本评审一致**：审批在 Stage Runner 的代码路径上、Hook 改不了 effect、按 Registry 核对 effect、deny 不重试、before_finish 的上限、
  Hook 是纯函数、确定性。它没有看到 `run_hooks` 的重试上限与预算口径的问题，这两条以本评审为准。
- **新提出、采纳并在这一轮修**：
  1. after_node 上 `pii_redaction` 的改写要等全部 Hook 跑完才作用，`trace_result` 先把原始结果写进了 trace。改为 Policy Engine 把前一个 Hook 的
     modify 作用到 subject 上再交给下一个，after_node 上策略声明的 Hook 排在默认的前面（`pii_redaction`、`trace_result`、`audit_log`）。
  2. Hook 的结果不按 `hook_result.schema.json` 校验，不认识的 decision 会被当成放行。改为不认识或不完整的结果（含用不上的 patch）一律 deny，Stage Runner 也兜底。
  3. `trace_result` 的 8 KiB 按字符算。改为按 UTF-8 字节、在字符边界上截断。
- **判“不确定”、已核对**：`human`、quality 路径能否绕过审批——写操作的 Atomic Node 与 write_irreversible 的 Node 不能挂 quality（`stage_plan.py:161`），
  human 节点只发问、不写外部；普通 Node 的 deny 有 Reviewer 的上限（`reviewer/rules.py:108`，判 replan，连续 replan 2 次升级）；
  Subgraph 子工作流里 Catalog 取不到时子工作流失败，父 Stage 把它记为失败（本评审 2.3 已登记）。
- **新提出、登记**：并行的 Node 读同一份预算快照、都可能通过 `budget_check`（要预留预算）；`single_writer_lock` 只在一个 Stage Runner 里有效，
  跨 Session 写同一份共享产物没有分布式锁；`checkpoint` 只记模板名、不记回滚方式的描述；Node Executor 只核对 GateRecord 的字段、不验证它的来源
  （activity 只由工作流在内部队列上调用，属于纵深防御）；扩展 Hook 的 reason 可能带数据。
- **不采纳**：`before_stage` 没有自动插入 `budget_check`。附录 F.3 的“预算与配额检查”是对所有 Node 的，Stage 的预算检查由策略声明（细化第 2 节第 2 条）。

## 6. 处理结果（2026-10-24）

验收测试先行（`tests/test_policy_engine.py`、`tests/test_policy_workflow.py`、`tests/test_cost_and_vendor.py` 新增的用例）：

1. `run_hooks` 的两处 local activity 用 `timeouts.HOOK_RETRY`（最多 3 次）；用完按“Hook 执行失败”拒绝，Task 不再无限重试。Policy Engine 写 Telemetry
   失败只记日志、Hook 结果照常返回（Collector 不可用时 Task 不卡）。改 local activity 的重试策略不改变命令序列。
2. write_irreversible 的审批问题带上输入（规范化 JSON，超过 2000 字符截断）。
3. 没成功的 Node 也执行 `audit_log`（只审计），subject 带结局与失败类别，记录里有 `status`、`failure_class`，错误说明不进审计。
4. 成本口径：tool_loop 每一步（含 Context Builder 的摘要与 Probe）、Planner、Reviewer、批量子图与 sub_agent 的花费都计入 Round 的成本；
   这一轮的规划花费在 Stage 执行前就算进已花的钱；tool_loop 每一步之前查 Task 的预算，用完就停、记为 denied 交给 Reviewer；
   子工作流带着 Task 的预算。sub_task（子 Task）的花费还带不回来（TaskWorkflowResult 没有成本字段，要改 proto），登记。
5. 另一家复审的三条（见第 5 节）。

第 3 条与第 4 条里“每一步之前查预算”改变命令序列，走新的版本分支 `r01-audit-and-budget`（追加在 `patches.AT_START` 末尾），新 golden
`r01_failed_node_audited`、`r01_tool_loop_budget`；之前的 golden 都没有这个标记，按旧路径回放。py-worker 837 项通过。
