# Policy Engine 与默认 Hook（中档工作项细化，TMP 计划 T-09，评审 R-01）

> 实施计划第 7 节阶段 2 的中档工作项“Policy Engine 与默认 Hook”（中 · 中，强档评审 R-01）。第 7 节说“细化另行编写”，这里是细化。
> 依据：设计书 3.2.6、附录 D.4、D.5、D.6、F.1–F.3、B.8；计划 S-04（Hook 调用点先用桩）、S-05（effect 规则与审批双保险）、R-01；
> `schemas/hook_result.schema.json`、`defs.schema.json` 的 `hook_mount`、`hook_set`；T-08 的 Registry（`docs/plan/registry.md`）。
> 状态：已实现（2026-10-23）。验收测试与第 6 节的取舍经人确认。

## 1. 现状

| 部分 | 现在 | T-09 之后 |
| --- | --- | --- |
| Hook 的执行 | `run_hooks` 是桩（`activities/stubs.py`），一律 allow | Worker 进程内的库 `agentruntime_worker.policy`：`PolicyEngine.run_hooks` 按名字执行确定、便宜的 Hook |
| Hook 的来源 | 按 effect 写死的默认 Hook（`stage_plan.default_node_hooks`）加 Stage 里写的 `hooks` | 再加租户策略与 Task 固定的 Recipe 声明的 `hook_set`（经 Registry，`load_stage_policy`） |
| 挂载点 | before_stage、after_stage、before_node、after_node | 另接 before_finish（带驳回次数上限）、on_task_received、on_task_start、on_task_end |
| effect 的来源 | Stage Runner 信 Planner 在 Atomic Node 上写的 executor、effect（Planner 规划时对照 Catalog 校验） | 接了 Registry 时，Stage Runner 执行前再按 Registry 登记的模板核对，写低了或 executor 不符就拒绝 |

## 2. 第一期范围

**做**

1. **Policy Engine**（`agentruntime_worker.policy`）：Hook 目录（名字 → 实现，每个 Hook 声明能挂的挂载点）；`run_hooks`（local activity，
   代替桩，Worker 默认就用它）按顺序执行，遇到第一个 deny 就停；结果合 `hook_result.schema.json`。名字不认识、挂错挂载点、
   Hook 抛异常，一律按 deny（fail-closed）；异常的原文不进结果（可能带数据），只记类型。每次 deny 都写审计记录。
2. **默认 Hook 的真实现**（附录 F.3）：

   | Hook | 挂载点 | 做什么 |
   | --- | --- | --- |
   | `budget_check` | before_node、before_stage | Task 预算已用完，或本 Node（`budget.max_cost_usd`，Goal Node 的 `budget.usd`）/ 本 Stage（`budget.usd`）的预算超过剩余，deny |
   | `idempotency_check` | before_node | 写操作：subject 里的幂等键必须等于按 tenant_id、task_id、node_id 与输入算出的值，否则 deny；非写操作 allow |
   | `single_writer_lock` | before_node | write_reversible：subject 说明 Stage Runner 没有串行化这个写者时 deny |
   | `trace_result` | after_node | read：把结果写进 trace（超过 8 KiB 截断），allow |
   | `checkpoint` | after_node | write_reversible：记一条检查点（Node、结果摘要、回滚模板），allow |
   | `audit_log` | after_node | 记审计：Node、模板、effect、role、输入与结果的哈希，不记原文；总在 after_node 的最后 |
   | `pii_redaction` | after_node | 结果里的邮箱、手机号、身份证号改成掩码（modify，JSON Patch）；没有就 allow |
   | `acceptance_recheck` | before_finish | 交付节点没成功、交付物为空、或不合交付节点的 output_schema，deny |

   前六个按 effect 自动插入（与现在的 `default_node_hooks` 相同）；后两个只在租户策略或 Recipe 声明时才挂。
