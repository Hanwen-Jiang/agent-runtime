# N-11～N-13、N-21 评测首轮收口（2026-11-02）

> 状态：设计收口，供首轮实现与云端冒烟使用。
>
> 这份文档把 `feedback-2026-11-01.md` 第 5 节的提纲收敛成一个可执行的首轮切片。它不改 `docs/design/agent-runtime-design.md`；正式设计书仍需由原文档维护流程更新。它也不把尚未实现的适配器描述成已完成。

## 1. 首轮目标与边界

首轮只回答三个问题：Runtime 的真实 Task Loop 能否被 Terminal-Bench 题目驱动、已有 Task 轨迹能否被 DeepEval 指标读取、Memory Service 的检索结果能否被 Ragas 计算。首轮结果用于发现接入错误、比较同一配置下的候选版本和确定下一轮样本，不作为公开排行榜成绩、生产门槛或线上隔离性能结论。

评测代码放在 `services/py-learning`，每项评测独立进程、独立输出目录、独立虚拟环境；不改 Task Loop 的工作流序列，不新增业务状态表，不把评测证据传播到计费、身份或 UI 子系统。三项评测均以明确的 `--out`、`--budget-usd` 和数据集版本运行，日志、配置摘要和结果写入输出目录，令牌只从环境变量读取。

首轮总模型预算为 **30 美元**，默认分配如下：

| 评测 | 首轮范围 | 分配 | 结果用途 |
| --- | --- | ---: | --- |
| Terminal-Bench 2.0 | 5 题 smoke；环境与判分先用 oracle 对照 | $20 | 接入与端到端解决率冒烟 |
| DeepEval | 10 个已确认的 Task 轨迹 | $5 | 轨迹指标和 judge 输入校验 |
| Ragas | 20 个脱敏检索样本；先用固定 fixture，再接真实 Search | $5 | 检索指标链路校验 |

实际调用前必须通过预算预检；未通过预检的评测不启动。分配是每项的最高计划额度，不代表供应商账单的跨进程硬上限。

## 2. 共同运行契约

### 2.1 数据与报告

每项评测接受一个带版本的输入文件，并在 `<out>/manifest.json` 写入：评测名、评测版本、数据集版本或内容摘要、Runtime revision、模型 Profile、租户、预算分配、开始与结束时间、样本数、成功/失败/跳过数和 `estimated`/`measured` 标记。原始输入、模型响应和包含用户内容的 trace 不复制到普通日志；需要保留时写入受控的 ObjectStore 引用。

每个样本至少有 `case_id`、`split`（`dev`、`holdout` 或 `sealed`）和 `source`。首轮只跑 `dev`；`holdout` 只生成汇总；`sealed` 不在开发命令中暴露。失败样本保留错误代码和阶段，不把异常栈中的令牌或用户密钥写进报告。

### 2.2 预算与模型调用

三项评测的 judge、GEval 和 Ragas LLM 调用必须经现有 Model Gateway，角色为 `judge`，并设置 `max_cost_usd_micros` 为本次剩余预算。评测 runner 串行发起 judge 调用，每次收到响应后才扣除 `usage.cost_usd_micros`；网关错误中的 `billed_cost_usd_micros` 也必须计入。SDK 自己的重试关闭，避免在 runner 外重复计费。

由于当前线上历史曾出现 Task 账单超过计划预算、且网关未能对所有未知用量提供硬保证，首轮不宣称跨进程的绝对 `$30` 上限。启动前按租户固定的模型白名单、输入上限、输出上限和网关最大尝试次数计算单次最坏成本；剩余预算不足以覆盖下一次最坏成本时停止提交。若供应商或网关不能提供 usage，也按该单次最坏成本占用预算并停止该评测。首轮还必须配置供应商侧额度或人工账单告警。

**Failure**：重试、未知 usage 或 Task 侧漏计导致计划预算被突破。

**Evidence**：此前线上 SWE-bench 冒烟已出现计划总额 $10、网关计费 $12.16 的情况；现有 `CallContext.max_cost_usd_micros` 已存在，但评测端仍需按响应和错误详情累计。

**Minimum fix**：评测端做单项预检、串行调用、按成功 usage 和 billed cost 记账；网关未能证明单次上限时依赖供应商侧额度并停止该项。把网关全局预算账本留到单独的网关工作项。

**Benefit**：降低首轮失控花费，并让每份报告能解释实际成本。

**Performance cost**：judge 调用串行，吞吐下降；预检增加一次本地计算，不增加远程读。

**Implementation cost**：每个 runner 统一使用一个 cost collector 和一个预算 preflight；不新建跨系统账本。

