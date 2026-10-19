# Schema 正例与反例

S-01 的验收样例。每个 schema 一个文件：`<schema>.examples.json`，内含：

- `valid`：正例，必须通过校验。`source` 注明出处，`notes` 记录转写时补全或改动的地方。
- `invalid`：反例，必须校验失败。`violates` 列出预期的出错位置（JSON Pointer），可选给出 JSON Schema 关键字，用来确认反例失败的原因正是它想测的规则，而不是别的笔误。

校验由 `tests/contract/test_schema_examples.py` 执行。

## 附录 YAML 的转写

计划 S-01 要求把附录 B、C、D、I 中的 YAML 示例都转成正例。对应关系：

| 附录 | 正例 |
| --- | --- |
| B.1 Task State | `task_state` / `appendix_b1_task_state` |
| B.2 Stage | `stage` / `appendix_b2_stage`；其中两个节点另见 `goal_node` / `appendix_b2_read_tickets`、`atomic_node` / `appendix_b2_analyze` |
| C.3 Task Plan | `task_plan` / `appendix_c3_task_plan` |
| D.1 Atomic Node | `atomic_node` / `appendix_d1_q_region` |
| D.2 Node Output | `node_output` / `appendix_d2_node_output` |
| I.3 Memory Item | `memory_item` / `appendix_i3_memory_item` |

附录 B.4（Verdict 判定表）与 F.2（Hook 的结果）没有 YAML，`verdict` 与 `hook_result` 的正例按表格内容编写。B.3 的六类任务与图 34 的三个 Round 也写成了 Stage 正例。

附录的 YAML 是示意，有占位符和简写。转写时的改动：

| 位置 | 原文 | 转写 | 原因 |
| --- | --- | --- | --- |
| B.1、C.3、I.3 的 ID | `tsk_01J...`、`mem_01H...`、`ses_42`、`tr_88` | 完整 ID：`tsk_`/`ses_`/`mem_` 加 26 位 ULID，`tr_` 加 32 位十六进制 | 占位符无法校验格式 |
| B.1 `pinned.node_templates` | `by_stage` | 模板名 → 版本的映射 | 占位值；版本清单要能逐项固定（设计书 3.1.2） |
| B.2 `read_tickets.output_schema` | `{reasons: [{name, share, examples}]}` | 等价的 JSON Schema | 简写不是合法的 JSON Schema 语义 |
| C.3 `context.profile` | `profile: research` | `work_mode: research` | 附录 A 中 profile 是 Context Zones 的分区名，一个词只指一样东西 |

## 需要人审的命名

附录 A 没有给出英文名、由本契约补充的取值与字段，评审时请确认：

| 位置 | 取值或字段 | 依据 |
| --- | --- | --- |
| Task Plan `horizon` | `full_dag`、`per_stage`、`explore` | 设计书 3.2.1 第 2 条 |
| Task Plan `context.work_mode` | `coding`、`research`、`conversation`、`writing`、`data_analysis`、`batch`、`long_horizon` | 附录 G.3 |
| Verdict `layer` | `execution`、`plan`、`shape`、`goal` | 附录 B.5 |
| Verdict `signals` | `repetition`、`stall`、`budget_burn`、`plan_drift`、`context_health`、`parallel_anomaly`、`sample_divergence` | 附录 B.6 |
| Verdict `progress_check` | `criteria_met`、`progress`、`looping`、`assumptions_hold`、`next_step` | 附录 B.6 的五个问题 |
| Verdict `escalate.options` | `continue_as_is`、`replan_with_guidance`、`stop_and_wrap_up` | 设计书 4.11、附录 B.8 |
| Verdict `repair.strategy` | `retry_with_error`、`change_method` | 附录 B.8 第 1、2 步 |
| quality 参数 | revise：`judge`、`max_rounds`；ensemble：`n`、`aggregate`（`vote`/`judge`）、`judge`；search：`expand_k`、`max_depth`、`beam_width`、`scorer` | 附录 E.2 |
| Task State `status`、Round `status` | Task：`queued`、`running`、`done`、`aborted`、`cancelled`、`failed`（与 proto 的 TaskStatus 一致）；Round：`planning`、`running`、`reviewing`、`completed` | 附录 B.1、B.9 |
| Node 状态 | 附录 D.6 的七个状态外加 `unknown`（附录 K.4：写操作结果未知） | 附录 D.6、K.4 |
| Memory Item `evidence.outcome` | `verified`、`failed` | 附录 I.3 |
| Hook 结果 `injection.zone` | Context Zones 五区 | 附录 A、G.1 |
