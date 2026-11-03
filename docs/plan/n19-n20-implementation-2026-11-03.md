# N-19 / N-20 实施记录（2026-11-03，E 线）

对应 [feedback-2026-11-01.md](feedback-2026-11-01.md) N-19（会话流：快照加增量）、N-20（Runtime 一侧的首事件延迟），人工验收基线
Q57～Q62，候选基线 [n19-n20-contract-baseline.md](n19-n20-contract-baseline.md) 与契约清单 `tests/contract/fixtures/n19_n20_contract_cases.json`。
前端（局部更新、虚拟化、token 合并到每帧一次）是 D 线的事；这里只做 Runtime 一侧。

## 契约（CONTRACT DRAFT 提交；2026-11-03 人审通过，已合进 main）

- `schemas/session_snapshot.schema.json`（新）：`GET /v1/sessions/{session_id}/snapshot` 的响应；`defs` 加 `snapshot_task_status`、
  `snapshot_source`、`snapshot_error_code`。没有 proto 改动：快照是 Ingress 上与 SSE 并列的 HTTP 只读端点，不新增 gRPC 方法。
- SSE 的游标过期：`409` 与 `{"code": "resnapshot_required"}`（写在快照 schema 的说明里）。
- 跟进的一条：退回记录时没结束的 Task 游标为 0（见下面“退回”）。
- 首事件延迟没有改契约：只进 trace 与指标（基线“缺失段标为 unknown，旧客户端继续忽略未知字段”，这里干脆不进事件）。

## N-19 实现

- **快照**（`internal/events/snapshot.go`）：与 SSE 同一套认证与按租户限流（单独的路由键 `session_snapshot`），Session 不存在或属于
  别的租户一律 404。取最近的 `tasks`（默认 20，最多 50）个 Task，按提交先后排；每个 Task 读它的事件流，留最近 `events`（默认 50，
  最多 200）条（与 SSE 的 data 相同的 JSON）、最新的一份 `plan_updated`（N-04）与游标 `last_event_id`。只读：不写 Cache、不建表、
  不起后台任务、不缓存快照。
- **游标**：快照之后客户端对没结束的 Task 用 `last_event_id` 作 Last-Event-ID 订阅 SSE；SSE 只推 `event_id` 更大的，按 event_id
  幂等应用（Q61）。顺序由 Task 工作流这一个写者推、业务库分配 event_id 保证（Q60），客户端不排序。
- **退回（Q58）**：事件流读不出来（Cache 不可用）或是空的而业务库分配过 event_id（流丢了），这个 Task 退回业务库的记录
  （`source: record`，结束的带补发的 `task_finished`）并在 `errors` 报 `events_unavailable`，不悄悄给空。解不开的一条事件跳过并报
  `event_undecodable`，游标照样往后。没结束的 Task 退回时游标为 0：业务库的计数在“分配了 event_id、还没追加进流”的那一刻领先，
  拿它作游标会丢掉那一条；从 0 续传只是多补几条，按 event_id 去重。
- **游标过期**：SSE 的 Last-Event-ID 早于流里最早的一条（中间的过了保留期），或流丢了而业务库记着更晚的，返回 409
  `resnapshot_required`，不再悄悄跳过事件。结束的 Task 照旧补发 `task_finished`（最终事件可恢复，Q62）。

## N-20 实现

- 口径（Q59）：请求接收（Task Service 写进工作流输入的 `submitted_at`）→ 第一条可展示事件（`plan_updated`、`stage_started`、
  `node_started`、`tool_status`、`human_input_required`、`verdict`、`token_delta`、`thinking_delta`；都没有时是 `task_finished`）。
- 分段：`queue`（submitted_at → 工作流开始跑）、`planning`（第一轮规划，含重试）、`sandbox_cold_start`（第一次 `tool_status` 的
  `sandbox_startup_ms`，复用环境时没有）、`first_token`（第一次流式生成从发出请求到第一个分片的用时；没开流式时记 `unknown`，不伪造）、`first_event`（总数）。
- Task 工作流只记时间（`workflow.now()`，不加命令、不加版本分支），收尾时随 `finalize_task` 的输入（新的可选字段）交给 activity，
  由它记 `runtime.task.first_event` span（各段 `runtime.latency.<段>_ms`，缺的为 `unknown`）与指标 `runtime.task.first_event_ms`
  （属性 `segment`，只记已知的段）。子 Task 与之前的运行不记。

