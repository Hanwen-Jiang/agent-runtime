# 工具与凭据：ToolDriver、平台内工具、MCP Gateway 与 SecretStore

> 学习笔记，2026-10-24。对应代码：`services/py-worker/src/agentruntime_worker/tools/`（`platform.py` 平台内工具、`mcp.py` MCP Gateway、`catalog.py` ToolCatalog）；
> 接口与开发实现 `libs/py-core/src/agentruntime_core/providers/tool_driver.py`、`dev/tool_driver.py`、`providers/secret_store.py`、`dev/secret_store.py`、`dev/sandbox.py`；
> Node Executor 的 `call_tool` 与凭据签发 `executor/node_executor.py`。设计依据：设计书 4.9、4.10、4.12；细化 `docs/plan/tool-driver.md`；评审 R-04。

## 1. ToolDriver：工具的登记与调用

每个工具登记一份 ToolSpec：名字、版本、说明、输入与输出的 JSON Schema、**effect**、permissions、超时、是否支持“按幂等键查询”。登记是唯一的事实来源：

- `resolve_catalog` 给 Goal Node 的工具 effect、tool_loop 给模型的工具定义、Planner 的 Node Catalog 里的工具，都取自同一份登记（同名取最大版本）。
- **没登记的工具按 write_irreversible 处理**，调用被拒。
- Node Executor 调用时按登记再复核一次 effect：工具的 effect 高于工作流批准的就拒绝。
- 工具版本固定到 Task 还没做（登记）。

## 2. 平台内工具（按配置登记）

| 工具 | effect | 什么时候登记 |
| --- | --- | --- |
| repo_read、repo_list、repo_grep | read | 设了 `AR_TOOL_WORKSPACE_ROOT` |
| repo_edit | write_reversible | 同上 |
| shell_run | write_reversible | 同上；**不隔离，仅限开发**（argv 列表、不经 shell） |
| web_fetch | read | 设了 `AR_TOOL_FETCH_HOSTS`（主机名单，拒绝内网地址，不跟随重定向） |
| context_read | read | 总是：按 `obj://` 路径取回 Context Builder 外置或遮蔽的内容与 Node 的外置结果，只读本租户的 |

**工作区按租户与 workspace 划分**：`<根>/<租户>/<workspace，没有时为 default>`，租户与 workspace 来自调用的作用域（服务端确定的 TenantContext），不来自参数。
T-13 的真模型 CI 修复就是这样跑的：tool_loop 先 `shell_run` 跑 unittest 看到失败，`repo_read` 读源码，`repo_edit` 改一处，再 `shell_run` 确认通过。

## 3. MCP Gateway

Worker 进程内、stdio 传输：按登记文件（`AR_MCP_TOOLS_FILE`）启动 MCP Server、包装它的工具。**effect 必须人工登记**（不信 MCP Server 自己的说法），不能声明权限。
单独部署、HTTP 传输、MCP 工具的权限、MCP Server 的凭据改由 SecretStore 引用，都登记到后续。

## 4. SecretStore：节点拿不到长期密钥

- 长期密钥（平台的供应商 Key、租户的 BYOK、发布令牌、Claim-Check 的租户数据密钥）都在 SecretStore 里，按作用域隔离。
- 工具登记了 permissions 时，Node Executor 按角色 `tool.<工具名>` 签发**短期凭据**（有效期 min(15 分钟, 工具超时 + 1 分钟)），调用结束吊销；
  凭据只在 activity 里，不进结果、事件历史与日志。工具回显的凭据在出口处换成掩码；吊销失败记 trace 与日志。
- `Secret` 类型的打印形式不含值，Telemetry 再按名字脱敏。
- 开发 Sandbox 只给子进程白名单环境变量（之前把 Worker 的整个环境透传给模型写的代码，包括密钥文件的路径）。
- 生产实现（云 KMS + ESO 管平台密钥、OpenBao 管租户动态凭据）、按前缀批量吊销还没有，已登记。

## 5. 面试速查

**Q：为什么工具的 effect 要人工登记，不信工具自己说的？**
A：effect 决定要不要审批、能不能重试、要不要幂等键。让工具（尤其是第三方 MCP Server）自报，就等于让外部决定安全边界。登记由平台维护，没登记的按最高风险处理。

**Q：Agent 调工具怎么防止越权访问别的租户的文件？**
A：工作区路径由服务端的租户与 workspace 拼出来，参数里只给相对路径，并检查不能逃出工作区；context_read 只读本租户 ObjectStore 作用域里的对象，别的租户的路径一律当作不存在。

**Q：shell_run 安全吗？**
A：开发实现不安全：不隔离、能读工作区以外的文件。所以只在开发环境登记，生产要放进有资源限制、网络隔离的 Sandbox（已登记）。

**Q：模型或工具怎么拿到调用外部 API 的凭据？**
A：拿不到长期密钥。Node Executor 按工具登记的权限向 SecretStore 要一个短期凭据，只交给这次调用，结束就吊销；凭据不进上下文、结果和日志。

**Q：MCP 和平台内工具有什么不同？**
A：平台内工具是我们自己写的、行为可控；MCP 工具来自外部 Server，平台只能包装它：effect 人工登记、不给权限、输出当作不可信数据（进上下文前标为低信任）。
