# 评测适配器实现状态（2026-11-02）

本状态记录对应分支 `codex/evals-design-closeout`。三个首轮适配器已提交，当前 release slice 是离线 fixture/oracle 契约、字段校验、报告和 CLI；输出统一标记 `estimated`/`measured`，不会把 fixture 分数当成云端真实成绩。

| 适配器 | 当前可用路径 | 当前明确边界 |
| --- | --- | --- |
| Terminal-Bench 2.0 | 5 题内置 smoke 的 `fixture`/`oracle`；缺 verifier 记 `infra_failed` | `harbor` 只做依赖预检，Runtime `BaseAgent` 桥未接通时直接失败，不伪造完成结果 |
| DeepEval | JSON case loader；ToolCorrectness、TaskCompletion、StepEfficiency fixture 规则；缺人工标注为 `unscored` | 尚未把 GEval judge 接到 Model Gateway；当前不产生真实 judge 成本 |
| Ragas 0.4.3 | `SingleTurnSample` 兼容适配；ContextPrecision、ContextRecall、Faithfulness；缺 reference/response 或未安装依赖为 `unscored` | 尚未接 Model Gateway 的 LLM/embedding wrapper；当前不产生真实 Ragas 成本 |

## 变更决策记录

**Failure**：Harbor 包已安装但 Runtime 题目环境、BaseAgent 会话和官方 verifier 尚未接通时，若继续沿用 fixture 循环，会生成看似完成的 Terminal-Bench 结果。

**Evidence**：当前模块只有 Harbor import 预检，没有题目容器、Task API/Temporal namespace、ToolDriver 或同环境 verifier 调用。

**Minimum fix**：`mode=harbor` 在预检通过后仍显式返回 `harbor_bridge_not_implemented`；只有 `fixture`/`oracle` 写入结果。

**Benefit**：避免把未执行的任务计入解决率，且保留可复核的错误阶段。

**Performance cost**：真实 Harbor 冒烟在桥接完成前不可运行；当前只增加一次本地预检。

**Implementation cost**：不新增运行时状态或远程扫描；后续只需补隔离的 BaseAgent/ToolDriver bridge 和同环境 verifier 调用。

**Operational cost**：真实切片仍需要 Docker、Harbor 0.24.0、题目镜像和独立评测运行配置。

**New failure surface**：安装 Harbor 后，命令会明确因 bridge 缺失退出，而不会返回空报告。

**Release slice**：本次接受 fixture/oracle 离线验收；Harbor oracle 1 题和 Runtime agent 同环境验收属于下一切片。

**Decision**：保留显式失败；不在当前切片添加未经验证的 Harbor CLI 猜测调用。