3. **Hook 的来源**：`StagePolicy` 增加 `hooks`（挂载点 → Hook 名）。接了 Registry 时 `load_stage_policy` 取 Task 固定的租户策略
   版本与固定的 Recipe 的 `hooks`，按“租户策略、Recipe”的顺序合并去重；固定的版本取不到时不可重试地失败（设计书 3.1.2：租户策略
   取不到一律按拒绝处理）。没接 Registry 时仍是空的。每个挂载点上的顺序：默认 Hook、策略声明的 Hook、Stage 里写的 Hook；
   after_node 的 `audit_log` 永远最后。策略只能加 Hook，去不掉默认 Hook。
   **R-01 评审之后（2026-10-24）**：after_node 上策略声明的 Hook 排在默认的前面，前一个 Hook 的 modify 先作用到 subject 上再交给下一个
   （脱敏之后 trace_result 记的才是脱敏过的结果）；没成功的 Node 也执行 `audit_log`；run_hooks 有重试上限。见 `review-R-01-policy-engine.md` 第 6 节。
4. **挂载点**（都在 workflow.patched("t09-policy-engine") 之后，旧历史按原路径回放）：
   - Task 工作流在第一轮规划之前加载 `StagePolicy`（不再等第一个 Stage），交给各 Stage 共用。
   - on_task_received（subject：message、inputs）、on_task_start（subject：Task State）：只有 allow 与 deny；deny 时 Task 以 abort
     结束，不规划；modify、inject 在这两个挂载点第一期不支持，按 deny。
   - before_finish（subject：Reviewer 的 done 判定、交付节点、交付物、交付节点的 output_schema）：deny 时不收尾，把驳回理由交给下一轮
     的 Planner；同一 Task 驳回满 2 次后，第 3 次不再驳回而是问人（同 Reviewer 的 escalate：照现状收尾、带指导重规划、停止并收尾）。
   - on_task_end（subject：Task 的结局）：收尾时执行，结果只记录，deny 不改变结局。
   - before_compact 在 Context Builder 里（activity 内），第一期没接：策略在这里声明了 Hook 时，Task 在开始时就以 abort 结束
     （不悄悄跳过）。
   - Node 与 Stage 挂载点的 subject 增加预算（Task 的预算与已花费）；写操作的 before_node 增加幂等键与“写者已串行化”。
5. **按 Registry 核对 effect**（R-01“审批插入不可绕过”）：Stage Runner 在 Stage 开始时把所有 Atomic Node 的模板一并交给
   `resolve_catalog`（同一个 patch 之后）；Catalog 来自 Registry（`CatalogInfo.authoritative`）时，每个 Atomic Node 执行前：
   - executor 与登记的不同，deny；
   - 写的 effect 低于登记的，deny（不悄悄抬高：Planner 要按模板写，审批、幂等、缓存都由 effect 决定）；写高了照常执行（更保守）；
   - Catalog 里没有这个模板，deny；`resolve_catalog` 失败（重试用完或不可重试），本 Stage 的 Node 全部 deny，一个也不执行。
   拒绝都在 before_node Hook 与审批之前，被拒的 Node 不会问人、不会执行。没接 Registry（桩 Catalog）时不核对，端到端测试的默认不变。
6. **重跑 S-05 的 effect 规则测试**（`tests/test_effect_matrix.py`）改用真的 Policy Engine。

**不做（登记到后续）**

- 工具的 effect 仍由桩工具目录给出（一律 read），tool_loop 里“调用不可逆工具前逐次审批”取决于工具目录给的 effect：开发环境里这是一个
  缺口，由 T-10（ToolDriver）补上。
- 提示注入检测（附录 F.3“外部内容进入上下文”）：与 S-13 的投毒防护一起做；数据驻留与模型白名单由 Model Gateway 执行（T-06 已做白名单）。
- before_compact 挂载点；on_task_received 与 on_task_start 的 modify、inject。
- reroute 换 Recipe 后不重新加载 Recipe 的 Hook（用 Task 开始时固定的 Recipe）。
- Registry 发布租户策略、Recipe 时不校验 Hook 名是否存在（运行时按 deny 处理）。

## 3. 难点与防错

- **审批插入不可绕过**：write_irreversible 前的人工审批是 Stage Runner 的代码路径，不是 Hook，Hook 的结果改不掉它；modify 只改
  inputs 与 result，改不了 Node 的 effect；Planner 把 effect 写低时由第 5 条拦下；Node Executor 没有审批记录仍拒绝执行（S-05 兜底）。