**Operational cost**：每项评测要单独配置模型白名单、单价和供应商告警。

**New failure surface**：价格配置过期或网关返回未知 usage 会提前停止并留下 incomplete 报告。

**Release slice**：N-11～N-13 首轮只接受“可解释地停止”或“完整跑完”，不把不完整结果计入分数。

**Decision**：首轮采用；跨进程硬预算账本 DEFER。

### 2.3 并发与续跑

N-21 提供 `python -m agentruntime_learning.benchmarks suite run --plan <plan.json> --out <dir>`。计划只允许 `terminalbench`、`deepeval`、`ragas`（SWE-bench 可选），要求名字唯一、每项额度为正、份额之和不超过总额、`max_parallel >= 1`。每项子进程只拿到自己的 `token_env` 和输出目录；令牌不写入文件。

一个子进程失败不停止其他项；`suite.json` 记录每项状态、退出码、耗时、日志路径和实际成本，最终有失败或 incomplete 时套件退出码为 1。复跑同一个目录时跳过已完成项，重跑失败或 incomplete 项；计划名单变化拒绝续跑。`--dry-run` 只打印命令。

首轮在三项适配器分别通过离线验收后再开 2～3 路并发。并发不是预算控制；每项仍按 2.2 的预检和停止规则执行。

## 3. N-11 Terminal-Bench 2.0

### 3.1 运行方式

固定 Harbor 0.24.0 与 Terminal-Bench 2.0 数据集版本。适配器实现 Harbor `BaseAgent`：题目容器由 Harbor 创建，Runtime 通过独立的评测 Task API/Temporal namespace 驱动同一个 `BaseAgent` 会话；评测专用 ToolDriver 把 `shell_run` 映射到该题目环境的 `BaseEnvironment.exec`，`context_read` 继续读取 Task workspace。题目执行结束后由 Harbor 在**同一个题目环境**中执行官方 verifier，再由适配器把 reward、verifier 输出摘要、Round 数和 Model Gateway 成本写入记录。

评测专用 Worker、Task API 地址、Temporal namespace、数据库目录和 Harbor 运行目录全部与生产隔离。实现优先复用现有 Planner、Stage Runner、Reviewer 和 tool loop；工具路由必须通过显式的评测 provider 注册或构造参数完成，不修改生产全局缓存，不 monkey-patch 私有对象。

这条路径不新增公共 `RunCheck` proto，也不把 Terminal-Bench 题目镜像灌进现有 K8s 沙箱。现有 K8s sandbox 的只读根目录、固定工作目录和可能的 universal fallback 无法保证题目所需的系统状态；Harbor 自己拥有题目环境的创建、执行、判分和销毁，能保持官方 verifier 的同环境语义。

### 3.2 首轮数据与输出

首轮选择 5 个可构建、可联网依赖已在环境登记中满足的题目，固定 seed 和题目清单；先用 Harbor oracle 在同一清单上跑一次，确认 verifier 输出与适配器的 resolved/unresolved 映射，再跑 Runtime agent。任何环境构建失败、verifier 未执行或 agent 与 verifier 不在同一容器的样本都记为 `infra_failed`，不能计入解决率。

每题记录 `task_id`、`task_name`、数据集版本、环境镜像 digest、Runtime revision、agent status、verifier status、reward、rounds、model_cost_usd、infra_error_code 和日志引用。报告同时给 `resolved / judged`、成本和 Round 分布；5 题结果只标为 smoke，不与公开榜单直接比较。

### 3.3 Terminal-Bench 设计决策

**Failure**：现有生产沙箱无法保持 Terminal-Bench 题目的原始系统状态和判分环境。

**Evidence**：题目 verifier 必须在 agent 完成后的同一环境执行；当前沙箱实现固定工作目录并使用只读根文件系统，且 D-17 环境不保证题目镜像不会 fallback。

**Minimum fix**：在 py-learning 增加 Harbor 0.24.0 评测桥和隔离的 Runtime Worker/Task API 运行配置。

**Benefit**：得到可复现的同环境执行与判分结果。

**Performance cost**：每题启动/销毁一个 Harbor 容器；5 题 smoke 的冷启动时间较长。

**Implementation cost**：新增一个评测进程边界、ToolDriver 注入、记录和离线验收；不改 proto 和生产 Worker。

**Operational cost**：cloudnium_cloud 需要 Docker、Harbor 依赖、题目镜像缓存、网络授权和独立 Temporal namespace。

**New failure surface**：Harbor、题目镜像或 Runtime Task API 任一侧故障会产生 `infra_failed`，不能混入 agent 分数。

