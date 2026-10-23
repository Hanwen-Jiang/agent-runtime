# R-05 评审：Ingress 与 SSE

> 评审对象：`6e4c003` 时 task-service 的 Task API（`services/go-task-service/internal/taskapi/server.go`）、SSE 端点与推流
> （`internal/events/events.go`、`internal/session/activities.go`）、事件 id（`internal/taskstore`），开发用 Ingress（`libs/go-core/dev/localingress`）
> 与 Cache（`libs/go-core/dev/memcache`），以及人工回复在 Task 工作流里的处理（`services/py-worker/src/agentruntime_worker/workflows/task.py`）。
> 依据：实施计划第 5 节 R-05 的评审重点；设计书 3.1.1、4.5、4.14、附录 L.15；测试结果。按 6.2 的模板，不看实现过程。评审日期 2026-10-23。
>
> **评审独立性**：同 R-04：实现出自之前的 Claude 会话，评审在新会话里只看代码、设计书与测试结果。计划第 7 节把这部分列为中档实现、
> 强档评审；实际由强档写成，严格按写审分离还要另一家的强模型或人复审。
>
> **范围**：网关产品只定义接口（4.14），这里评的是 task-service 自己做的认证、限流与推流。Cache 只有内存实现，没有 Valkey 适配器。

## 测试结果（评审时）

Go：`internal/events`（Last-Event-ID 续传、心跳、鉴权与归属、Cache 重启后 event_id 不回退、推流丢失后从业务库补发 task_finished）、
`internal/taskapi`、`internal/session`、`libs/go-core/dev/localingress`、`dev/memcache` 全部通过。

## 1. 评审重点逐条

### 1.1 Last-Event-ID 续传

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 续传 | **已处理** | 取请求头 Last-Event-ID 或查询参数 last_event_id，必须是非负整数，否则 400（`events.go:299`）；只推 event_id 大于它的事件（`events.go:210`、`events.go:239`）。 |
| event_id 稳定 | **已处理** | 由业务库分配，从 1 起严格递增，Cache 重启也不回退（`taskstore.go:168`，`TestEventIDsDoNotGoBackwardsAfterTheCacheRestarts`）；订阅从流头开始、按 event_id 过滤，不依赖 Cache 里流的 id（`events.go:161`）。 |
| 已结束的 Task | **已处理** | 读出流里剩下的事件；流丢了就用业务库里的结局补发 task_finished（`events.go:226`，S-07 场景 9）。 |
| 重复推送 | **已处理** | 每类事件只推一次（Cache 里的标记）；推送后、记标记前崩溃会重复一次，客户端按 event_id 去重（`activities.go:94-121`）；已结束 Task 的 task_finished 只发一次（`events.go:242`）。 |
| 推什么 | **未处理** | 只推两类事件：task_accepted 与 task_finished（`activities.go:51`、`activities.go:65`），Worker 一侧不推任何事件。proto 定义的 round_started、stage_started、node_started、node_finished、thinking_delta、tool_status、token_delta、human_input_required、verdict 都没有生产者。设计书 4.14“SSE 推送思考过程、工具状态与 token 增量”没有实现，续传只在两条事件上验证过。客户端从 SSE 得不到“等待人工回复”，要审批只能经 HumanChannel 得知问题。 |

### 1.2 Cache 里推流数据的保留期

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 保留时间 | **已处理（写死）** | 每条事件保留 24 小时（`main.go:270`、`activities.go:25`），不能配置。按条过期：运行超过 24 小时的 Task（例如等审批）早期的事件先过期，续传补不回来；目前只有两类事件，受影响的只有 task_accepted。 |
| 去重标记 | **已处理** | 标记与事件保留期相同；task_finished 另以业务库为准（`activities.go:65-75`），标记过期也不会重推。 |
| 容量上限 | **不确定** | 只按时间清理，没有按条数或字节的上限。等 token_delta 推起来，单个 Task 的流会很大：内存实现全在内存里，Valkey 的 Stream 要配 MAXLEN。 |
| 跨节点广播 | **未处理** | 设计书 4.5 用 Cache 的发布订阅做 SSE 跨节点广播；内存实现只在本进程内可见，task-service 多副本时，Session activity 写进 A 副本的事件，连在 B 副本上的客户端收不到。要等 Valkey 适配器。 |

