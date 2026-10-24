# Node Executor 与 effect 规则：幂等、对账、审批与故障注入

> 学习笔记，2026-10-24。对应代码：`services/py-worker/src/agentruntime_worker/executor/node_executor.py`（Node Executor，activity 侧）、
> `executor/keys.py`（幂等键与规范化哈希）、`workflows/stage_runner.py` 的 `_dispatch`、`_execute`、`_reconcile`、`_approval`（工作流侧）；
> 故障注入套件 `tests/e2e/faults/`、故障 Worker `tests/e2e/e2e_support/fault_worker.py`、外部 ledger `e2e_support/ledger.py`。
> 设计依据：设计书 3.2.4、3.6，附录 D（D.4 按 effect 的规则、D.6 失败分类）、K.4；实施计划 S-05、S-07。

## 1. effect：一个字段决定缓存、重试、幂等与审批

每个 Atomic Node 声明 `effect`，取值（附录 A）：`none`、`read`、`write_reversible`、`write_irreversible`。规则写死在代码里，不交给模型判断：

| effect | 缓存 | 自动重试 | 额外要求 |
| --- | --- | --- | --- |
| none | 可以 | 可以 | — |
| read | 可以，设 TTL | 可以 | 结果写入 trace（`trace_result` Hook） |
| write_reversible | 不缓存 | 仅幂等时 | 必须带幂等键并登记回滚方式；同一时刻只一个写者 |
| write_irreversible | 不缓存 | **不可以**（activity 最多 1 次） | 必须带幂等键；执行前必须有通过的人工审批 |

effect 从哪来：Planner 照 Node Template 抄；接了 Registry 时 Stage Runner 执行前再按登记的模板核对，**写低了直接拒绝**（不悄悄抬高，因为审批、幂等、缓存都由它决定）。
program 节点调用的工具，ToolDriver 登记的 effect 高于 Node 声明的，Node Executor 拒绝。

## 2. 幂等键

- 由 `tenant_id`、`task_id`、`node_id` 与**输入的规范化哈希**生成（`keys.idempotency_key`），**不含 attempt 编号**：activity 重试、工作流重放时不变。
- Planner 或模型写的幂等键不算数，Stage Runner 按最终输入（Hook 改写之后）重新生成；Node Executor 收到后再按同样的规则算一遍核对，不一致就拒绝。
- 外部系统按 Idempotency-Key 去重，或支持“按幂等键查询是否已生效”——这是对账的前提，在 ToolDriver 登记里声明（`supports_idempotency_lookup`）。

## 3. 结果未知（UNKNOWN）与对账

写操作 activity 超时、Worker 在“外部已生效、结果还没写进历史”时崩溃，都可能让结果未知。这时**不当作失败重试**：

```
写操作超时 / Node Executor 暂时查不到
→ reconcile_node：按幂等键问外部“这个键生效了吗”
   → 生效了：取回结果，当作成功（外部只生效一次）
   → 没生效且可以确认：交给 Reviewer
   → 查不了（工具不支持查询）：状态记为 unknown，交给 Reviewer，必要时问人
```

write_reversible 且外部按 Idempotency-Key 去重时，可以用同一个键安全地重试；write_irreversible 不重试，只对账。

## 4. 审批双保险

1. **Stage Runner**：write_irreversible 的 Node 前插入人工审批（gate，id 是 `<node_id>.1`），问题经 HumanChannel 发出，等人回复的信号，超时按拒绝。这是代码路径，不是 Hook，
   Hook 的结果改不掉它。审批问题带上要执行的输入（R-01 评审之后）。tool_loop 里每次调用不可逆工具前也逐次审批（D16），被拒 2 次就把整个 Goal Node 记为 denied。
2. **Node Executor 兜底**：没有通过的 GateRecord（gate 是 `<node_id>.1`、选项是 approve）就拒绝执行。

谁能审批：只有提交者，或带 `task.approve` scope 的用户（R-05 评审之后）。

## 5. 失败分类与重试策略

| 失败 | 怎么处理 |
| --- | --- |
| transient（超时、限流、依赖暂时不可用） | 可重试错误，activity 按退避重试 |
| invalid_output（model 节点输出不合 schema） | 带上校验错误重问一次 |
| denied、invalid_arguments 等 | 不可重试，直接交给 Reviewer |
| 写操作超时 | 先对账（第 3 节） |

