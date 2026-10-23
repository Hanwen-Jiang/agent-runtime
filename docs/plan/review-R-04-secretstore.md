# R-04 评审：SecretStore 接入

> 评审对象：`6e4c003` 时的 SecretStore 接口与开发实现（`libs/py-core/src/agentruntime_core/providers/secret_store.py`、`dev/secret_store.py`，
> `libs/go-core/providers/secretstore`、`dev/filesecrets`），以及它的调用方：Node Executor 的短期凭据（`services/py-worker/src/agentruntime_worker/executor/node_executor.py`）、
> 平台内工具与 MCP Gateway（`tools/platform.py`、`tools/mcp.py`）、开发用 Sandbox（`libs/py-core/src/agentruntime_core/dev/sandbox.py`）、
> Model Gateway 的供应商密钥与 BYOK（`libs/go-core/adapters/openaicompat`）、Registry 的发布令牌、Claim-Check 与向量加密的数据密钥。
> 依据：实施计划第 5 节 R-04 的评审重点；设计书 4.12、附录 L.13；CLAUDE.md 硬性规则“其他”第 1 条；测试结果。按 6.2 的模板，不看实现过程。
> 评审日期 2026-10-23。
>
> **评审独立性**：实现出自之前的几个 Claude 会话；这次评审在新会话里只读代码、设计书相关章节与测试结果，没有看实现过程。
> 计划第 7 节把 SecretStore 接入列为中档实现、强档评审，按 1.3 第 2 条这样就够；但这部分实际由强档写成，严格按写审分离，
> 还要另一家的强模型或人复审。
>
> **范围**：只有开发实现（本地 JSON 文件，明文）。设计书 4.12 推荐的云 KMS + External Secrets Operator、Vault 或 OpenBao 都没有接，
> 下面关于生产实现的结论都是“未处理”，在处理建议里登记。

## 测试结果（评审时）

- Go：`libs/go-core/dev/filesecrets`（含 `secretstoretest` 一致性测试）、`dev/localingress`、`dev/memcache`、`adapters/openaicompat`
  （含密钥不外泄的用例）、task-service 的 `internal/taskapi`、`internal/events`、`internal/session` 全部通过。
- Python：py-core 的 `tests/dev/test_secret_store.py`、`test_sandbox.py`、`test_telemetry.py` 共 105 项通过；py-worker 的 Context Builder、
  Node Executor 与 call_tool 相关测试 86 项通过。

## 1. 评审重点逐条

### 1.1 节点拿不到长期密钥

| 执行路径 | 结论 | 位置与说明 |
| --- | --- | --- |
| 模型调用（model 节点、Planner、Reviewer、tool_loop） | **已处理** | Worker 只经 Model Gateway 调模型，不持有供应商密钥；网关每次调用按作用域从 SecretStore 读（BYOK 优先，`provider.go:218`），密钥只在 Authorization 头里（R-02 评审 2.3）。 |
| program 节点与 tool_loop 的工具调用 | **已处理** | 工具登记了 permissions 时，Node Executor 按角色 `tool.<工具名>` 签发短期凭据，有效期取 min(15 分钟, 工具超时 + 1 分钟)，调用结束在 finally 里吊销（`node_executor.py:417-433`）；call_tool 走同一条路径（`node_executor.py:192`）。凭据只在 activity 里，不进 activity 的结果与事件历史。目前登记的真工具（平台内工具、MCP 工具）都不要求权限（`platform.py:70`；`mcp.py:97` 拒绝声明 permissions），这条路径只有测试工具走过。 |
| shell_run 与 MCP Server 的子进程环境 | **已处理** | 只传白名单里的环境变量（`platform.py:39`、`mcp.py:43`），Worker 的 `AR_*` 变量与密钥不传下去。 |
| sandbox_exec（开发用 Sandbox） | **未处理** | `dev/sandbox.py:95` 把 Worker 的整个环境 `{**os.environ}` 传给子进程。模型写的代码能读到 Worker 进程的全部环境变量：`AR_SECRETS_FILE`（指向明文密钥文件，含各作用域的 KEK）、开发与评测时常设的 `AR_MODEL_PROVIDER_API_KEY`、同机跑 task-service 时的 `AR_DEV_SIGNING_KEY`。开发实现自称“不能运行不可信代码”，但它是仓库里唯一的 Sandbox，端到端测试与真模型演示都用它。 |
| shell_run 读文件 | **未处理（已知限制）** | `shell_run` 不隔离（CLAUDE.md 与 `tool-driver.md` 写明仅限开发环境），命令能读工作区以外的任何文件，包括明文密钥文件。 |
| MCP Server 的凭据 | **不确定** | 登记文件的 `env` 原样传给 MCP Server（`mcp.py:164`）。要给 MCP Server 第三方凭据，只能把长期密钥明文写进登记文件（`AR_MCP_TOOLS_FILE`），不经 SecretStore，也不能吊销。MCP Server 不是节点代码，但它替节点调用外部系统；第一期不支持 permissions，凭据怎么交给 MCP Server 待设计（`mcp.py:9`）。 |

