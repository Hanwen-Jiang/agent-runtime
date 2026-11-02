# N-19 / N-20 会话流与延迟验收基线（候选）

> 状态：候选基线，未修改 `/proto`、`/schemas`、前端或模型调用链。
> 日期：2026-11-02。现有 SSE 事件和 `Last-Event-ID` 续传已经可用；本文件只冻结下一阶段要验收的快照、增量和延迟边界。

## 当前事实

- `TaskEvent.event_id` 在单个 Task 流内单调递增；客户端用 `Last-Event-ID` 续传。
- 当前已生产并测试的事件包括 `round_started`、`stage_started`、`node_started`、`node_finished`、`tool_status`、`human_input_required`、`verdict`、`decision_retry`、`guidance_updated`、`context_usage` 和任务结束事件。
- Worker 当前通过非流式 `Generate` 调用模型；`thinking_delta` 和 `token_delta` 没有生产者。它们必须等 `StreamGenerate` 的顺序、节流、取消和最终 usage 语义冻结后再接入。
- `sandbox_startup_ms` 只在冷启动的 `tool_status` 中出现；这可以作为 N-20 的现有时间段事实。

## N-19 快照与增量

首屏快照候选包含：会话可见的 Task 摘要、每个 Task 最近事件、每个 Task 的最后事件游标和快照生成时间。快照之后只发送 `event_id > cursor` 的增量。

验收边界：

1. 同一会话只能看到授权租户和会话范围内的 Task。
2. 快照中的最后游标与随后增量的首个事件之间不能丢事件，也不能重复应用。
3. 断线重连用 `Last-Event-ID` 请求增量；游标过期时返回“需要重新快照”的明确结果。
4. 增量应用按 `event_id` 幂等，旧事件不能覆盖新状态。
5. 事件 payload 继续遵守现有脱敏规则；快照不包含 SecretStore 值、代理凭据或完整工具输出。

## N-20 延迟分段

首个可见事件的时间拆成：`accepted_at → queued_at → planner_started_at → sandbox_ready_at → first_event_at`。如果模型采用流式通道，再增加 `first_token_at`；没有流式通道时不伪造该字段。

每段记录只保存时间戳或毫秒数，不复制 Task State，不创建后台聚合器。缺失段标为 `unknown`，旧客户端继续忽略未知字段。

## 复杂度决策

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 快照 + 增量 | 重连后重复渲染或丢事件 | 当前只有事件流，没有首屏统一游标 | 复用现有 `event_id`，增加一次快照读取和游标 | 首屏更快，重连可恢复 | 一次快照读取和有限事件序列化 | 中，需 SSE/前端协同 | 需设置快照大小上限 | 快照与增量游标不一致 | S-01/N-19 | DEFER，先完成人工契约评审 |
| `token_delta` / `thinking_delta` | UI 无法显示模型增量 | 当前 Worker 使用 `Generate` | 先冻结 `StreamGenerate` 分片、取消、usage、节流语义 | 支持逐 token UI | 更多事件和连接带宽 | 高，涉及 Gateway、Worker、SSE、前端 | 需限速、保留和重放策略 | 分片乱序、断流、重复 | N-19 后续 | DEFER |
| 延迟分段 | 无法定位首事件慢 | 已有 `sandbox_startup_ms`，但排队/规划未统一记录 | 复用现有事件时间和 trace，先补四段时间字段 | 可定位排队、规划、冷启动瓶颈 | 少量 span/event 属性 | 中 | 看板需新增查询 | 时钟来源不一致 | N-20 | KEEP 作为小切片 |

## 进入实现前的 S-01 门槛

- 确定快照是否复用现有 Task API，还是新增会话快照接口。
- 确定游标过期、事件保留和快照大小上限。
- 确定延迟字段放在现有 payload、trace attributes 还是新的事件类型。
- 确定 `StreamGenerate` 的首分片重试、客户端取消、最终 usage 和成本结算。
- 先补正反验收测试，再修改 proto/schema 或前端；当前阶段不添加任何生成字段。
