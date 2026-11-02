# ContextBuilder Activity 接线实施记录（2026-11-03）

对应人工验收基线 Q87–Q93，并完成 Q78 的调用侧任务族、Q81/Q82/Q84 的对象读取边界。此文记录实现增量，不修改已冻结设计书；实现仍按工作项验收，不把设计批准写成实现已验收。

## 实现范围

- Task 开始时通过 `q87-context-builder-v2` 固定 `context_version`，标记追加在 `patches.AT_START` 末尾。没有该标记的历史 Task 继续传 v1；缺少字段的历史 Activity/子工作流输入也默认 v1。子工作流沿用父输入版本，不因在新 Worker 上启动而升级。
- Stage Runner 只复制已有数据到 Activity 输入：完整 Task State、Task Plan、版本、任务族和历史结果。当前 DAG 只取依赖祖先，按依赖顺序排列，之前 Round 的可见成功结果在前；无关并行节点不进入历史。tool_loop 另追加其工具结果。Workflow 不构建提示、不调用模型、不访问对象存储。
- Node Executor 和 ModelToolLoop 在 Activity 内使用真实 ContextBuilder。v2 包括普通生成节点、answer/rewrite 对话节点；未配置真实 Gateway 的开发桩仍保留既有固定提示协议。Task Plan 的 `triage.task_family` 传入 Atomic Model、tool_loop、摘要和 Probe 的现有 `CallContext.task_family`；无 Plan 的 fast 路径与隔离 sub_agent 传已分诊的任务族。
- v2 将完整 Task State 作为 JSON 原样放在精确状态分区，不参与摘要，也不受旧 `state_tokens` 上限截断。系统约束保持原文。先外置/遮蔽旧结果，再摘要旧消息；最近 `keep_recent` 条保留，reset 也不移除它们。仍超过 `effective_tokens` 时返回 `context_window_exceeded`，不静默丢状态。工具定义继续计入 token 预算。
- v2 复用现有 ObjectStore 外置与摘要机制，放在原 Task/Node 路径下的 `v2/` 子路径，避免读取旧算法摘要；未新增表、独立状态机、跨域 hash 或后台任务。rationale 不进入 Activity 下游输入。
- v2 模型结果依赖 State/历史，旧模板与 inputs 缓存键不足以区分这些请求，因此该路径跳过旧缓存，其他执行路径保持原缓存。没有新增缓存系统。
- `context_read` 复用 ToolInvocation 中由 Node Executor 给出的 Task ID，只接受同租户、同 Task 的 `context/<task>/` 或 `node-results/<task>/` 路径。同 Session 不等于跨 Task 授权；跨 Task 与跨租户均返回 not_found，缺少 Task 作用域返回 denied。没有增加新的授权状态。

## 非平凡变更决策

| 字段 | 版本与精确状态 | 对象读取与缓存边界 |
| --- | --- | --- |
| Failure | 升级使历史 Task 换算法；旧实现截断 State、reset 摘要近期消息 | 同租户可读别的 Task 对象；上下文改变仍命中旧模板缓存 |
| Evidence | 旧 Activity 无版本，`_precise_state` 超过 1500 tokens 外置，reset 覆盖全部步骤；Q89/Q90/Q93 直接要求修正 | 原 context_read 只验证 tenant 和目录前缀；原缓存键仅模板与 inputs，未含新接入 State/历史 |
| Minimum fix | 单一 Temporal patch + 输入版本，保留 v1，v2 不压缩固定区与近期区 | 可信 Task ID 与路径段比较；v2 ContextBuilder 模型路径跳过旧缓存 |
| Benefit | 旧任务可重放/重试；精确状态和近期事实不会静默丢失 | 默认拒绝跨 Task 读取；不会把旧上下文答案返给新上下文 |
| Performance cost | 输入大小随已存在状态/可见结果增长；精确 State 占用模型窗口，超长明确失败 | 读取增加一次字符串段比较，无远程查询；v2 模型调用不享受旧结果缓存 |
| Implementation cost | 一个版本字段、一处分支、既有 Builder 局部修改及 Activity/Workflow/replay 用例 | 一个窄作用域检查、一个缓存绕过条件及正反例 |
| Operational cost | 发布时保留 v1 支持，直到不存在历史 Task 的重试/恢复需求 | 无新存储或运维组件；可能增加 v2 模型调用次数 |
| New failure surface | 不支持的版本明确失败；不可压缩部分大于有效窗口时明确失败 | 不传可信 Task ID 的直接工具调用被拒绝；跨 Task 分享需明确授权能力 |
| Release slice | 本次 Q87–Q93 Activity 接线；未声明生产全组件验收完成 | 本次 Q81/Q82/Q84 窄边界修复；新跨 Task 分享入口不在本次范围 |
| Decision | KEEP；版本分支在历史保留窗口结束后才可移除 | KEEP；将来只有具备全部有效输入的缓存方案/权威授权入口才能替换 |

## 验证

依赖从本隔离 worktree 安装，执行前输出 `agentruntime_worker.executor.node_executor.__file__`，确认导入此 worktree 的源码。使用官方 [Temporal CLI v1.9.1](https://github.com/temporalio/cli/releases/tag/v1.9.1) 的 Windows 包，并与该发布的 checksums.txt 校验 SHA256；二进制只放本地 `.tools/temporal/`，不提交、不改全局 PATH。

- Context/Node Executor/tool_loop/Stage Runner/AT_START/replay 组合：180 passed，93.82 秒。包括 38 份既有 golden 历史重放、变异命令序列必须失败、本次测试生成的三类工作流历史重放。
- 最终增量定向回归：34 passed，1 deselected，11.16 秒；再次覆盖全部既有 golden、新 Activity/Workflow、父版本继承及读取边界。该轮不重跑“本次会话三类历史全集”断言（上面的 180 项组合已覆盖）。
- 真实 Temporal 新路径：Task → Stage → 两步 ModelToolLoop Activity → Atomic Node Activity → Reviewer；仅模型供应商、Planner/Reviewer 和业务工具为测试替身，ContextBuilder、NodeExecutor、ModelToolLoop、ObjectStore 和 Workflow 都运行真实代码。解码实际 ActivityTaskScheduled 载荷核对 State/Plan/祖先历史/工具观察/版本/任务族；随后重放该历史。
- 子工作流参数化 v1/v2：新 Worker 支持 v2 时，缺字段的旧父输入仍产生 v1 Activity；v2 父输入产生 v2 Activity。两者都重放通过。
- 真实 Activity 正反例：完整大 State、系统约束、近期结果保留、旧消息摘要、不可压缩溢出/未知版本拒绝、旧输入提示兼容、对话节点、变化 State 不复用旧缓存、context_read 同 Task 成功与同 Session 跨 Task 拒绝。

复现命令使用 `uv run --project services/py-worker --extra test pytest -c services/py-worker/pyproject.toml ...`；Temporal 用例临时将 `.tools/temporal` 加入该命令进程 PATH。未改写任何既有 golden。

## 明确边界

本次验证使用本地 Temporal 1.32.0 开发服务与开发 Provider，未把真实供应商调用、线上 k3s 全组件、真实对象存储生产凭据验收计为通过。Session 对话沿用已有受租户/Session 查询约束的 TranscriptSource，在 Activity 内读取；工具定义与 Memory 同样沿用现有 Activity 依赖，不新增独立快照服务。`effective_tokens` 仍来自现有 Worker 配置，尚未动态接入 Model Profile。跨 Task 对象显式分享没有现成授权入口，本切片按已批准默认拒绝，不通过同 Session 猜测授权。
