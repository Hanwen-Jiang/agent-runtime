# R-06 复审（另一家的强模型：OpenAI gpt-6-sol，经 a6api）

> 2026-10-24。按实施计划 6.2 的模板，只给了工作项全文、设计书相关章节、非测试源码的 diff 与测试结果（材料包的组成见 [README.md](README.md)），
> 没有给之前同一家模型写的评审。下面是模型输出的原文，没有改动；采纳与处理见对应评审文档的“另一家复审”一节。

## 1. 逐条检查“评审重点”与“难点与防错”

### R-06 评审重点

| 检查项 | 结论 | 依据与代码位置 |
|---|---|---|
| 未过评测不能发布 | **已处理** | `services/py-learning/src/agentruntime_learning/pipeline/run.py` 的 `run_pipeline` 先执行 `non_inferiority`，不通过时返回 `rejected`；只有通过后才调用 `registry.publish`、`registry.promote`。Go 端 `services/go-task-service/internal/registry/service.go:125–157` 还会校验证据并拒绝 `passed != true`。测试结果报告了真实 Registry 的拒绝演示。 |
| 后台任务单独记预算 | **已处理** | `pipeline/budget.py` 的 `LearningBudget` 管理单次运行预算；`pipeline/attribution.py` 的 `_request` 将剩余预算传给 `max_cost_usd_micros`，`CallContext` 不带 `task_id`；`pipeline/run.py` 在平台作用域记录 `learning.cost_usd`。但 Gateway 是否总按上限计费，材料不足以确认；还需看 Gateway 对 `max_cost_usd_micros` 的执行保证。 |
| 运行中的 Task 不受影响 | **不确定** | 发布通过 Registry 的 `Publish`/`Promote` 完成，代码本身没有修改运行中配置；但运行中 Task 固定版本的保证属于 Registry/Worker 行为。工作项称 T-08 已测，当前材料只有 Go `Promote` 代码及一条端到端结果摘要；还需看 T-08 的固定版本实现与测试。 |
| 失败归因提示 | **已处理** | `pipeline/attribution.py` 的 `attribute_failures` 对失败 Task 调用 `small@low`，输入由 `_prompt` 构造，输出解析后写入报告；无 Gateway、预算用尽或调用/解析失败时记为 `unattributed`。但输出校验并未完整实施，见下文。 |

### “难点与防错”逐条检查