**心跳**：长 activity 在后台持续发心跳，heartbeat 超时取 start-to-close 的 1/20（1 到 30 秒）。Worker 被杀后，超过心跳超时 activity 就被别的 Worker 接手。

## 6. 其他规则

- **缓存**：只缓存 none、read 且声明了 cache 的 Node；键 = 模板版本 + 输入哈希，按租户隔离。
- **大结果外置**：超过阈值的结果写进 ObjectStore，result 只留摘要与 `obj://<租户>/…` 路径（`meta.result_ref`）。
- **只传 result**：Node 输出三段（result、rationale、meta），rationale 只写 trace，不往下游传。
- **meta**：tier、tokens、cost_usd、latency_ms，以及 2026-10-24 加的 vendor（实际服务的供应商，Reviewer 据此避开同一家）。
- **凭据**：工具登记了 permissions 时，按角色签发短期凭据，调用结束吊销；凭据不进结果、日志与事件历史，工具回显的凭据换成掩码。

## 7. 故障注入套件（S-07）

“可恢复”不能靠嘴说，要可重复地测。十个场景（`tests/e2e/faults/test_fault_s<编号>_*.py`）：

1. Worker 在 model 节点执行中崩溃（2026-10-24 起另有经真 Model Gateway 的用例：上游请求挂起时杀 Worker）
2. 写操作已执行、结果未写入历史时崩溃
3. 写操作结果未知：外部已生效，响应丢失
4. 模型供应商持续失败，Model Gateway 同档切换（替身与真网关各两条用例）
5. human 节点等待期间 Worker 全部重启
6. 信号重复到达；同一 Task 重复提交
7. 发布新版本时有运行中的 Task（版本固定）
8. Session 工作流 Continue-As-New 时有信号到达
9. Cache 重启：SSE 续传与限流计数丢失
10. ObjectStore 暂时不可用：Claim-Check 的重试与报错

每个场景三类断言：**Task 的最终状态、外部副作用的次数（外部 ledger 记的，在 Worker 进程之外）、事件历史无异常**（`e2e_support/history.py`：
没有意外的 WorkflowTaskFailed、已完成的步骤不重新调度、结束时没有悬着的 activity，并且能用当前代码回放）。故障由故障计划注入（`faults.py`）：在指定位置
crash、hang、超时、丢响应。套件进夜间任务。

## 8. 面试速查

**Q：怎么保证写操作“恰好一次”？**
A：严格说做不到恰好一次执行，能做到的是“恰好一次生效”：幂等键不含 attempt，重试和重放时不变；外部按键去重；结果未知时先按键对账，不盲目重试；
不可逆写不自动重试。再用故障注入测试“已执行未记录”“响应丢失”这些窗口。

**Q：幂等键为什么不能含 attempt 编号？**
A：重试的意义就是“同一个操作再试一次”，键变了外部就当成新操作执行两次。键只由“谁、哪个 Task、哪个 Node、什么输入”决定。

**Q：activity 超时了，为什么不直接重试？**
A：超时只说明调用方没等到结果，不说明外部没执行。写操作直接重试可能执行两次。要先问外部这个幂等键生效没有；问不了就标记 unknown 交给 Reviewer 或人。

**Q：人工审批放在 Hook 里不行吗？**
A：Hook 的结果可以被策略改写，也可能因配置漏掉。审批是 Stage Runner 的固定代码路径，Node Executor 还会兜底检查审批记录，两道都过才执行。

**Q：Worker 崩溃后怎么知道哪些步骤做过？**
A：事件历史里记着每个 activity 的调度与完成。重放时已完成的直接拿历史里的结果，不再执行；正在跑的那个因心跳超时被重新调度。故障注入测试用外部 ledger 数副作用次数验证这一点。

**Q：怎么证明恢复测试本身是对的？**
A：外部副作用记在 Worker 进程之外，进程被杀也丢不了；断言事件历史没有意外失败、没有重新调度已完成的步骤；所有测试跑出来的历史都要能被当前代码回放。
