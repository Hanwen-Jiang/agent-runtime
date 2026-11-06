# NET-OPEN 公网授权的契约与实现

> 状态：已批准契约的实现完成（2026-11-07），待整版集成与部署验收。此次未改 proto/schema，也未启用任何租户的 open 档位。
> 依据：[环境配置计划](environment-config.md)第 1 节（open = 所有公网域名，默认对所有租户关闭）、第 121、128、143 行（`sandbox.network.modes`、`sandbox_grants.scope`、授权的 `scope: hosts / public`）；[代审结论](delegated-review-2026-11-04.md)与[后续](delegated-review-followup-2026-11-04.md)的 NET-OPEN 行（租户显式允许 open 才能发 public；旧授权没有 scope 按 hosts；拒绝 IP 字面量、非公网地址、云元数据、CGNAT、25 端口；按校验过的地址拨号，重定向重新准入，CONNECT 另核对 SNI；public 不扩大凭据的目标）。

## 草案

| 部分 | 草案 |
| --- | --- |
| 授权的范围 | `taskapi.v1.NetworkRequest.scope`（字段 5，`NetworkScope`）：不写就是 HOSTS（旧客户端与旧授权不变）；HOSTS 只放行 `hosts`；PUBLIC 是所有公网域名，`hosts` 必须为空（否则 `invalid_network_request`） |
| 租户的上限 | 租户策略 `sandbox.network.modes`：`deny`、`allowlist`、`open` 的数组（`$defs.network_policy_mode`），不写为 `["deny", "allowlist"]`。含 `open` 才能申请 PUBLIC（否则 denied，`network_open_not_allowed`）；不含 `allowlist` 时不能申请 HOSTS（`network_allowlist_not_allowed`）。`allowed` 为 false 时一律不联网，`modes` 不起作用 |
| 其余规则 | 批准人（approver）、时长（15、30、60 分钟与 `max_grant_minutes`）、端口（不写为 80 与 443）、用量上限（`max_up_bytes` 等）、只读的 Task 不能申请、到期与 Task 结束时收回：PUBLIC 与 HOSTS 一样 |
| 网关的准入 | 两种范围都拒绝：IP 字面量；解析到非公网地址的名字（私网、回环、链路本地、CGNAT 100.64.0.0/10、云元数据地址，一个地址不合格就整条拒绝）；25 端口。按校验过的地址拨号，不再解析第二次；重定向是另一个请求，重新准入；CONNECT 另核对 TLS ClientHello 的 SNI 与 CONNECT 的主机一致（不一致断开） |
| 凭据 | PUBLIC 不扩大网络密钥的范围：`SecretHeader` 只加在它自己写明的主机（443、TLS）上，与授权的范围无关 |
| 环境的档位 | `workspace.v1.NetworkMode` 不变：环境的 OPEN 是“环境不另加限制”，RESTRICTED 是环境的名单；它们在 Task 的授权之内再收窄，不能放宽。环境是 OPEN、Task 的授权是 HOSTS 时照旧只通授权的主机 |

## 实现时怎么接（不改契约）

- task-service：`CheckNetworkRequest` 按 `modes` 与 `scope` 核对（去掉现在的 `network_scope_unavailable`）；`NetworkPolicy` 加 `Modes`；`sandbox_grants` 加 `scope` 列（迁移，默认 `hosts`，旧行即 hosts）；`sandboxgrants.WriteSubmitGrant` 写它。
- Worker：`request_network` 的参数加可选的 `scope`（`hosts`、`public`，不写为 hosts），审批的问题里写明“所有公网域名”；`agentruntime_worker.sandbox.grants.check_request` 与 Go 一侧同一组用例（两边的 network 测试）。
- 出网网关：`scope` 为 public 的授权按上面“网关的准入”放行；HOSTS 照旧按 `grant.hosts`。拒绝的理由写进逐连接审计（`public_private_address`、`public_ip_literal`、`port_25`、`sni_mismatch`）。
- coding-agent：联网申请的界面在租户允许 open 时多一个“所有公网”选项。