| 检查项 | 结论 | 依据与代码位置 |
|---|---|---|
| 门槛判定与发布顺序；未通过时 Registry 写方法零调用 | **已处理** | `pipeline/run.py` 的 `run_pipeline` 在门槛失败分支直接返回，写方法只出现在通过分支。Go `service.go:151–157` 是第二道 `passed` 校验。测试文件 `test_pipeline_run.py` 存在且 pipeline 测试通过；具体调用断言未提供，不能独立核验断言细节。 |
| 候选组纳入失败 Task，避免选择偏差 | **已处理** | `pipeline/evaluation.py` 的 `candidate_cohort` 纳入签名相同的 Task，以及签名为候选严格前缀且失败的 Task。 |
| 样本不足不能通过 | **已处理** | `pipeline/evaluation.py` 的 `non_inferiority` 对候选组、现行组分别检查 `min_samples`，不足时加入失败原因；默认值为 100。 |
| 租户隔离：异租户 trace 不影响候选、评测、归因；发布按指定租户；每次运行审计 | **不确定** | `pipeline/traces.py` 的 `task_traces` 按 span `tenant_id` 过滤，`pipeline/run.py` 用指定租户构造 Registry tenant，并在平台作用域记审计。不过 Worker 的 `trace_task`（`services/py-worker/src/agentruntime_worker/reviewer/wrap_up.py`）没有显式写入 `tenant_id`，依赖 `telemetry.bind(tenant)` 自动注入；需检查 Telemetry 实现及真实 OTLP 导出，确认该属性确实在每个 Task/Round span 上。另，审计日志没有记录输入文件名，未满足计划中“租户、文件、读到的 span 数、跳过的其他租户 span”的完整字段要求。 |
| trace 不带用户数据；归因输入仅来自结局、verdict 与 Stage 形状 | **已处理** | `attribution.py` 的 `_prompt` 只拼结局、Round verdict 和 `shape`；`verdicts.py` 的 `stage_shape` 只保留指定结构字段，不含 inputs、rationale。工作项提到的消息断言未给出测试内容，无法核对测试具体覆盖。 |
| 预算用完停止调用；请求上限不超过剩余预算；花费不进 Task 指标 | **已处理** | `attribution.py` 在预算耗尽时不发请求，且请求上限取 `budget.remaining_micros`；调用不带 `task_id`。运行预算写入平台指标，不写入 Task 成本。若单次实际费用超过 Gateway 接收的上限，`LearningBudget.charge` 仍可能记录超支；是否会发生取决于 Gateway 的限额保证。 |
| 确定性：同批 trace 顺序不影响候选、证据 | **已处理** | `traces.py` 对 Task、Round 排序并去重；`candidates.py` 对支持 Task 排序，并以确定规则选签名和正文 Task；`evaluation.py` 排序 cohort、对 task_id 集合计算哈希。计划允许 `evaluated_at` 与 report 不同。 |
| 至少一次投递：同一 task_id 只算一次 | **已处理** | `traces.py` 的 `task_traces` 按 task_id 归并重复根 span，并按 span_id 取一个；Round 按 `round_seq` 去重。需注意重复根中取哪个 span 由 span_id 决定，不保证是最新或内容最完整的那个。 |

## 2. 其他风险与设计一致性

### 确定性风险（Temporal 工作流重放）

- **没有发现 Learning Pipeline 被实现成 Temporal Workflow**：一期明确采用一次性命令，`pipeline/__main__.py` 用 `asyncio.run`，与计划“不做 offline 队列上的 Temporal 工作流”一致。
- **Worker 侧新增的 Telemetry 在 Activity 上下文中执行**：`wrap_up.py` 的 `trace_task` 使用 `activity.in_activity()` / `activity.info()`；可见代码未显示在 Temporal Workflow 代码中进行非确定性操作。因此，基于当前材料，**没有确认的 Workflow 重放违规**。
- 仍需核对 `WrapUp` 的调用位置以及 Telemetry 实现，确认 `trace_task` 确实只从 Activity 调用；此外，Activity 重试可能产生不同 span_id 的重复根 span，当前去重会选 span_id 最小者，可能取到不完整记录。

### 并发风险

- **同租户、同任务族的并发运行可能互相覆盖晋升结果**：`run_pipeline` 没有显示针对任务族的锁、版本比较或 CAS；候选名固定为 `learned_<任务族>`，通过后分别 `Publish` 再 `Promote`。并发运行可能按不同 trace 得到不同候选，最终默认版本取决于 Promote 顺序。Registry 是否有更强并发保护，材料未提供；需检查 Registry 的并发版本语义及相关测试。
- **同秒重跑可能写同一个报告文件**：`pipeline/run.py` 的文件名精度到秒，`{task_family}-{now:%Y%m%dT%H%M%SZ}.json`；并发或短时间重跑会写同一路径。
- 至少一次投递的 span 去重是单次读取内的归并；跨多次运行的发布幂等性没有在 Learning 代码中体现。

### 租户隔离风险

- Trace 过滤、Registry 调用的 tenant 参数及 `learning.read_traces` 的平台作用域均有实现；跨租户 span 不进入 Task 集合的逻辑在 `traces.py` 的 `task_traces`。
- 但 span 是否确实带有可信的 `tenant_id` 依赖 `TelemetryProvider.bind(tenant)` 的隐式行为；diff 未提供该实现。若绑定没有给 Task 和子 span 注入租户属性，现有过滤会把这些 span 当成其他租户排除，导致数据缺失；若属性来源不可信，则隔离判断也不可靠。
- 平台审计缺少文件名，不能完整实现计划要求的读取审计字段。