### 1.3 控制指令的鉴权

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 认证与租户 | **已处理** | 提交、取消、信号、查询都先认证、再按路由限流（`server.go:142`）；租户取自认证身份，请求里的 tenant_id 只校验一致（`server.go:171`）；别的租户的 Task 一律 NotFound（`server.go:175-181`）。错误不带底层原因与凭据（`server.go:430`）。 |
| SSE 的认证 | **已处理** | Bearer 令牌，确认 Task 属于该租户（`events.go:121-148`）；Cache 按租户绑定。 |
| SSE 的限流 | **未处理** | SSE 端点不调用 `Ingress.Admit`（`events.go:119`）。每个连接占一个订阅与一个 goroutine，没有按租户的速率或连接数限制；4.14 的 Ingress 接口包括限流。 |
| 连接超过令牌有效期 | **未处理** | 只在建立连接时认证；令牌过期后流照样推下去。Ingress 算出了 ExpiresAt（`localingress.go:89`），SSE 没用。过期或吊销的身份可以一直收事件。 |
| 谁能审批 | **未处理** | 同一租户里任何认证用户都能对任何 Task 发人工回复，包括 write_irreversible 的审批（`server.go:326-358`）：只记下 replied_by（`server.go:349`，进 GateRecord.approved_by），不检查这个人有没有审批权。Identity 有 Scopes 字段（`ingress.go:31`），没有任何地方用。取消同理：同租户任何用户都能取消别人的 Task。设计书只说“控制指令经 HTTP POST 到达，转为工作流的 cancel 或信号”（3.1.1 第 5 条），没有规定谁能审批，需要人定规则。 |
| 回复不匹配 | **未处理（静默）** | Task 工作流对问题 id 或选项对不上的回复只记一条警告就丢掉（`task.py:203-209`），API 却返回成功（信号是单向的）：客户端以为审批已经送到，Task 还在等，直到问题超时。 |

## 2. 其他风险

### 2.1 确定性

task-service 这一侧没有工作流代码的改动问题；人工回复在 Task 工作流里按问题 id 查 dict 匹配，不依赖遍历顺序。没有发现确定性风险。

### 2.2 并发

- SSE 处理器用原生 goroutine 与 channel（`events.go:176`）：这是 HTTP 处理器，不是工作流代码，不受 Temporal 的限制；请求结束时上下文取消，
  goroutine 退出（`events.go:182`），没有泄漏。
- 运行中 Task 的每次重连都从流头读起（`events.go:162`），代价随事件数线性增长。现在只有两类事件，无所谓；推 token 增量以后要按 event_id 定位。

### 2.3 租户隔离

- **已处理**：见 1.3，`TestStreamRequiresAuthAndOwnership`。
- SSE 只认 Authorization 头，不接受查询参数里的令牌（令牌进 URL 会进访问日志），这是对的；但浏览器的 EventSource 带不了 Authorization 头，
  浏览器客户端要靠网关把会话 Cookie 换成 Bearer，部署时要配。

## 3. 与设计书不一致的地方

1. 4.14：SSE 只推受理与结束两类事件，没有思考过程、工具状态、token 增量，也没有等待人工回复。
2. 4.14：Ingress 的限流没有覆盖 SSE。
3. 4.5：SSE 跨节点广播没有实现（只有内存 Cache）。

## 4. 处理建议

**本期修**：

1. SSE 在令牌过期时结束这条流；客户端带新令牌与 Last-Event-ID 重连。
2. SSE 经 `Ingress.Admit` 限流（路由 `stream_events`），超限回 429 并带 Retry-After。

**需要人定**：

- 审批权：谁能回复人工问题、谁能取消 Task（见回复里的决定项）。
- 进度事件推流：生产者在 Python Worker，而 event_id 由 task-service 的业务库分配，要定事件从哪里写、怎么分配 id，适合单列一个工作项。

**登记到后续**（T-17 时写进 `docs/reference/risks-and-open-questions.md`）：Valkey 适配器与跨节点广播；流的条数上限；保留时间可配置；
重连按 event_id 定位读；人工回复改用带校验的 Temporal Update，对不上的回复当场拒绝（改 Task 工作流，属于核心链路）；
浏览器客户端的会话换令牌（部署层）。

## 5. 处理结果（2026-10-23）

人确认了决定 D4、D5、D9、D10。验收测试先行：`services/go-task-service/internal/events/r05_test.go`、`internal/taskapi/control_auth_test.go`、
`cmd/task-service/main_test.go` 的 `TestDevTokenCarriesScopes`。

1. SSE 在令牌过期时结束这条流：每次心跳与每个事件之前按 `HandlerDeps.Clock` 检查。生产心跳 15 秒，过期后最多再过 15 秒断开；
   客户端带新令牌与 Last-Event-ID 重连。
2. SSE 经 `Ingress.Admit` 限流，路由 `stream_events`，按（租户、路由）计数，与提交、查询等分开；超限回 429，带 Retry-After（秒，至少 1）。
   限的是建立连接的速率，不是同时在线的连接数，后者仍登记。
3. 审批与取消权（D9）：业务库 tasks 加 `submitted_by`（迁移 3，SQLite 与 Postgres 同一份）。取消与人工回复只接受提交者，或身份带
   `task.approve` scope 的用户；否则 PermissionDenied（code `not_task_owner`），请求不触达 DurableEngine。查询仍对同租户开放。
   迁移之前提交的 Task 没有提交者，只接受带 scope 的用户。开发令牌加可选的 scope（载荷键 `s`）：`task-service dev-token --scope task.approve`。
   没覆盖：经 HumanChannel 送回的回复（开发实现是 Worker 控制台）不经 Task API，不受这条约束，回复人以通道给出的为准。
4. 进度事件推流（D10）单列为 TMP 计划的 T-11b，排在 T-13 之前。已完成（2026-10-23）：round_started、stage_started、
   node_finished、human_input_required、verdict 经跨语言 activity `publish_task_event` 交给 Session Worker 推进 SSE，细节见 TMP 计划
   T-11b 的结果。1.1“推什么”一项的思考过程、工具状态与 token 增量仍未处理，已登记。
