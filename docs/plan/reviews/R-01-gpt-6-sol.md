# R-01 复审（另一家的强模型：OpenAI gpt-6-sol，经 a6api）

> 2026-10-24。按实施计划 6.2 的模板，只给了工作项全文、设计书相关章节、非测试源码的 diff 与测试结果（材料包的组成见 [README.md](README.md)），
> 没有给之前同一家模型写的评审。下面是模型输出的原文，没有改动；采纳与处理见对应评审文档的“另一家复审”一节。

## 1. 逐条检查“评审重点”与“难点与防错”

### 1.1 评审重点

| 检查项 | 结论 | 依据与代码位置 |
|---|---|---|
| `write_irreversible` 的审批插入不可绕过 | **不确定** | 普通 Atomic Node 路径已插入审批：`StageRunner._dispatch()` 在 `write_irreversible` 分支调用 `_approval()`，审批通过后才调用 `_execute()`（`stage_runner.py:523–538`、`674–688`）；Node Executor 也要求存在 `gate` 且 `gate_node == <node_id>.1`（`executor/node_executor.py:398–408`）。但 `_dispatch()` 先处理 `quality_type != "none"` 和 `executor == "human"`（`stage_runner.py:525–532`），再判断 `write_irreversible`，如果这两类 Node 允许声明不可逆写，审批可被绕过；材料未给出 schema 是否禁止这种组合，还需查看 Node schema/Stage 校验规则。 |
| `deny` 不重试 | **已处理** | Policy Engine 对 Hook 异常、未知 Hook、挂载点错误返回成功的 `deny` 结果，而不是抛出可重试异常（`policy/__init__.py:PolicyEngine._evaluate()`）；Stage Runner 收到 Hook deny 后直接返回 `StageResult`，不调用执行器（`stage_runner.py:490–515`）。不可逆写的执行 activity 也固定 `attempts = 1`（`stage_runner.py:540–557`），tool loop 的写操作调用使用 `maximum_attempts=1`（`stage_runner.py:1024–1041`）。但是 after-node Hook 的 deny 发生在实际执行之后，后续 Reviewer 是否会再次执行该 Node，取决于未提供的 Reviewer 规则。 |
| 驳回类 Hook 有次数上限 | **不确定** | `before_finish` 明确限制为前两次驳回，第 3 次转人工（`task.py:368–383`，常量 `MAX_FINISH_REJECTIONS = 2`）；tool loop 中不可逆工具审批拒绝达到 2 次后记为 denied（`stage_runner.py:1014–1023`）。但普通 Node 级 Hook deny 依赖 Reviewer 的次数上限，材料没有提供 `reviewer/rules.py` 的实现，无法确认所有 Node 级循环都受上限约束。 |
| Hook 内没有复杂推理 | **已处理** | 默认 Hook 实现位于 `policy/hooks.py`，只执行预算、哈希、正则脱敏、JSON Schema 校验等确定性逻辑，没有模型调用或网络调用；`PolicyEngine._record()` 负责 Telemetry 写入，未放在 Hook 函数内部（`policy/hooks.py`、`policy/__init__.py:PolicyEngine._record()`）。测试结果也显示 Policy Engine 相关测试通过，但未见单独的静态 import 检查结果。 |

### 1.2 难点与防错