### 与设计书/工作项不一致的地方

1. **现行 Recipe 选择不等于默认 Recipe**：`pipeline/run.py` 的 `_baseline_recipe` 将 `list_recipes` 返回的 Recipe 引用排序后取第一个，没有可见逻辑识别 Registry 默认版本；可能用错现行组，进而影响门槛结论。
2. **审计缺少文件字段**：计划要求记录文件名，`run_pipeline` 的 `learning.read_traces` 属性没有文件名；`__main__.py` 读取文件后只把行合并传入，文件来源已丢失。
3. **归因输出没有按完整 schema 校验**：`attribution.py` 的 `_parse` 只检查 category 属于枚举且 reason 是字符串；没有校验 reason 非空、长度上限及 `additionalProperties: false`。这与“输出按 schema 校验”不符。
4. **重跑发布幂等性未见实现**：计划称发布按门槛结果幂等；代码每次通过门槛都执行 `Publish`、再 `Promote`，未见幂等键或既有版本检查。Registry 的 `Publish` 是否内部实现幂等，材料不足；需查其实现。
5. **部署配置仍配置 KEDA 消费触发器，但 Job 读固定文件**：`deploy/helm/agent-runtime/templates/learning.yaml` 一边声明 Kafka lag trigger，一边传入固定 `tracesFile`；与“一期只能读文件、Redpanda 消费后续再做”有潜在运行语义冲突。需看 Helm 默认值和部署启用方式，确认是否会把 Kafka lag 误当作文件任务触发依据。
6. **设计书 3.5 / J.3 中影子评测、灰度放量及自动回滚未实现**：这是计划明确登记为一期不做的范围，属于已知差异，并非本次意外遗漏；观测式评测的边界已在工作项中说明。
7. **H.2 的成功交付期望成本目标未直接用于门槛**：当前按 J.3 的“每次成功成本”均值及上界比较；工作项明确决定第一期只用 J.3 两条门槛，因此不按 H.2 另加门槛判定。

## 3. 按严重程度排序的问题清单

1. **现行组可能选错非默认 Recipe，导致门槛对比失真并错误发布或拒绝**；建议从 Registry 明确读取默认 Recipe 版本，而不是对全部 Recipe 排序取第一个（`pipeline/run.py` 的 `_baseline_recipe`）。
2. **并发 Learning 运行可能以不同候选交错 Publish/Promote，最终默认版本由时序决定**；建议在 Registry 侧提供带基线版本的 CAS/幂等 Promote，或对租户与任务族串行化并发运行（`pipeline/run.py` 的 `run_pipeline`）。
3. **租户隔离依赖 Telemetry 隐式注入 `tenant_id`，当前 diff 无法证明 Task/Round span 都带可信租户标识**；建议显式验证并补充真实导出端到端断言，检查 `telemetry.bind(tenant)` 的实现。
4. **归因结果未完整按 schema 校验，可能把空 reason、超长 reason 或额外字段当成有效归因写入报告**；建议用项目 schema validator 校验完整输出，而不是只做字段类型检查（`pipeline/attribution.py` 的 `_parse`）。
5. **通过门槛后的重跑未见发布幂等保护，可能重复创建版本并反复 Promote**；建议以评测证据版本/候选内容哈希作为幂等键，并确认 Registry `Publish` 的重复请求语义（`pipeline/run.py` 的发布分支）。
6. **审计日志缺少计划要求的 trace 文件名，无法按文件追溯读取来源**；建议在读文件时保留路径并写入 `learning.read_traces` 审计属性（`pipeline/__main__.py` 与 `pipeline/run.py`）。
7. **短时间或并发运行可能覆盖同秒生成的报告文件**；建议文件名增加唯一 run_id 或更高精度时间戳（`pipeline/run.py` 的 `filename` 生成逻辑）。