- **deny 不重试**：被 Hook 拒绝的 Node 记为 denied，不调用执行器、不重试；Reviewer 按规则判 replan（`reviewer/rules.py`），
  一直被拒时受 Reviewer 各级次数上限约束，最终问人或停止（`test_reviewer_oscillation`）。
- **驳回类 Hook 有次数上限**：before_finish 驳回最多 2 次；Node 级的 deny 由 Reviewer 的上限兜住。
- **Hook 里没有复杂推理**：Hook 是纯函数加审计记录，不调模型、不做网络 I/O（静态检查 `policy` 包的 import）；同样的输入得到同样的结果；
  一千次默认 Hook 的执行在秒级以内。
- **fail-closed**：Hook 执行不了、名字不认识、挂错地方、策略取不到、Catalog 取不到，都按拒绝处理。
- **审计不泄露**：审计与 deny 记录只有 Node、模板、effect、哈希与理由，不含输入与结果的原文；`trace_result` 写结果原文是附录 D.4 的要求。
- **确定性**：Hook 在 local activity 里执行；工作流只看结果；新的命令（提前加载 StagePolicy、Task 级挂载点、多出来的 Catalog 查询）
  都在 patch 之后，旧的 golden 历史仍能回放，新路径有新的 golden。

## 4. 验收（测试已写，待人确认）

| 验收 | 测试 |
| --- | --- |
| Policy Engine：每个 Hook 的规则；结果合 schema；不认识的名字、挂错地方、抛异常都 deny；遇 deny 即停；确定、便宜、不调模型；deny 与审计记录不含原文 | `services/py-worker/tests/test_policy_engine.py` |
| 默认 Hook 与策略 Hook 经真实的 Task 工作流执行：顺序与合并、预算拒绝的 Node 不执行不重试、pii_redaction 改写交付物 | `tests/test_policy_workflow.py` |
| 按 Registry 核对 effect：写低、executor 不符、模板缺失、Catalog 失败都拒绝且不问人不执行；写高照常；桩 Catalog 不核对 | 同上 |
| write_irreversible 的审批不受 Hook 影响：Hook 全部 allow 时仍要问人，超时按拒绝 | 同上 |
| before_finish：驳回后下一轮看得到理由；驳回满 2 次后问人，三种回复各自的去向 | 同上 |
| Task 级挂载点：on_task_received、on_task_start 的 deny 让 Task 不规划就结束；modify、inject 按 deny；on_task_end 只记录；before_compact 声明了就拒绝；策略取不到就拒绝 | 同上 |
| Hook 的来源：Registry 里租户策略与 Recipe 的 hooks 合并进 StagePolicy；固定的版本取不到不可重试；Registry 不可用可重试 | `tests/test_registry.py` |
| 组装：Worker 默认用真的 Policy Engine；接了 Registry 时 load_stage_policy 读 Registry | `tests/test_worker_entry.py`、`tests/test_registry.py` |
| 重放：新路径的 golden 历史（effect 被拒、before_finish 驳回、Task 级挂载点）；旧 golden 仍能回放 | `tests/test_stage_replay.py` |
| S-05 的 effect 矩阵用真的 Policy Engine 重跑 | `tests/test_effect_matrix.py` |

## 5. 依赖

S-04（Hook 调用点）、S-05（effect 规则、审批）、S-10（Reviewer 的上限与问人）、T-08（Registry：租户策略、Recipe、Node Template）。

## 6. 写测试时定下的取舍（待人确认）

1. before_finish 驳回上限 2 次，第 3 次问人（不是直接 abort，也不是忽略驳回放行）。
2. on_task_received、on_task_start 第一期只认 allow 与 deny，modify、inject 按 deny；before_compact 声明了 Hook 就在开始时拒绝。
3. effect 写低一律拒绝，不自动抬高；写高照常执行。executor 不符也拒绝。
4. Stage 里 Planner 写的 `hooks` 照常执行（它只能多加检查）；设计书说 Hook“由 Recipe 与租户策略声明，Planner 不手写”，
   要不要让 Planner 的 Stage 不再带 hooks，留到 S-09 的提示里改。
5. Worker 默认就用真的 Policy Engine，不加开关。
