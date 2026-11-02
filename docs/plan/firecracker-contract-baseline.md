# Firecracker 前置验收基线（D-19～D-22、D-26）

> 状态：候选验收基线，未实现 Firecracker、fc-manager、vsock 或快照，也未修改 proto/schema。
> 真实 `/dev/kvm`、Firecracker 版本、客户机内核和宿主容量必须在独立环境 gate 中验收；本包只冻结可审查的安全边界和失败语义。

## 首版边界

- D-19 只登记 `enabled`、`default` 和 `capabilities`；禁用后端不接新环境，但已有环境仍可执行、销毁和被对账看见。
- Firecracker 不自动降级到 gVisor。只有租户策略显式允许更低隔离级别时才进入既有回退规则。
- D-20 的 fc-manager 是宿主高权限组件；Worker 只能访问受限 API，用户输入不能进入 shell 命令、jailer 参数或管理目标。
- D-21 只转换平台构建且已批准的 OCI 镜像；租户自定义环境在 Firecracker 上先返回 `unsupported` 并走既有 universal 回退说明。
- D-22 的客户机没有网卡；代理、包镜像和预览只能走固定的 vsock 转发目标，不能把转发器变成任意 TCP 跳板。
- D-26 快照只包含基础环境和 setup 结果，不包含租户工作区、直传变量、网络密钥或其他机密；恢复后必须重置随机数、机器标识和 vsock 连接。

## 验收范围

正例与反例见 `tests/contract/fixtures/firecracker_contract_cases.json`。这些案例是实现前的审查清单，不代表 Firecracker 已经通过真实运行验收。

必须单独记录的真实 gate：`/dev/kvm`、jailer/seccomp/cgroup、客户机 PID 1、无网卡、真实 vsock、冷启动 P50/P95、容量耗尽、fc-manager 重启、VM 丢失、快照恢复和宿主清理。

## 复杂度决策

| 机制 | Failure | Evidence | Minimum fix | Benefit | 成本 | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| 后端 enabled/capabilities | 禁用或能力不足的后端仍被新任务选中 | D-19 要求滚动排空和能力声明 | 启动时校验登记，创建时拒绝 disabled，保留已有环境 | 避免错误路由和静默降级 | 一次配置解析与创建检查 | KEEP |
| fc-manager 独立宿主服务 | 把 KVM/jailer 权限放进 Worker Pod 扩大权限面 | Firecracker 需要 `/dev/kvm` 与宿主级资源 | 独立服务、最小 API、Worker 单向访问 | 限制高权限边界 | 新服务和部署 gate | KEEP |
| 快照缓存 | setup 重复执行增加启动时间 | D-26 需要快照，但 setup/工作区导入顺序仍有冲突 | 首版冷启动；确认输入顺序后再做包含租户/工作区隔离的键 | 后续降低启动延迟 | 宿主缓存、失效和恢复测试 | DEFER |
| 第二套环境状态机 | 现有 SandboxBroker/ledger 已有生命周期 | 没有证据需要独立 Firecracker 状态源 | 复用现有环境状态与对账 | 避免状态分裂 | 无新增表/轮询 | REJECT |