### 1.2 凭据不进日志与 trace

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 值类型 | **已处理** | Python `Secret`、Go `secret.Value`：打印、格式化只得掩码，Python 侧禁止序列化（`secrets.py:26-52`）；一致性测试断言 repr、str 与日志文本里都没有明文（`conformance/secret_store.py:178-188`）。 |
| Telemetry | **已处理** | 值为 Secret 的属性，以及键名含 password、secret、token、api_key、authorization、credential、cookie 的属性，记为 [REDACTED]（`dev/telemetry.py:26`；一致性测试 `conformance/telemetry.py:147`）。 |
| 错误信息 | **已处理**（SecretStore 自身与网关） | SecretStore 的错误不带明文；Model Gateway 去掉上游错误里的密钥（R-02）；Registry 比较发布令牌时不打印令牌（`registry/service.go:44-66`）。 |
| 工具回显凭据 | **未处理** | 短期凭据交给工具后，工具的输出或错误信息如果带上凭据原文（例如把请求头、连接串写进错误），会原样进入 Node 结果、tool_loop 的 history（事件历史）、trace 与 Reviewer 的输入：`_tool` 直接返回 `result.output`（`node_executor.py:423-425`），`_from_provider` 把工具错误的 message 原样放进 ApplicationError（`node_executor.py:329`）。代码手里有这次签发的凭据明文，出口处可以查一遍。 |
| 吊销失败 | **未处理（看不见）** | 吊销失败被 `contextlib.suppress(ProviderError)` 吞掉（`node_executor.py:432`），日志、trace 都没有记录；没吊销成功的租约只能等过期，事后也查不到。 |

### 1.3 吊销路径可用

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 短期凭据用完即吊销 | **已处理** | 见 1.1；Task 被取消时 activity 收到取消，finally 照样吊销。一致性测试覆盖：吊销后立即 REVOKED、重复吊销不报错、别的租户吊销不了（`conformance/secret_store.py:120-135`）。 |
| Worker 崩溃时的租约 | **已处理（靠过期）** | 崩溃后没人吊销，租约最多再活 min(15 分钟, 工具超时 + 1 分钟)。开发实现的租约只在内存里，进程重启后查不到（not_found）。 |
| 按租户或 Task 批量吊销 | **未处理** | 接口只能按单个 lease_id 吊销（`providers/secret_store.py:57`）。租户下线、凭据泄露、Task 被判 abort 时，没有一步吊销全部租约的路径。Vault 与 OpenBao 都能按前缀吊销，接口可以照此加一个方法（Provider 接口，不是 proto）。 |
| 静态密钥轮换（BYOK、平台 Key） | **已处理（适配层）/ 未处理（开发实现）** | 适配层每次调用都重新读密钥，不缓存（`provider.go:202`）。但两种语言的本地文件实现只在启动和写入时读文件（Go `filesecrets.go:265`，Python `dev/secret_store.py:145`）：别的进程（例如 `model-gateway dev-secret`）改了密钥文件，正在跑的服务要重启才看得到。生产实现不受影响。 |
| 生产实现 | **未处理** | 没有云 KMS、Vault 或 OpenBao 的适配器；平台密钥、租户凭据与各作用域的 KEK 都以明文存在一个 JSON 文件里。 |