| 难点与防错 | 结论 | 依据与代码位置 |
|---|---|---|
| 审批插入是 Stage Runner 代码路径，不是可被 Hook 改写的结果 | **不确定** | 正常 Atomic 路径满足：Registry 核对在 Hook 和审批前执行（`stage_runner.py:459–469`），Hook 只能修改 inputs/result，不能修改 effect（`stage_runner.py:502–520`），审批在 `_dispatch()` 中直接执行（`stage_runner.py:532–537`）。但 `human`/`quality` 分支先于审批判断，是否构成实际可用的绕过路径需要查看 schema 约束。 |
| Planner 把 effect 写低时拒绝，不自动抬高 | **已处理** | Stage 开始时对 Atomic Node 的模板一并查询 Registry；`_registered()` 检查 executor、模板存在性以及 Node effect 是否低于 Registry 登记值（`stage_runner.py:294–328`、`_registered()`）。低于登记值直接返回 denied，不会执行或询问人工。 |
| Registry Catalog 缺失或查询失败时 fail-closed | **已处理** | `RegistryCatalog.resolve_catalog()` 将模板缺失、拒绝、非法模板转为不可重试错误，并标记 `authoritative=True`（`registry/catalog.py:RegistryCatalog.resolve_catalog()`）；Stage Runner 捕获 Catalog 失败后记录 `_catalog_error`，整个 Stage 的 Node 全部 denied（`stage_runner.py:_ensure_stage_catalog()`、`run()`）。但 SubgraphWorkflow 自身调用 `_ensure_catalog_for()` 时没有展示同样的错误转换路径，子图场景还需补看。 |
| 被 Hook 拒绝的 Node 不调用执行器、不自动重试 | **已处理** | `StageRunner._guarded()` 在 before-node Hook denied 后直接返回（`stage_runner.py:490–510`）；Policy Engine 遇到第一个 deny 即停止后续 Hook（`policy/__init__.py:PolicyEngine.run_hooks()`）。 |
| Reviewer 对持续 deny 有上限 | **不确定** | `before_finish` 和 tool-loop 审批有显式上限；普通 Node deny 的最终终止条件不在给出的代码中，需要查看 `reviewer/rules.py` 以及 `test_reviewer_oscillation` 的实际断言。 |
| Hook 执行异常、未知名字、错误挂载点均按 deny | **已处理** | `PolicyEngine._evaluate()` 对未知 Hook、挂载点不匹配和异常均返回 `deny()`，异常只记录异常类型（`policy/__init__.py:PolicyEngine._evaluate()`）。 |
| Hook 结果符合 schema，非法结果也 fail-closed | **未处理** | `PolicyEngine.run_hooks()` 没有对 Hook 返回值执行 `hook_result.schema.json` 校验（`policy/__init__.py:run_hooks()`）；如果某个 `HookSpec.run()` 返回未知 decision，Stage `_hooks()` 可能忽略它并继续执行（`stage_runner.py:690–717`）。当前内置 Hook 返回值是合法的，但缺少引擎级防护。 |
| 每次 deny 写审计记录，且异常原文不泄露 | **未处理** | deny 会追加 `policy.decision` 记录，异常原因只包含异常类型（`policy/__init__.py:run_hooks()`）。但 `pii_redaction` 的 output patch 要等全部 after-node Hook 执行完才应用，而 `trace_result` 可能已经记录原始结果（`stage_runner.py:513–520`、`policy/hooks.py:trace_result()`、`pii_redaction()`）；因此策略声明脱敏时，原始 PII 可能已进入 trace。另 `policy.decision` 记录没有模板、effect、输入/结果哈希，仅有 hook、mount、reason、node_id。 |
| 默认 Hook 按 effect 自动插入，策略只能增加且 audit 最后 | **不确定** | Node 级 Hook 合并逻辑明确保证默认 Hook 在前、策略 Hook 在后，并将 `audit_log` 强制放最后（`stage_runner.py: _node_hooks()`）。但 `sp.default_node_hooks()` 的实际实现不在材料中；另外 Stage 的 `before_stage` 路径没有显式加入默认 `budget_check`，只合并策略与 Stage 自声明 Hook（`stage_runner.py:run()`）。 |
| StagePolicy 按租户策略、Recipe 顺序合并并去重 | **已处理** | `RegistryStagePolicy.load_stage_policy()` 先加载固定租户策略，再加载固定 Recipe，并通过 `_merge()` 按顺序去重（`registry/catalog.py:RegistryStagePolicy.load_stage_policy()`、`_merge()`）。固定版本取不到时转为 non-retryable ApplicationError。 |
| Task 级挂载点按要求处理 | **已处理** | Task 开始时先加载 StagePolicy，再执行 `on_task_received`、`on_task_start`；deny 或 Hook 执行失败直接 abort，不规划（`task.py:407–463`）。`before_compact` 有声明时也直接拒绝（`task.py:424–426`）；`on_task_end` 的 deny 只记录、不改变结局（`task.py:_task_end()`）。 |
| before_finish 驳回最多两次，第 3 次问人 | **已处理** | `TaskWorkflow._rounds()` 在 Reviewer 返回 done 后执行 `_before_finish()`；前两次回 Planner，第 3 次进入 `_ask_human()`，符合计划中的取舍（`task.py:368–383`）。 |
| Temporal 重放确定性 | **已处理** | 新增的 `POLICY_PATCH`、Task 级挂载点、StagePolicy/Catalog 查询均在 patch 之后；`patches.at_start()` 在任何 local activity 前一次性记录全部 patch（`workflows/patches.py:at_start()`、`task.py:run()`、`stage_runner.py` 子工作流入口）。测试结果显示 golden 历史和 `patches_at_start` 回放通过，调换 replay-mutation 两行后回放失败，说明测试确实覆盖了命令序列变化。 |

---