**Release slice**：先 oracle 1 题，再 oracle+Runtime 5 题；未完成这两步不扩到 20 题。

**Decision**：采用隔离 Harbor 桥；公共 `RunCheck` DEFER。

## 4. N-12 DeepEval

### 4.1 输入规范

DeepEval 不提供本项目数据集；适配器从现有 `evalsets/task-loop` 和已保存的 Task trace 生成 `DeepEvalCase`。首轮每个 case 至少包含：

```json
{
  "case_id": "ci_fix_failing_test",
  "split": "dev",
  "input": "用户消息",
  "actual_output": "最终交付文字",
  "expected_tools": [{"name": "repo_read"}],
  "tools_called": [{"name": "repo_read", "arguments": {}, "result_status": "ok"}],
  "task_completed": true,
  "reference_answer": "人工确认的验收要点",
  "trace_ref": "obj://..."
}
```

缺少人工确认的 `expected_tools`、`reference_answer` 或完成标签时，相关指标为 `unscored`，不填 0。只从 trace 中读工具名、参数摘要、阶段状态、Verdict、Round 数和已有成本；不把完整 prompt、密钥或跨租户内容送给 judge。

### 4.2 指标与 judge

固定独立虚拟环境的 DeepEval 4.3.1。首轮只启用 `TaskCompletion`（有人工完成标签时）、`ToolCorrectness`（要求 `tools_called` 与 `expected_tools`）、`StepEfficiency`（用 Round/工具步数的规则输入）和一个受控 GEval rubric（最终交付是否满足 `reference_answer`）。`PlanAdherence` 只有 N-04 计划字段稳定且 case 带人工计划标注后才启用。

DeepEval 的 LLM-as-a-judge 通过一个薄适配器调用现有 Model Gateway 的 `Generate`，请求带结构化 schema、`role=judge`、不同于被测生成者的供应商约束和 `max_cost_usd_micros`；不使用仓库里只为离线对照的直连 `provider_gateway`，因为它不实现 Embed 且绕过线上 Gateway 计量。每项指标写 `score`、`threshold`、`reason`、judge model、usage、cost 和 `unscored_reason`。

### 4.3 DeepEval 设计决策

**Failure**：没有统一 trace→LLMTestCase 的转换会让指标看似有分数但无法复核；缺标注的 case 被误判为失败会污染准入结论。

**Evidence**：ToolCorrectness 要求 `input`、`actual_output`、`tools_called`、`expected_tools`；现有 Task trace 的完整 prompt 不应复制到评测输出。

**Minimum fix**：实现只读 trace normalizer、缺标注为 `unscored`、DeepEval 4.3.1 独立环境和 Gateway judge adapter。

**Benefit**：指标输入可复核、成本可归因，能与人工结论对照。

**Performance cost**：每个启用的 LLM 指标增加一次或多次 judge 调用；首轮串行。

**Implementation cost**：normalizer、薄 adapter、JSON 报告和 10 个 case 的离线 fixture；不改 Worker 工作流。

**Operational cost**：需维护 DeepEval 独立环境和 judge 模型白名单。

**New failure surface**：trace schema 演进或 judge JSON 解析失败会产生 `unscored`/incomplete。

**Release slice**：先 10 个 dev case 通过 fixture 测试，再对 10 个已确认真实 trace 做一次云端 judge。

**Decision**：采用；PlanAdherence、自动生成数据集和准入门槛 DEFER。

## 5. N-13 Ragas

### 5.1 输入规范与两阶段顺序

固定独立虚拟环境的 Ragas 0.4.3。输入为 `RagasCase`：

```json
{
  "case_id": "memory_ci_fix_recipe",
  "split": "dev",
  "user_input": "查询",
  "reference": "人工确认的答案或关键事实",
  "retrieved_contexts": ["检索片段 1", "检索片段 2"],
  "response": "基于检索结果的回答",
  "retrieval_backend": "fixture|memory-service",
  "embedding_version": "a6api/text-embedding-3-large@2026-10",
  "trace_ref": "obj://..."
}
```

首轮先运行 20 个脱敏 fixture，确认样本 schema、指标 API、成本记录和报告格式；再把同一批样本的 `retrieved_contexts` 替换成 Memory Service Search 结果。没有 `response` 的样本不运行 Faithfulness；没有人工 `reference` 的样本不运行 Context Recall。不得将缺字段样本填 0。

首轮不阻塞在 Milvus 切换：fixture 和已部署的开发 Memory Service 可以验证 Ragas 适配器；生产 Ragas 结论必须等 M-03 接通真实 Search 后重新跑。比较 embedding 或 Jev 时，每个实验固定数据集、top-k、chunking、tenant 和模型版本，分别输出报告，不能把不同配置混在一个均值里。