## 2. 其他风险

### 2.1 确定性

所有 SecretStore 调用都在 activity 或服务进程里，工作流代码不碰密钥，没有发现确定性风险。

### 2.2 并发

开发实现的跨进程文件锁只保护写（Python `dev/secret_store.py:103-128`，Go `filesecrets.go:189`），读不重新加载文件，见 1.3。
没有发现数据竞争。

### 2.3 租户隔离

- **已处理**：凭据、租约、数据密钥按作用域隔离，别的租户 not_found 或 denied（一致性测试）；Node Executor 按 Node 的租户绑定（`node_executor.py:418`）。
- **风险**：同一个密钥文件里存着所有租户的明文密钥与 KEK，而 sandbox_exec 与 shell_run 都能读到它（1.1）：任何一个租户的 Task 都能拿到全部租户的密钥。
  这是开发实现的限制，但 T-13 的端到端演示会在这套环境里用真密钥跑这些工具。

## 3. 与设计书不一致的地方

1. 4.12 的分层治理（平台静态密钥用云 KMS + External Secrets Operator，租户动态凭据用 Vault 或 OpenBao）没有实现，只有明文文件。
2. 4.12“节点代码永远拿不到长期密钥”在 sandbox_exec 上不成立：开发 Sandbox 透传 Worker 的环境。
3. 4.12 的“吊销”只能逐个租约，没有按租户或 Task 批量吊销。

## 4. 处理建议

TMP 计划第 0 节第 4 条：未处理的修掉或登记。

**本期修**（小，而且不修会让上面的结论失真）：

1. 开发 Sandbox 只传白名单环境变量（与 shell_run 同一份白名单），不再透传 Worker 的环境。
2. 凭据出口检查：工具的输出与错误信息里出现这次签发的凭据原文时换成掩码，并在 span 上记一个事件（只有工具名与 lease_id）。
3. 吊销失败记下来：span 事件加一条警告日志，只带工具名、lease_id 与错误 code；Node 的结局不受影响。

**登记到后续**（T-17 时写进 `docs/reference/risks-and-open-questions.md`）：生产 SecretStore（云 KMS + ESO 管平台密钥，OpenBao 管租户动态凭据）；
接口加按租户或 Task 前缀批量吊销，Task abort 与租户下线时调用；MCP Server 的凭据改由 SecretStore 引用（登记文件写 `secret:<名字>`，
启动时从平台作用域取）；shell_run 进 Sandbox（与 T-10 登记的资源限制、网络隔离一起做）；开发实现读密钥前检查文件是否变了。

## 5. 处理结果（2026-10-23）

本期修三条已改（人确认了决定 D1–D3），验收测试先行：`libs/py-core/tests/dev/test_sandbox.py` 末尾、`services/py-worker/tests/test_node_executor.py`
里 R-04 的三条。

1. 开发 Sandbox 只传白名单里的环境变量（PATH、PATHEXT、SYSTEMROOT、WINDIR、COMSPEC、TEMP、TMP、HOME、LANG、LC_ALL，另加
   PYTHONIOENCODING），与 shell_run 同一份；Worker 的 `AR_*` 与别的变量都不传给沙箱里的代码。
2. 工具的输出或错误信息里出现这次签发的凭据原文时换成 `***`，类别、code 不变，Node 照常进行；span 事件 `credential_redacted`
   记工具名、lease_id 与位置（output 或 error）。写操作超时后对账取回的结果同样处理。
3. 吊销失败记 span 事件 `credential_revoke_failed`（工具、lease_id、错误 code）与一条警告日志；Node 的结局不变，租约等过期。

“登记到后续”的几条不变。