## 2. 其他风险

### 2.1 确定性风险：Temporal 工作流重放

已提供的实现对主要重放风险处理得较好：

- `patches.at_start()` 在 Task、SubAgent、Subgraph 工作流入口先记录所有版本分支，避免 local activity 期间查询导致第一次 `workflow.patched()` 的位置不同（`workflows/patches.py:at_start()`）。
- 新的 Task 级 `LOAD_STAGE_POLICY`、`RUN_HOOKS`、Stage Catalog 逻辑均处于 `POLICY_PATCH` 分支之后。
- 测试结果明确显示 golden 历史回放通过，调换 replay-mutation 顺序后回放失败。

仍有两个需要补充验证的点：

1. `SubgraphWorkflow.run()` 先通过 `patches.at_start()`，随后直接调用 `_ensure_catalog_for(nodes)`；其 Catalog 失败是否始终转换为 StageResult denied，而不是让子工作流失败，材料未完全展示。
2. `StageRunner._registered()` 在运行时可能再次调用 `_ensure_catalog_for([node])`；需要确认所有可能进入该路径的 dynamic/template Node 都已经在同一 patch 语义下覆盖。

### 2.2 并发风险

1. **single-writer lock 只在一个 StageRunner 实例内有效。**  
   `_write_lock` 是 `StageRunner` 对象内的 `asyncio.Lock`（`stage_runner.py:StageRunner.__init__()`），只能串行化同一工作流实例内的 Node；SubgraphWorkflow、SubAgentWorkflow 或不同 Task 工作流之间没有共享锁。若它们写入同一份共享产物，`single_writer_lock` 看到的 `write_serialized` 仍可能为真，但实际上不存在跨工作流互斥。

2. **预算检查存在并发超卖。**  
   多个 Node 可并行进入 `_guarded()`，它们读取同一个 `_budget()` 快照；成本只有执行完成后才累加到 `self.spent_usd`（`stage_runner.py:492–502`、`_execute():571–574`）。因此多个 Node 可以同时通过预算检查，合计成本超过 Task 剩余预算。

3. **after-node Hook 的结果修改不是 Hook 链式可见的。**  
   所有 after-node Hook 先以原始结果执行，所有 patch 最后统一应用（`stage_runner.py:513–520`）。因此后续 Hook 看不到前一个 Hook 的修改，尤其会影响脱敏、schema 校验、审计和 trace 的语义。

### 2.3 租户隔离风险

1. **脱敏与 trace 的执行顺序可能造成跨系统的敏感数据泄露。**  
   `pii_redaction` 只是返回 output patch；`trace_result` 会在 patch 应用前把原始结果写入 Telemetry event（`policy/hooks.py:trace_result()`、`pii_redaction()`；`stage_runner.py:513–520`）。如果 Telemetry 存储或日志权限没有严格按租户隔离，原始邮箱、手机号、身份证号可能被保留。

2. **Telemetry 的租户绑定本身有实现，但其存储隔离无法从材料确认。**  
   `_record()` 使用 `Telemetry.bind(TenantContext(...))`（`policy/__init__.py:_bound()`），Registry 请求也带租户上下文（`registry/catalog.py`、`registry/client.py`）。但 `ConsoleTelemetryProvider` 的实际输出和生产 Telemetry 后端是否按 tenant/workspace 做强隔离，材料未给出，需要查看 TelemetryProvider 实现及后端权限配置。

3. **审计 reason 不是统一哈希字段。**  
   内置 Hook 的 reason 目前主要包含 Node ID、预算数值等；但策略/扩展 Hook 的 reason 可以是任意字符串，`policy.decision` 直接记录它（`policy/__init__.py:run_hooks()`）。若扩展 Hook 将输入片段放入 reason，仍可能造成租户数据泄露，需要在 Policy Engine 层限制 reason 格式或做长度/内容约束。

### 2.4 与设计书不一致的地方

1. **Planner 不应手写 Hook，但实现继续执行 Stage 中的 `hooks`。**  
   设计书 3.2.6 说 Hook 来源是 Recipe 与租户策略，Planner 不手写；实现却在 `StageRunner.run()` 和 `_node_hooks()` 中合并并执行 `stage["hooks"]`（`stage_runner.py:run()`、`_node_hooks()`）。工作项文档的取舍明确允许这一点，但它仍与设计书原文不一致。