### 5.2 指标与 judge

首轮启用 `ContextPrecision`、`ContextRecall`、`Faithfulness`；如果 Ragas 版本还要求响应相关性所需字段，明确记录为未启用，不静默换指标。LLM 与 embedding 均走现有 Model Gateway 的相应能力和租户上下文，计量写入同一结果文件；Ragas 内部并发关闭，由 runner 控制并发和预算。

### 5.3 Ragas 设计决策

**Failure**：检索样本缺少 reference/response，或 embedding 与检索数据版本混用，会生成无法解释的分数。

**Evidence**：Faithfulness 需要 response；Context Precision/Recall 需要 reference 与 retrieved contexts；Memory Service 当前仍有未部署/未接 Worker 的状态。

**Minimum fix**：20 个脱敏、人工确认的 fixture；明确字段缺失为 `unscored`；先 fixture 后真实 Search；固定 embedding、top-k、tenant 和数据版本。

**Benefit**：先验证适配器，再验证真实记忆检索，不把部署切换与评测接入绑在一起。

**Performance cost**：每个样本至少一次 embedding 或 judge 调用；首轮串行，耗时较长。

**Implementation cost**：Ragas 0.4.3 adapter、Gateway LLM/embedding wrapper、样本 loader 和报告；不改 Memory Service proto。

**Operational cost**：维护脱敏样本、embedding Profile 和 Memory Service 的开发/生产数据版本。

**New failure surface**：embedding Profile 不可用、Search 返回空或 Ragas API 变更会导致 `unscored`/incomplete。

**Release slice**：fixture 20 条全量 schema/指标验收；真实 Search 只在 M-03 环境就绪后运行。

**Decision**：采用；自动生成 Ragas 数据集、跨 embedding 的线上切换和准入门槛 DEFER。

## 6. 验收与上线顺序

1. 离线：三个独立环境可安装固定版本；fixture loader、字段校验、缺标注 `unscored`、成本累计、令牌脱敏和报告 schema 通过测试。
2. Terminal-Bench：Harbor oracle 1 题通过；同题 Runtime agent 记录到同一环境 verifier；再跑 5 题 smoke。
3. DeepEval：10 个 fixture 通过；10 个真实 dev trace 的 ToolCorrectness 与人工工具标注一致性可复核。
4. Ragas：20 个 fixture 的三项指标输入齐全；缺 response/reference 的样本被跳过并说明原因；开发 Memory Service Search 的第二阶段报告能回溯 embedding 与 top-k。
5. Suite：各项单独通过后才启用 2～3 路并发，验证 `suite.json`、失败隔离、续跑和 `--dry-run`。
6. 云端：在 `cloudnium_cloud` 使用独立输出根目录和评测租户；先 dry-run 和 oracle，再按 `$20/$5/$5` 分配运行。任何 preflight、环境构建或计量前置检查失败都停止，不自动扩大样本或预算。

## 7. 明确不在本轮

- 不修改 `docs/design/agent-runtime-design.md`、proto 或 schema；正式设计书更新走原文档导出和 S-01 流程。
- 不新增跨进程预算账本、全量远程扫描、后台对账 worker 或跨子系统 evidence state。
- 不把 Harbor 题目环境接入生产 SandboxBroker，不宣称 Terminal-Bench 结果代表生产 K8s 隔离性能。
- 不在 M-03 未就绪时声称 Ragas 已测到生产 Memory Service；不把未标注 DeepEval/Ragas 样本计为 0 分。
- 不把 5 题 Terminal-Bench smoke、10 条 DeepEval dev、20 条 Ragas fixture 当作公开榜单或发布门槛。

## 8. 依据与版本锁定

- Terminal-Bench 运行约定：[Run Terminal-Bench 2.0](https://www.tbench.ai/docs/run-terminal-bench-2-0)。
- Harbor agent 与环境能力：[Harbor agents 文档](https://docs.harborframework.com/agents/pre-integrated-agents)。
- DeepEval 工具调用指标所需字段：[Tool Correctness](https://deepeval.com/docs/metrics-tool-correctness)。
- Ragas 指标字段与计算：[Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)、[Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)、[Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)。
- 依赖版本锁定为 Harbor 0.24.0、DeepEval 4.3.1、Ragas 0.4.3；这些版本在 2026-10-19 依赖冻结日之前已发布，满足仓库 `exclude-newer` 约束。实现时仍需在独立虚拟环境中生成锁文件并记录解析结果。
