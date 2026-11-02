# 评测适配器实现状态（2026-11-02）

本状态记录对应分支 `codex/evals-design-closeout`。三个首轮适配器已提交，当前 release slice 同时包含离线 fixture/oracle 和已配置的真实执行入口；输出统一标记 `estimated`/`measured`，不会把 fixture 分数当成云端真实成绩。

| 适配器 | 当前可用路径 | 当前明确边界 |
| --- | --- | --- |
| Terminal-Bench 2.0 | 5 题内置 smoke 的 `fixture`/`oracle`；缺 verifier 记 `infra_failed`；`harbor` 通过真实 `BaseAgent` bridge 调 Runtime，同容器由 Harbor 执行 verifier 并解析 `result.json` | 真实模式需要 Python 3.13+ 的 py-learning eval venv、Harbor 0.24.0、Docker/题目镜像，以及部署方提供 `AR_TERMINALBENCH_RUNTIME_COMMAND`；缺任一项会显式失败 |
| DeepEval | JSON case loader；fixture 规则；`gateway` 模式提供结构化 judge 和可注入 `GatewayDeepEvalLLM`，逐 case 记录 usage/cost | 真实 judge 仍受 Gateway factory、tenant 和模型 tier 配置约束；fixture 不产生真实 judge 成本 |
| Ragas 0.4.3 | `SingleTurnSample` 兼容适配；ContextPrecision、ContextRecall、Faithfulness；`gateway` 模式提供结构化 LLM 和 embedding wrapper，共享单次预算 ledger | 真实 Ragas 需要 Ragas 0.4.3、Gateway factory；未安装依赖或缺标注会按字段级 `unscored` |

## 变更决策记录

**Failure**：Harbor 运行如果没有把 Runtime 调用放进题目环境，或没有读取 Harbor 自己的 verifier 结果，会生成看似完成的 Terminal-Bench 结果。

**Evidence**：Harbor 0.24.0 的 `BaseAgent.run(instruction, environment, context)` 和 `BaseEnvironment.exec(...)` 已提供同容器执行入口；作业结果由 job-level `result.json` 的 `trial_results` 和 per-trial `result.json` 组成。

**Minimum fix**：加入 `RuntimeBridgeAgent`，使用 `AR_TERMINALBENCH_RUNTIME_COMMAND` 调用 Runtime，保留退出码/日志，解析 Harbor 的 `verifier_result.rewards`；DeepEval/Ragas 通过 Gateway 的 `role=judge` 请求并在单次运行内共享预算。

**Benefit**：真实分数来自 Harbor 官方 verifier，judge/embedding 费用来自 Gateway usage；缺配置时仍保留可复核的明确失败阶段。

**Performance cost**：Harbor 每题增加一次 Runtime 命令；DeepEval 每 case 一次 judge 调用，Ragas 按 metric 发起 Gateway 调用。运行串行化，避免首轮预算并发超支。

**Implementation cost**：新增一个 BaseAgent bridge、两个 Gateway wrapper 和结果解析；不新增持久化状态机或远程扫描。

**Operational cost**：真实切片需要 Python 3.13+ eval venv、Docker、Harbor 0.24.0、题目镜像、Gateway factory/tenant，以及 Runtime command 环境变量。

**New failure surface**：Harbor CLI、Runtime command、Gateway RPC 或 schema 返回不匹配时会显式退出/按 case 标记 `unscored`，不会转成通过。

**Release slice**：本次接受 fixture/oracle 离线验收，并交付已配置的 Harbor/Gateway 真实入口；云端真实跑数仍以部署参数和依赖安装作为前置条件。

**Decision**：保留最小真实 bridge；缺 Harbor、Runtime command 或 Gateway 配置时显式失败，避免伪造分数。

## 真实运行入口

Harbor：在 Python 3.13+、安装了 `harbor==0.24.0` 和本项目包的 eval venv 中设置 `AR_TERMINALBENCH_RUNTIME_COMMAND`，命令中使用 `{instruction}`、可选 `{workspace}` 和 `{budget_usd}` 占位符，再运行 `terminalbench run --mode harbor`。Harbor 负责题目容器和官方 verifier。当前 `cloudnium_cloud` 主机 Python 是 3.12.3，不能直接满足 py-learning 的 `requires-python >=3.13`，需要使用 3.13 eval 镜像或先补齐解释器。

DeepEval/Ragas：设置 `AR_EVAL_GATEWAY_FACTORY=module.path:factory`（factory 返回现有 Model Gateway client），并传入 `--tenant-id`、`--budget-usd`；Ragas 需要 embedding 指标时另外传 `--embedding-version`。三项合计预算可按计划拆分，建议首轮使用总上限 `$30`。

