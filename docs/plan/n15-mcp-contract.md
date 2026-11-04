# N-15 用户 MCP 连接的契约草案

> 状态：契约草案（S-01），2026-11-04，**等人审**。`buf lint`、`buf breaking --against proto/breaking-baseline.binpb` 通过，生成代码已更新。审过之后再写实现，并更新兼容性基线。
> 依据：[N-15/N-16 前置基线](n15-n16-contract-baseline-2026-11-02.md)与它的 10 个候选正反例（`tests/contract/fixtures/n15_n16_contract_cases.json`）；[代审结论](delegated-review-2026-11-04.md)与[后续](delegated-review-followup-2026-11-04.md)的 N-15 行（tenant/user 是归属边界；Server、Tool、Effect 三层都要授权，effect 由受信登记方定，不信远端自报；SecretStore 只给引用、只向授权的 HTTPS 目标注入；网关管会话，撤销后旧租约立即失效）；设计书 4.9（内核只认强类型的工具契约，外部 MCP 由边缘的 MCP Gateway 包装，effect 等人工补齐，未登记的不能调用）。现有的部署侧 stdio 登记（`AR_MCP_TOOLS_FILE`）不变。

## 草案

| 部分 | 草案 |
| --- | --- |
| 服务 | 新的公开服务 `mcp.v1.McpConnectionService`（Task API 同一个端口，经 Ingress 认证）：`CreateMcpConnection`、`ListMcpConnections`、`GetMcpConnection`、`RefreshMcpTools`、`ConfigureMcpTools`、`DeleteMcpConnection`。用户与租户取自认证身份，请求里的 `tenant_id` 只校验一致；只能管自己的连接，别人的与不存在的一样 NotFound（`mcp_connection_not_found`） |
| Server 一层 | 地址只收 https、主机是域名，要落在租户策略 `mcp.hosts` 里；解析到非公网地址（私网、回环、链路本地、CGNAT、云元数据）的拒绝，按校验过的地址拨号（同出网网关的 netguard）。`mcp.allowed` 不是 true 时不能建、不能刷新、不能打开工具（`mcp_not_allowed`），列出、读、删除照常 |
| Tool 一层 | 建连接时连一次（initialize、tools/list，至多 15 秒），工具一律先关着；登记者逐个打开。内核里的名字是 `mcp.<连接名>.<规整后的工具名>`，规整后撞名的打不开（`tool_name_conflict`）。刷新时新出现的先关着，schema 变了（`schema_digest`）的关掉要重新确认，远端没有了的去掉 |
| Effect 一层 | 登记者打开工具时定 effect 与超时：只收 read、write_irreversible（没有回滚，不收 write_reversible）；远端的注解只能往高调（`read_only_hint` 为 true 才能选 read）；不超过租户策略 `mcp.max_effect`（不写为 write_irreversible）；运行时另受 Task 的 effect 上限约束。write_irreversible 的调用逐次人工审批、不自动重试，幂等键按附录 D.4 生成并经 `_meta` 带给远端。超时 1～300 秒，不写为 30 |
| 凭据 | `McpAuth` 只给引用：本人在 UserSecretService 里的 `secret_name` 与请求头名（同 SecretHeader）。值只加在这个连接的主机的 HTTPS 请求上，重定向到别的主机不带；响应、错误、日志、trace、事件里都没有值。删除用户密钥后调用报 `mcp_auth_missing`，不回落到不带头的请求 |
| 租约（实现时，不进 proto） | Task 提交时按当时的设置给这个 Task 发租约：哪些连接的哪些工具、各自的 effect、超时与 schema（业务库一张表，像 `sandbox_grants`）；Task 结束、连接被删除时收回，之后的调用 denied（`mcp_lease_revoked`），进行中的被取消。Planner 的目录与 tool_loop 的工具定义取自租约 |
| 网关（实现时） | Worker 进程里的 MCP Gateway（现有 stdio 那一个）加上 Streamable HTTP：按（连接，Worker）持有会话（`Mcp-Session-Id`），会话断了下次调用时重连；Runtime 不复制会话状态 |
| 生命周期与幂等 | 同名、同地址、同密钥引用再建返回已有的（`created` 为 false），不同的 AlreadyExists（`mcp_connection_exists`）；删除幂等（`deleted` 为 false）；每人至多 `mcp.max_connections`（不写为 10，`mcp_connection_limit`）。连不上照样建好（state 为 ERROR，`error_code` 说明原因） |
| 兼容 | 新服务与租户策略的新一节；旧客户端不受影响。`McpConnectionState` 不认识的值不能当作 READY |
| 租户策略 | `mcp`：`allowed`、`hosts`、`max_connections`（1～50）、`max_effect`（`$defs.mcp_tool_effect`：read、write_irreversible）。正反例在 `schemas/examples/tenant_policy.examples.json` |

## 与候选正反例的对应

| 用例 | 由什么保证 |
| --- | --- |
| mcp_registration_requires_owner | 用户取自认证身份；没有用户的身份 denied |
| mcp_call_requires_allowlist | 主机在 `mcp.hosts` 里、工具打开了、有这个 Task 的租约，三样都要 |
| mcp_tool_requires_effect_metadata | 打开时 effect、超时必填（超时有默认），schema 取自远端；幂等键由内核生成 |
| mcp_secret_ref_only、mcp_wrong_host_secret_denied | `McpAuth` 只有引用；值只加在连接的主机上，重定向不带 |
| mcp_payload_has_no_secret | 同 SEC-API：值只进不出 |
| mcp_connection_tenant_scope | 别人的连接 NotFound；租约按 Task 的租户与提交者发 |
| mcp_disconnect_revokes_lease | 删除时收回租约，之后 `mcp_lease_revoked` |
| mcp_lifecycle_is_idempotent | 同名同设置再建返回已有的；删除幂等；刷新、打开工具重复做结果相同 |
| mcp_old_client_ignores_fields | 新加的字段旧客户端忽略；状态不认识不当成功 |

## 代审时要明确的

1. 第一期只有用户自己的连接；租户里共享的连接（管理员登记、全租户可用）另立。
2. effect 只收 read 与 write_irreversible；选 read 要远端自己说只读（`read_only_hint`）。远端没说的工具只能当不可逆写用（每次人批）。
3. 网关先放在 Worker 进程里（与现有 stdio 网关一起），不单独部署；它直接连远端，按 netguard 的规则拨号，不经出网网关（出网网关是给沙箱的）。
4. 租约在提交时发、按当时的设置：之后改设置（比如把一个工具改成 read）不影响已经在跑的 Task。
5. 远端给的工具说明与 schema 进模型的上下文：当作工具的说明，前面标明来自用户登记的 MCP Server。

## 在实现之前

`McpConnectionService` 没挂到 task-service 上（调用得到 Unimplemented）；租户策略里写了 `mcp` 只经 Registry 校验，运行时不读它。
