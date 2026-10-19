# Agent Runtime（暂名）

这个文件夹是 Agent Runtime 的代码仓库。[强档实施计划](docs/plan/strong-tier-plan.md)的 S-01 已建立契约骨架：`/proto`（含生成的 Go 与 Python 代码）、`/schemas`、各服务的空骨架、`/deploy` 与契约验收测试 `/tests/contract`。目录说明与常用命令见 [CLAUDE.md](CLAUDE.md)。

## 文档

| 文件 | 是什么 | 什么时候看 |
| --- | --- | --- |
| [docs/design/agent-runtime-design.md](docs/design/agent-runtime-design.md) | 项目设计书，含 34 张结构图 | 一切以它为准；实现任何模块前先读对应章节 |
| [docs/plan/strong-tier-plan.md](docs/plan/strong-tier-plan.md) | 强档实施计划：用 AI 分模块实现时，必须由强模型完成或评审的工作项、顺序与验收 | 安排开发顺序、给 AI 下任务、做评审时 |
| [docs/reference/patterns.md](docs/reference/patterns.md) | Agent 模式的叫法、出处、流程与在本设计中的落点 | 实现 Stage 形状、mode、quality、reasoning 时 |
| [docs/reference/task-loop-evidence.md](docs/reference/task-loop-evidence.md) | Task Loop、Reviewer 纠偏与 Triage 的设计依据 | 实现 Planner、Reviewer，遇到边界情况时 |
| [docs/reference/model-evaluation.md](docs/reference/model-evaluation.md) | 模型评测平台、价格与路由的调研 | 实现 Model Profile 与 Model Gateway 选档时 |
| [docs/reference/memory-and-retrieval.md](docs/reference/memory-and-retrieval.md) | 记忆框架、经验学习、投毒案例与 RAG 依据 | 实现 Memory Service 时 |
| [docs/reference/context-engineering.md](docs/reference/context-engineering.md) | 上下文组织与压缩的调研与实证对比 | 实现 Context Builder 时 |
| [docs/reference/runtime-quickref.md](docs/reference/runtime-quickref.md) | 运行核心速查：对象存放、模块依赖、自研边界、接口与开发实现、旧叫法对照 | 随手查 |
| [docs/reference/risks-and-open-questions.md](docs/reference/risks-and-open-questions.md) | 风险、待团队决定的问题、待定指标 | 开工前与每个阶段结束时 |
| [docs/reference/sources.md](docs/reference/sources.md) | 全部参考来源，按主题去重 | 需要回到原始出处时 |

## 来源

| 本文件夹的文档 | 来自 |
| --- | --- |
| 设计书 | Claude Docs 文档《Agent Runtime 项目设计书（暂名）》（https://claude.ai/code/artifact/9208d7e8-6b94-4792-9382-a0d0f36921b5），即 `Z:\agentruntime\项目计划书` 的内容补全第 4 章接口图之后的版本 |
| 强档实施计划 | 新编写，依据设计书 |
| `docs/reference/` | 由 `Z:\agentruntime` 下的 `主文档`、`模式图谱`、`精简版` 三份调研文档重新编排而成，每个文件开头注明了具体出处 |

调研文档写于命名统一之前。`docs/reference/` 中已全部改用设计书附录 A 的术语；读原文时，用 [runtime-quickref.md](docs/reference/runtime-quickref.md) 第 7 节换算。调研文档里的示意图没有随 md 导出，需要时看原 `.docx` 或 `.pdf`。

## 给 AI 编码助手

在本仓库工作的 AI 先读 [CLAUDE.md](CLAUDE.md)：先读什么、怎么接任务、术语，以及不能违反的硬性规则（契约、工作流确定性、副作用、租户隔离）。
