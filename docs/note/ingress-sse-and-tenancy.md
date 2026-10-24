# Ingress、SSE 与租户隔离：Task API、续传、进度事件、Claim-Check

> 学习笔记，2026-10-24。对应代码：task-service 的 Task API `services/go-task-service/internal/taskapi/`（`server.go`、`policy.go`）、
> 事件与 SSE `internal/events/events.go`、业务库 `internal/taskstore/`、Session 工作流推事件 `internal/session/activities.go`；租户上下文 `libs/go-core/tenancy`、
> Python 同名模块 `libs/py-core/src/agentruntime_core/tenancy.py`；Claim-Check `libs/py-core/src/agentruntime_core/adapters/claim_check.py` 与 Go 的 `adapters/temporal/claimcheck`；
> Postgres 行级安全 `libs/go-core/adapters/postgres`。设计依据：设计书 1.1、3.1.1、4.3、4.7、4.14，附录 K.3、L.8；实施计划 S-06、S-12；评审 R-05。

## 1. 一个 Task 从哪进来

```
客户端 POST SubmitTask（Bearer 令牌）
→ Ingress 认证：租户、用户由令牌决定；请求里的 tenant_id 只用来校验一致，不一致就拒绝
→ 限流（按租户与路由计数）
→ 提交去重：同一租户的 client_request_id 返回同一个 task_id
→ 租户策略给出 effect 上限与预算；Registry 给出固定版本
→ 信号进 Session 工作流 → 启动 Task 子工作流
→ 返回 task_id、session_id 与 SSE 订阅地址
```

控制指令（取消、人工回复）也走 Task API，转成工作流的取消或信号。**谁能审批与取消**：只有提交者，或带 `task.approve` scope 的用户；查询对同租户开放。
别的租户的 Task 看起来和不存在一模一样（not found），不泄露存在性。

## 2. SSE：推进度、可续传

- 事件：task_accepted、round_started、stage_started、node_finished（顶层 Node）、human_input_required（带 question_id、kind、选项、deadline）、verdict、task_finished。
  载荷不带结果与 rationale。
- **event_id 由业务库分配**，从 1 起严格递增；Cache 重启、流里的 id 从头再来，event_id 也不回退。客户端断线后带 `Last-Event-ID` 重连，从下一条接着收。
- Cache 只存可丢失的数据：Task 已经结束、流却丢了时，SSE 端点用业务库里的结局补发 task_finished。
- Python 的 Task 工作流经跨语言 activity `publish_task_event`（session 队列）把事件交给 Go 的 Session Worker，由它分配 id、推进 Cache；每个事件都等推送完成，
  推送失败不影响 Task（这一 Round 余下的不再推）。
- 令牌过期就断开这条流（每次心跳与每个事件之前检查）；建立连接经 Ingress 限流。
- 还没做：思考、工具状态与 token 的增量；多副本时的跨节点广播（要 Valkey）。

## 3. 租户隔离：每一层都要拒绝跨租户

| 层 | 做法 |
| --- | --- |
| 接口 | Provider 客户端一律按请求绑定租户：`bind(tenant)` 之后的调用不再传租户；缺租户 denied（missing_tenant），格式不对 invalid |
| 业务库 | Postgres 行级安全：事务开始时设会话变量，归还连接池时清除；SQL 不写租户条件，由 RelationalStore 保证；连接角色不能绕过 RLS |
| ObjectStore | 对象路径带租户前缀（`obj://<租户>/…`），按租户作用域读写 |
| 向量库 | Qdrant 租户字段分区与强制过滤（Memory Service） |
| 检索 | 过滤条件由服务端按身份拼接，永远不由模型生成 |
| 跨租户读取 | Learning Pipeline 这类离线任务用平台作用域并留审计，不复用在线服务的身份 |

越权测试套件以 A 租户身份对每个接口、每个服务读写 B 租户的数据，断言全部被拒或返回空，进 CI 与夜间任务（Postgres + Qdrant 后端）。

## 4. Claim-Check：大载荷不进事件历史

Temporal 的载荷有上限，Task State 会越长越大。超过阈值（默认 64 KiB）的载荷加密后上传到 ObjectStore，事件历史里只留引用：

- 对象键 `claim-check/<原载荷 sha256>`，在租户自己的作用域里；内容是 `ARCC | 版本 | 头部长度 | 头部 JSON（alg、key、nonce）| 密文`，按租户的数据密钥加密。
- Go 与 Python 同一种格式，有跨语言的测试向量；引用格式带版本号，给以后换成 Temporal 官方的 External Storage 留路。
- 解码别的租户的引用一律 denied；密钥不对 invalid；对象不在（过了保留期）not_found；ObjectStore 暂时不可用先本地重试，再报 transient。
- 对象必须活得比 Temporal 的 retention 长。task-service、Worker 与回放工具都要指向同一个 ObjectStore 与 SecretStore。

## 5. 面试速查

**Q：请求里带了 tenant_id，为什么不直接用？**
A：请求是客户端说了算的，可以伪造。租户由服务端根据认证身份确定，请求里的只用来校验一致；不一致直接拒绝。

**Q：行级安全和连接池怎么配合才不会串租户？**
A：租户变量要在事务或会话开始时设置、连接归还时重置；否则下一个借到这条连接的请求会带着上一个租户的变量。还要保证连接的数据库角色不是超级用户、不带 BYPASSRLS。

**Q：SSE 断线续传怎么做？Cache 丢了怎么办？**
A：每个事件有业务库分配的、严格递增的 event_id；客户端带 Last-Event-ID 重连，服务端从下一条推。Cache 只是加速，丢了也能从业务库补发最终结局，id 不回退。

**Q：为什么跨租户访问要返回 not found 而不是 forbidden？**
A：forbidden 会泄露“这个 ID 存在”，攻击者可以枚举。对外一律看起来不存在。

**Q：Claim-Check 是什么？为什么需要？**
A：Temporal 事件历史里的载荷有大小限制，大载荷还会拖慢重放。把大载荷放进对象存储、历史里只存引用；按租户密钥加密、路径带租户前缀，跨租户解码直接拒绝。
