# F3：N-18 文件交付的公开入口（契约草案，待 S-01 人审）

> 状态：2026-11-03 第 4 节五条人定照建议（S-01 人审通过）：proto 已加、已生成，兼容性基线已更新。验收测试已写：
> - `services/go-task-service/internal/taskapi/artifacts_test.go`
> - `services/py-worker/tests/test_deliver_file.py`
>
> 测试人审后已实现，两份测试都过了：
> - Worker 的 `agentruntime_worker.tools.deliver`：配了工作区目录时登记；
> - task-service 的 `taskapi/artifacts.go`：`ListTaskArtifacts` 与下载路由；
> - `main.go` 把 Claim-Check 的同一个 ObjectStore 交给下载，并跑迁移 207；
> - 两边的建表语句由 `tests/contract/test_task_artifacts_ddl.py` 比对。
>
> **还没做的**：
> - 第 2 节第 3 条 Reviewer 的 highlights 把文件排前面；
> - 第 2 节第 4 条 coding-web 的交付区（coding-agent 仓库，要先写它的测试）；
> - 第 5 节第 5 条端到端；
> - 存档模式（Cloudflare）的后端读文件：要借沙箱环境，那部分在 C 线正在改的范围里；
> - 对象元数据只写 ASCII，非 ASCII 的文件名只出现在对象键里，S3 上要实测。
>
> 登记在 `work-dag-2026-11-03.md` 的 F 线。
> 前置：`n18-file-delivery-contract-baseline-2026-11-03.md`（正反例已批准）；内部 helper `agentruntime_worker.delivery.FileDelivery`（`e3096a8`）。
>
> 与下文的出入：
> - **下载**：Go 的 ObjectStore 接口只有整个读出的 `Get`，下载按单文件 25 MiB 的上限整个读出再写回，不是流式。
> - **建表**：`task_artifacts` 由 Worker 的迁移 207 建（同 202 的 sandbox_grants：Worker 建，Go 服务只读）。

## 1. 要做到的

Agent 在工作区里做出的文件（报告、表格、图、打好的包）能作为交付给用户：交付区先列文件再给文字（N-18 原话），用户点了就下载；链接不能跨租户、跨 Task 用，过期就失效。

## 2. 做法（不碰 E 线的核心链路）

如果把文件清单放进 `TaskWorkflowResult` 再由 Task 工作流带出，就要改 `workflows/task.py`（E 线独占）并加版本分支。这里改为：**发布文件的工具在 activity 里直接把元数据记进业务库**，task-service 从同一个库读。Worker 与 task-service 本来就共用业务库（D-11、D-06）。工作流的命令序列不变，不用版本分支。

1. **Worker 的工具 `deliver_file`**：
   - 新模块 `agentruntime_worker/tools/deliver.py`，不放进 `tools/platform.py`（C 线独占）。登记进开发用 ToolDriver，effect 为 `write_reversible`。幂等键按附录 D.4，由租户、Task、Node 与输入的哈希生成。
   - 输入：工作区里的相对路径，可选显示名。按工作区现在的模式读文件：沙箱模式经 Sandbox 读，否则直接读工作区目录。
   - 校验：路径规范化，拒绝绝对路径、`..`、符号链接逃逸、保留目录；单文件、单 Task 的个数与总字节数有上限，只能由部署收窄。
   - 写入：经 `FileDelivery.publish` 写进 ObjectStore（`deliveries/<task>/<随机>/<名字>`）。
   - 记录：写一行 `task_artifacts`，见第 3 节。
   - 同一个幂等键重放时返回同一行，不重复写对象。
2. **task-service**：
   - `ListTaskArtifacts` RPC 只给元数据；
   - 下载走 HTTP 路由 `GET /v1/tasks/{task_id}/artifacts/{artifact_id}`，和 SSE 的事件路由一样挂在 Task API 的端口上。
   - 每次下载都重新核对：令牌里的租户、Task 属于这个租户、文件属于这个 Task、没过期。然后从 ObjectStore 读出来流式返回，不重定向到预签名地址（基线里缓存与预签名是 DEFER）。
   - 响应头：`Content-Disposition: attachment`；`Content-Type` 取自允许列表，否则用 `application/octet-stream`；另带 `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: sandbox`。