2. **Stage 级默认预算 Hook 未显式自动插入。**  
   计划要求 `budget_check` 可挂在 `before_stage`，且默认 Hook 按 effect 自动插入；但 `StageRunner.run()` 的 `before_hooks` 只来自策略和 Stage 声明，没有把默认 `budget_check` 加入 `before_stage`（`stage_runner.py:run()`）。Node 级预算是否插入依赖未提供的 `sp.default_node_hooks()` 实现。

3. **`checkpoint` 没有显式记录回滚模板/回滚方式。**  
   附录 D.4 要求 write_reversible 记录回滚方式；`checkpoint()` 只记录 `node_id`、`template` 和 `result_hash`（`policy/hooks.py:checkpoint()`）。除非模板本身被约定为完整回滚模板，否则当前审计记录不能证明具体回滚方式。

4. **trace 的 8 KiB 限制按字符而不是按字节计算。**  
   `trace_result()` 使用 `len(text)` 并截取字符串（`policy/hooks.py:trace_result()`），中文或其他多字节字符下，实际 UTF-8 字节数可能超过 8 KiB，与附录 D.4 的 KiB 限制不完全一致。

5. **工具调用的 effect 与幂等要求仍未完整实现。**  
   计划明确把工具 effect 目录和 ToolDriver 留给 T-10；当前 tool loop 的 tool effect 来自 `tool_effects`，且 `ToolCallInput` 路径没有像 Atomic Node 一样显式生成幂等键（`stage_runner.py:968–1042`）。因此若工具目录错误地把不可逆写标成 read，审批和 D.4 的幂等要求仍可被绕过；这是已登记的范围缺口，但对 R-01 的审批保证仍有影响。

6. **Node Executor 的审批记录只检查结构，不验证审批来源。**  
   `_check_rules()` 只检查 `gate` 非空、option 为 `approve`、gate node ID 正确（`executor/node_executor.py:405–408`），没有看到对人工审批记录、审批人身份或不可伪造凭证的校验。若 Node Executor activity 存在可被非 Stage Runner 直接调用的入口，构造一个字段匹配的 `GateRecord` 可能绕过真实人工审批；是否存在这种外部调用面需要继续查看 Worker activity 暴露和鉴权代码。

---

## 3. 按严重程度排序的问题清单

1. **可能存在 `human`/`quality` Atomic Node 绕过 `write_irreversible` 审批的路径，后果是不可逆写在未人工批准时执行，建议在 schema/Stage 校验中禁止这些组合，并在 `_dispatch()` 统一先执行不可逆 effect 审批。**
2. **`single_writer_lock` 只覆盖单个 StageRunner，跨子工作流或跨 Task 的同一共享产物仍可能并发写入，后果是数据竞争和产物损坏，建议使用按租户和共享产物标识建立的分布式锁或持久化写入协调器。**
3. **并发 Node 在成本尚未回写前都可能通过 budget_check，后果是实际成本超过 Task 预算，建议在预算检查前原子预留预算并在失败/取消时释放。**
4. **PII 脱敏在所有 after-node Hook 执行后才应用，`trace_result` 可能先记录原始敏感结果，后果是 Telemetry 中泄露未脱敏数据，建议先生成并应用修改后的结果，再执行 trace、审计和后续校验 Hook。**
5. **Policy Engine 未验证 Hook 返回结果 schema，未知 decision 可能被当作非 deny 放行，后果是非法或恶意 Hook 破坏 fail-closed 语义，建议在 `PolicyEngine.run_hooks()` 中严格校验 decision、reason 和 modification，不合法时直接 deny。**
6. **普通 Node deny 是否受 Reviewer 循环上限约束无法从材料确认，后果是策略拒绝可能持续触发重规划并耗尽 Round，建议提供并验证 `reviewer/rules.py` 的 deny 计数与终止路径。**
7. **Stage 级默认 `budget_check` 没有在 `StageRunner.run()` 中显式自动插入，后果是 Stage 的 `budget.usd` 可能未被检查，建议将默认 before_stage Hook 与 Node 默认 Hook 一样在代码中固定插入。**
8. **write_reversible 的 checkpoint 只记录模板名而非明确回滚方式，后果是发生故障时无法据审计记录可靠回滚，建议记录规范化的 rollback template 或 rollback descriptor。**
9. **trace 限制按字符而非 UTF-8 字节计算，后果是多字节文本可能超过 8 KiB 限制，建议按序列化后的 UTF-8 字节截断并保证结果可解析或明确标记截断。**
10. **Node Executor 仅验证 GateRecord 字段而未验证审批记录来源，后果是若 activity 可被直接调用，伪造结构化 gate 可能绕过人工审批，建议使用持久化审批记录、签名凭证或仅允许 Stage Runner 生成并校验 gate。**
