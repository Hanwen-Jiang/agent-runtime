# 对照 Aster Runtime：本项目还差什么

> 2026-10-24，main（e8eb29c 之后）。对照的是 [Aster 项目](aster-project.md) 第 2 节的七个模块与架构决定；本项目一侧以代码与测试为准。
> “完成度”是按 Aster 设计里该模块的能力清单粗估的，只用来排优先级，不是度量。

## 1. 一览

| Aster 模块 | 我们的对应 | 完成度 | 已有 | 主要缺口 |
| --- | --- | --- | --- | --- |
| M1 In/Out | Task API（HTTP POST + SSE）、Ingress | 约 60% | 提交、取消、人工回复、状态查询；SSE 续传（Last-Event-ID）；租户由令牌决定；按租户限流；提交去重 | 只有开发令牌，没有正式登录（OAuth、SSO）；事件正文只在 Cache；没有 token 与工具状态的增量；同一幂等键配不同正文不报冲突；没有 IDE、CLI 适配器 |
| M2 Kernel：运行引擎与恢复 | Temporal（Session、Task 工作流） | 约 90% | 重放、版本分支、golden 历史；故障注入套件；Claim-Check | 夜间端到端的偶发失败（回放、重复事件、Postgres 下的版本固定）已查明并修复（2026-10-25；Postgres 下是各套进程共用一个业务库） |
| M2 Kernel：租户与授权 | 租户策略、effect 上限、Policy Engine、gate | 约 55% | effect 上限、Hook、不可逆写前的审批、幂等键；RLS；越权测试 | 没有“逐步收窄到冻结计划”的执行规格；没有委托主体（草稿 C5）；审批不绑输入摘要、没有多人与策略审批（C3、C4） |
| M2 Kernel：资源 | 预算检查、Model Gateway 计量 | 约 40% | Task 预算、按租户计量、限流 | 没有预留与结算；没有配额与公平队列；子任务没有预算份额 |
| M2·1 Workspace Manager | 无（ToolDriver 只认工作区目录） | 约 10% | `AR_TOOL_WORKSPACE_ROOT/<租户>/<workspace>` 下的读写工具 | 没有 revision、快照、分支、提交与 Apply；没有 Task 级的版本固定（C2） |
| M2 Registry | Registry（T-08） | 约 75% | Node Template、Recipe、Model Profile、租户策略的发布、固定、影子 | 技能与 MCP 的安装、审批、撤销流程；MCP 现在靠登记文件 |
| M2·2 Strategy | Task Loop（Planner → Stage → Reviewer） | 约 80% | Triage 四级、八种 Verdict、mode 与 quality、tool_loop、真模型评测 | 没有 Swarm；Playbook 分类只靠 Recipe 选择 |
| M3 Context | Context Builder | 约 75% | 五区布局、Compaction Ladder、Compaction Probe、外置内容 | 不留上下文快照（取舍与遗漏）；子代理交接没有有界合同 |
| M4 Memory | Memory Service | 约 65% | 作用域 ACL、写入安全、Promotion、混合检索、级联删除与回滚；Postgres + Qdrant、Milvus 适配器 | Planner 还没接 Memory Service；文档知识的入库与版本（C7）；模型重排；Markdown 长期记忆 |
| M5 Tools | ToolDriver（T-10） | 约 55% | 强类型登记、effect、repo 与 shell 工具、web_fetch、MCP stdio | 没有 recovery 维度与“结果未知”；工具不在隔离环境里跑 |
| M6 BEM（执行模块） | Sandbox 接口 | 约 15% | 本地子进程实现（只限开发） | 没有容器、microVM 等生产实现；没有出口代理；凭据会进节点代码 |
| M7 LLM | Model Gateway（T-05、T-06） | 约 85% | Model Profile、Tier、按期望成本选档、限流、换家、BYOK、计量 | 判定插槽（类型化判定）没有；重试的所有者没写明 |
| 部署与观测 | Helm Chart、OTel（T-15）、Learning Pipeline（T-16） | 约 70% | Chart、Collector 配置、trace、最小学习闭环 | 默认向量库还没切到 Milvus；cell、多地域没有 |

粗算：按 Aster 的模块合起来，大约完成 60%。执行可靠性、模型路由、决策链路这些“难的部分”已经做到了；缺的主要集中在三块：Workspace Manager、生产级的执行隔离（BEM）、更细的授权与资源控制。这三块正好是在线多租户 coding agent 最需要的。

## 2. 对上线 coding agent 的影响

| 缺口 | 对 coding agent 的影响 | MVP 的处理 |
| --- | --- | --- |
| 执行隔离（BEM） | 多租户下不能在宿主机上直接跑用户仓库的命令 | 先做 Docker 沙箱：shell 工具在一次性容器里跑，不联网、限资源（见 [coding agent MVP 计划](../plan/coding-agent-mvp.md)） |
| Workspace Manager | 没有快照、分支、提交与 Apply | 产品层用 Git：每个项目一个克隆，每个 Task 一个分支；diff、提交、下载补丁由产品服务做 |
| 正式登录 | 只有开发令牌 | MVP 用开发登录（租户 + 用户名换令牌），标明仅供测试；正式版接 GitHub OAuth |
| 事件增量 | 界面只能看到节点级进度 | MVP 先展示节点级时间线；token 增量与事件落库（草稿外的 R8）放第二版 |
| 预算预留 | 并发节点可能超支 | MVP 用 Task 预算上限兜底 |