3. **Reviewer 的 highlights**：收尾时读本 Task 的 `task_artifacts`，把文件排在“重要输出”的最前面（新建还是改动、大小）。改的是 Reviewer 模块，不是 E 线的三个文件。
4. **coding-web**：交付区先列文件再给文字；下载经它自己的后端，用用户的令牌转调 Task API，浏览器拿不到对象存储的地址。

## 3. 契约改动（要人审）

**Task API（`proto/agentruntime/taskapi/v1/task_api.proto`）**，新增，不改已有字段：

```proto
rpc ListTaskArtifacts(ListTaskArtifactsRequest) returns (ListTaskArtifactsResponse);

message ListTaskArtifactsRequest {
  string tenant_id = 1;  // 只用来核对，租户以令牌为准
  string task_id = 2;
}

message TaskArtifact {
  string artifact_id = 1;          // art_<ULID>
  string name = 2;                 // 显示名
  string path = 3;                 // 工作区里的相对路径（规范化后）
  string media_type = 4;
  int64 size_bytes = 5;
  string sha256 = 6;
  google.protobuf.Timestamp created_at = 7;
  google.protobuf.Timestamp expires_at = 8;
  TaskArtifactState state = 9;     // READY、EXPIRED；未知值客户端按不可下载处理
  string download_path = 10;       // /v1/tasks/{task_id}/artifacts/{artifact_id}，相对 Task API 的地址
}

enum TaskArtifactState {
  TASK_ARTIFACT_STATE_UNSPECIFIED = 0;
  TASK_ARTIFACT_STATE_READY = 1;
  TASK_ARTIFACT_STATE_EXPIRED = 2;
}

message ListTaskArtifactsResponse { repeated TaskArtifact artifacts = 1; }
```

**业务表 `task_artifacts`**：
- 字段：`tenant_id`、`task_id`、`artifact_id`、`idempotency_key`（唯一）、`name`、`path`、`media_type`、`size_bytes`、`sha256`、`object_uri`、`created_at`、`expires_at`。
- Postgres 加行级安全，与 `tasks` 同样按租户。
- 状态由 `expires_at` 推出，不另设状态机（基线里 REJECT 的那一条）。
- `object_uri` 只在服务端用，不进响应。

**事件**：不加新的事件类型。Task 结束的事件照旧；客户端在收到结束事件后调 `ListTaskArtifacts`。这样不改 `/schemas`，断线重连也不会重复拿到一次性链接。

## 4. 要人定的

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 第 3 节的 proto 与表 | 按上面加（只加不改，过 `buf breaking`） |
| 2 | 上限 | 单文件 25 MiB、每个 Task 20 个、合计 100 MiB；部署只能收窄 |
| 3 | 保留多久 | 7 天，ObjectStore 对 `deliveries/` 前缀配生命周期规则；到期后列表里显示为 EXPIRED |
| 4 | 下载方式 | 第一版由 task-service 流式转发，每次重新授权；预签名地址以后看量再说 |
| 5 | `deliver_file` 的 effect | `write_reversible`（可删、会过期），不用逐次审批 |

## 5. 审过之后的验收（先写测试）

1. `deliver_file`：
   - 路径的正反例照基线清单 `tests/contract/fixtures/n18_file_delivery_contract_cases.json`；
   - 超过上限时拒绝，不留下对象和记录；
   - 同一个幂等键只写一次；
   - 沙箱与工作区目录两种模式都能读；
   - 文件内容不进日志与 trace。
2. `ListTaskArtifacts`：只列这个租户、这个 Task 的；别的租户拿同一个 `task_id` 查到的是空的或被拒；过期的显示为 EXPIRED。
3. 下载：
   - 越租户、越 Task、过期、伪造的 `artifact_id` 都被拒，错误信息不带对象地址；
   - 响应头符合第 2 节第 2 条；
   - 内容与 sha256 一致；
   - 大文件流式传，不整个读进内存。
4. Postgres 行级安全：换个租户连上看不到别的租户的行（照 S-12a 的越权用例）。
5. 端到端：Task 里调 `deliver_file`，结束后列出文件并下载，在 `tests/e2e` 里跑。
6. coding-web：交付区先显示文件，点击后经后端下载（coding-agent 仓库的测试）。

没覆盖、要部署后验的：真实对象存储的生命周期规则、经 Ingress 的大文件下载、浏览器里的下载体验。