- **流式（只量首个 token）**：`GrpcModelGateway.stream_generate` 调网关的 StreamGenerate（`text_delta`、`thinking_delta` 交给回调，
  返回最后一个分片的完整结果与 usage；没有最后一个分片就结束的按 transient）；`model_gateway.generate_streaming` 在客户端支持时走它
  并量首个分片的用时，不支持时退回 Generate、用时为 None。Worker 设 `AR_STREAM_DELIVER=1` 时 deliver 节点走这条路，
  `meta.first_token_ms` 由 Stage Runner 交给首事件延迟（只记时间）。默认关：真模型下结构化输出经流式的行为还没在线上验收。

## 决策表

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 快照端点 | 首屏要逐个 Task 读完整事件流，重连后重复或丢 | 只有逐 Task 的 SSE | 一个只读 HTTP 端点，复用事件流与业务库 | 首屏一次请求，游标对齐 | 每次读 N 个 Task 的流（内存 Cache） | 中 | 无新组件 | 很长的会话读流变慢（有上限） | N-19 | KEEP |
| 快照缓存/持久化 | 读不出流时没东西可给 | Q58 | 退回业务库的记录（已有的持久状态） | 不加第二份状态 | 无 | 低 | 无 | 记录只有终态与游标，没有中间事件 | N-19 | KEEP；不另存快照（REJECT） |
| 409 resnapshot | 过期游标静默丢事件 | 原 SSE 从头订阅、按 id 过滤，看不出缺口 | 订阅前看流里最早一条 | 丢事件变成明确结果 | 一次读一条 | 低 | 客户端要处理 409 | 分配后未追加的 id（崩溃）会误报一次，重取快照即可 | N-19 | KEEP |
| 延迟只进 trace/指标 | 定位不了首事件慢在哪 | 只有 sandbox_startup_ms | 工作流记时间、收尾 activity 记 span 与指标 | 不改命令序列、不改事件契约 | 收尾多一个 span、几个指标 | 低 | 看板要加查询 | 两个时钟来源（task-service 与 Temporal） | N-20 | KEEP |
| 首个 token | 只调 Generate 时量不到 | 网关已实现 StreamGenerate | deliver 节点可选走流式，量第一个分片 | 首个 token 有实测 | 无额外调用 | 低 | 打开要看线上的结构化输出 | 流中途出错按 transient 重试整次调用 | N-20 | KEEP，默认关；片段的推送见“没做的” |

## 验证

- Go：`internal/events` 新增 8 条（快照的游标与最近事件与计划、快照之后的增量不丢不重、游标过期 409、流丢失退回记录、Cache 不可用
  退回、解不开的事件跳过、结束的不重复、租户与 Session 范围与参数），`internal/taskstore` 照常通过。
- Python：`tests/test_n20_first_event_latency.py` 6 条（分段与 unknown、工作流交给收尾、复用环境无冷启动、子 Task 不记、收尾的 span 与指标）；
  `tests/test_n20_streaming.py` 6 条（gRPC 客户端的分片与最后一个分片、没有最后一个分片、首个 token 的量法与退回、deliver 节点走流式、别的节点与默认不走、环境变量）。
- 契约：快照 schema 的正反例（含退回记录）、`test_enum_parity`、`test_schema_examples` 通过。

## 没做的

- **token_delta / thinking_delta 的推送**：网关的 StreamGenerate 已实现（只在第一个分片之前重试与切换），Worker 还没接。缺的是从
  activity 到事件流的通道：事件流在 task-service 进程内的 Cache 里，进度事件只能由工作流经 Session Worker 的 activity 推，
  逐 token 走工作流会撑爆事件历史。可行做法（待人定）：task-service 的内部端口加一个只收 `token_delta`/`thinking_delta` 的 HTTP
  入口（平台令牌在 SecretStore），不分配 event_id、只进 Cache，SSE 只把“最新一条持久事件之后”的增量推给在线的连接，载荷带
  `offset` 供客户端去重；Worker 一侧的 StreamGenerate 已接上（见上），片段按时间与字数合并后尽力推送即可，丢了不影响结局（Q62）。新增的是一个内部入口与令牌，属于安全评审范围，这一轮不擅自开。
- 看板：Grafana 面板加 `runtime.task.first_event_ms` 按 segment 的分位数（部署线）。
- coding-web 用快照画首屏、处理 409（D 线）。
