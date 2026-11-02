# N-15/N-16 契约前置基线（2026-11-02）

状态：**候选验收包，等待 S-01 人工评审**。本文件把现有设计中用户 MCP/连接（N-15）和多 agent 协作（N-16）的可验收边界固定成正反例；不生成 proto，不改变运行时接口，也不把外部 MCP 会话或 agent 编排另建成第二套持久状态机。

## 已有设计与本批次的求同存异

- 与 `docs/design/agent-runtime-design.md` 的共同基线：内核只接受登记后的强类型 ToolDriver；外部 MCP 由边缘 MCP Gateway 包装，effect、权限、超时和幂等信息必须补齐；MCP 有状态会话由网关集中管理；SecretStore 只提供凭据引用。N-16 沿用 Goal Node 的 `sub_agent`/`sub_task`，子 agent 默认只读，嵌套深度不超过 2，读可并行、共享产物同一时刻只有一个写者。
- 本轮只冻结验收语义，不决定具体 RPC/数据库表/消息字段。连接会话、agent child workflow 和事件仍分别复用 Gateway、Task/Workflow、Progress/SSE 的现有生命周期。
- “用户 MCP”指用户可登记和绑定的 MCP 连接配置；它不意味着把任意 MCP server 直接暴露给内核。未登记的 server/tool 必须拒绝。

## N-15 用户 MCP/连接

| 边界 | 验收要求 |
| --- | --- |
| 登记与权限 | 连接和工具必须有 tenant/user owner；调用方只能看到已授权连接。MCP Gateway 返回的工具需有 name/version/schema/effect/timeout/idempotency 元数据。 |
| 网络与凭据 | 连接目标只允许显式的 HTTPS/allowlist 域名；SecretStore 只传 `secret_ref`，代理按目标 host 注入密钥，响应、日志、事件和 tracing 不得出现明文。凭据不能因为列出、断开或重连而回传。 |
| 生命周期 | 建立、健康检查、断开、过期、撤销、重连语义可观察且幂等；断开/撤销后旧 lease/token 不能继续调用。网关负责有状态 MCP 会话，Runtime 不复制会话状态。 |
| 兼容性 | 新增字段允许旧客户端忽略；未知状态不能被当作成功。工具登记失败不得留下可调用的半成品。 |

## N-16 多 agent 协作

| 边界 | 验收要求 |
| --- | --- |
| 身份与隔离 | 每个 child agent 有稳定的 task/parent/agent 标识；child 只能读写所属 tenant/session/task 的授权范围，跨 tenant 或跨 task 的结果必须拒绝。 |
| 编排 | `sub_agent` 默认只读、只返回摘要；`sub_task` 复用 Task Loop，嵌套深度不超过 2；超限或未授权派生必须拒绝并留下可诊断原因。 |
| 共享产物 | 读操作可以并行；同一共享产物同一时刻只有一个写者。写冲突要返回可重试/需重规划的明确结果，不静默覆盖。 |
| 取消与失败 | 父 task 取消应传播到 child；child 取消/超时进入终态并可重试或汇报，不得继续产生工具调用或写入。重复 cancel、重复完成和重复汇报是幂等的。 |
| 资源与观测 | 沿用 tenant task/模型预算和现有 active-task 限制；禁止无界派生。Task、Round、child agent 和 tool 调用的 trace 关联可查询，且不含 secret。 |
| 兼容性 | 旧客户端仍可读取 task/result；新增 agent/round 字段未知时可忽略，未知终态不能当作成功。 |

## Failure / Evidence / Minimum fix / 成本 / Decision

### N-15

- **Failure**：未登记的 MCP 工具、越权连接、错误 host 注入凭据或断开后旧 lease 仍可调用，导致工具越权或秘密泄露。
- **Evidence**：现有设计明确采用“强类型内核契约 + 边缘 MCP Gateway”，并要求 SecretStore 引用；`docs/reference/risks-and-open-questions.md` 仍把 MCP Gateway 权限和凭据引用列为待补项。
- **Minimum fix**：在 S-01 批准后为连接登记、调用授权、host allowlist、secret_ref/no-echo、lease revoke、幂等生命周期补充单一契约和正反例；复用现有 SecretStore、ToolDriver、Ingress 审计，不新增会话状态表。
- **Benefit**：把用户 MCP 的权限和密钥边界变成可回归的门禁，阻止半成品工具进入内核。
- **Performance cost**：每次调用增加一次本地授权/allowlist 检查；连接建立增加 Gateway 握手，正常调用不做远端全量扫描。
- **Implementation cost**：需 Gateway/SecretStore/ToolDriver 的跨组件契约评审和 generated client 更新；当前未实现。
- **Operational cost**：需维护 allowlist、连接撤销和 secret rotation 的运维流程；真实 MCP server/CA/网络资源仍需环境验收。
- **New failure surface**：Gateway 不可用、secret ref 过期、allowlist 配置漂移；必须返回明确的 unavailable/denied 原因，不能静默降级到直连。
- **Release slice**：P2-C 契约评审 → 本地 fake Gateway/SecretStore 验收 → 真实 Gateway/HTTPS/轮换验收。
- **Decision**：`ACCEPT CONTRACT PACK / DEFER IMPLEMENTATION`，待 S-01 和 Gateway 接口批准。

### N-16

- **Failure**：child agent 越租户或越 task 读写，共享文件多写者覆盖，取消后仍执行，或派生无限增长导致成本和状态不可控。
- **Evidence**：设计已规定 child 默认只读、嵌套深度 ≤2、共享产物单写者、多 agent 成本显著高于单 agent；Task Loop/Workflow 是现有权威生命周期。
- **Minimum fix**：在现有 Task/Workflow/Node 状态上增加 child identity、parent scope、读写 effect、取消传播、派生深度和 active-task 预算的契约检查；不创建第二套 agent 状态机。
- **Benefit**：保证多 agent 的隔离、可取消和单写者约束，同时保留现有 Task/Workflow 的恢复与审计能力。
- **Performance cost**：派生和写入增加本地 scope/ownership 检查；并行读增加模型和工具调用量，预算由现有 tenant/task 限制控制。
- **Implementation cost**：需要 Task Loop、Worker、进度事件和观测字段的联合评审；真实多 agent E2E 仍待 coding-agent/集群环境。
- **Operational cost**：需监控 child 数、取消延迟、写冲突和 token 成本；无须新增后台协调器。
- **New failure surface**：父子取消竞态、写者租约过期、child 结果重复投递；依靠现有 workflow 幂等键和单写者入口处理。
- **Release slice**：P2-C 契约评审 → 本地 fake child workflow/并发读写/取消测试 → 真实任务队列与 coding-agent E2E。
- **Decision**：`ACCEPT CONTRACT PACK / DEFER IMPLEMENTATION`，先冻结语义，待 S-01 和 Task Loop 单写者实现确认。

## 通过条件与阻塞

本包的本地测试只检查验收清单完整性和与设计基线的一致性，不宣称 N-15/N-16 运行时代码已完成。通过 S-01 前不得修改 proto/schema 或生成代码；缺少真实 MCP Gateway、SecretStore、任务队列和 coding-agent E2E 时，部署验收标记 `blocked_external`。