## 验收（实现时先写成测试）

1. 不写 `modes` 的租户：HOSTS 照旧，PUBLIC → `network_open_not_allowed`；旧客户端（不写 scope）的申请与授权行为不变。
2. `modes` 含 open：PUBLIC 被批准，写成 `scope = public` 的授权；`hosts` 不为空的 PUBLIC → `invalid_network_request`。
3. `modes` 只有 `["deny", "open"]`：HOSTS → `network_allowlist_not_allowed`。
4. 网关：public 授权下，公网域名放行；IP 字面量、解析到 10.0.0.0/8、127.0.0.1、169.254.169.254、100.64.0.1 的名字、25 端口全拒（与 D-16 的 SSRF 用例同一组）；DNS 重绑（第二次解析变内网）不生效，因为只按第一次校验过的地址拨号；重定向到内网地址的下一个请求被拒；CONNECT 的 SNI 与主机不一致断开。
5. public 授权下，向没在 `SecretHeader` 里写明的主机的请求不带网络密钥。
6. 用量上限、到期、Task 结束与撤销对 public 授权同样生效。
7. 租户策略的四处（`modes`、授权的 `scope`、普通主机匹配、CONNECT 的 SNI）都有新旧客户端的用例。

## 代审时要明确的

1. 档位的名字：租户策略里叫 `open`，授权的范围叫 `public`（环境配置计划第 121、143 行就是这样分开的）；环境的 `NetworkMode` 另有 OPEN、RESTRICTED、DENY，意思是“环境另加的限制”，不改名。
2. `modes` 不含 allowlist 时拒绝 HOSTS 的申请（租户只想要全通或全断时可以这样写）。
3. 25 端口在两种范围里都拒绝：现在的提交核对不拒 25（网关拒），实现时提交时就拒。

## 实现记录（2026-11-07）

- Go/Python 同时执行 `modes` 与 `scope`，旧客户端默认为 hosts；PUBLIC+hosts、未知 scope、25 端口拒绝。提交与工具审批沿用时长、批准人、effect 与用量规则。
- Sandbox Service 迁移 **211 sandbox_grant_scope** 为共享表增加非空 `scope`，默认 hosts；旧授权不扩权。Go 提交、Python 工具、Sandbox Service 与网关读写一致。
- public 复用网关的逐次 DNS/IP 检查、固定地址拨号、环境限制、计量、到期和撤销；CONNECT 的 SNI 必须等于目标主机。SecretHeader 仍只注入配置的 HTTPS 主机。只有静态域名白名单且不经网关的后端不能承载 public，按现有需网关的授权筛选流程排除。
- 已认证的 `GET /v1/network-policy` 只返回当前用户所属租户的联网投影（allowed/modes/hosts/max_grant_minutes/approver），不接收租户选择器；`Cache-Control: no-store`。coding-web 的同名 `/api/network-policy` 转发用户令牌，提交时把显式选择的 scope 写到已有 NetworkRequest；所有公网选项仅在策略允许 open 时出现，成功提交后清空本次选择。
- 审批文案明确 public 是“所有公网域名”。这次只改审批文本，不改变工作流命令序列。
- 新测试覆盖 Go/Python scope 规则、真实 SubmitTask 的 public 授权落库、旧授权迁移、认证投影隔离、PUBLIC 的 SSRF/重定向/SNI/环境限制/密钥目标/限流/撤销、web 转发；并保留已有网关回归。
- 额外修复网关测试夹具清理竞态：等待被 hijack 的连接完成审计后再关闭临时数据库。

部署顺序：先由 Sandbox Service 完成共享数据库迁移 211，再切读取/写入 scope 的 task-service、Worker、egress-gateway 与 coding-web；运行时当前策略默认不变。线上验证使用独立允许 open 的测试租户，验证后撤销该测试授权。此次未执行部署与真实公网探测。
