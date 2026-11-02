# 用户 Skill / 配置包前置基线（D-27 / N-14）

> 状态：候选前置基线，未修改 proto、schema 或运行时代码。D-27 仍是草案；本文只固定首版边界、验收问题和需要人工决定的语义。
> 依据：[环境配置计划](environment-config.md)、[强档实施计划](strong-tier-plan.md)、[云环境调研](../reference/cloud-environments-codex-claude.md)。

## 首版边界

用户、租户和平台可以登记只读配置包，包在创建环境时按显式选择交付给沙箱。包可以包含 Skill 文本、指令文件、工具说明和有限配置文件；包不能改变租户策略、effect、联网模式、沙箱隔离级别、SecretStore 归属或 Workspace 权限。

首版只保存包的非敏感元数据和内容引用：`id`、`version`、`digest`、`owner`、`visibility`、文件清单、大小和来源。包中的机密只能是 SecretStore 引用；响应、事件、日志、trace、工作流载荷和镜像构建上下文不得出现机密原值。

包交付优先复用现有 ObjectStore / Sandbox `write_file`，不新增配置包运行时状态机。交付失败要阻止使用该包的实例继续执行，并返回有限的错误分类；未选择的包不自动注入 Context Builder。

## 必须冻结的语义

| 项目 | 候选规则 | 必须验证 |
| --- | --- | --- |
| 归属 | `PRIVATE` 首版只允许本人读取、改、绑；组织共享另立 ACL | 跨租户、同租户其他用户读写均拒绝 |
| 路径 | 相对 POSIX 路径；拒绝绝对路径、`..`、空段、符号链接和保留路径 | Windows/Unix 大小写差异、重复路径、嵌套路径 |
| 大小 | 文件数、单文件、总包大小和解压后大小分别受部署上限，租户只能收窄 | 超限在保存和交付两处都拒绝 |
| Digest | 服务端对规范化清单和内容计算 digest；版本发布后内容不可变 | 内容改动必须产生新版本，旧版本仍可复现 |
| 覆盖顺序 | 候选为 `platform → tenant → user → workspace`；同路径冲突按显式优先级拒绝或覆盖，不能静默猜测 | 冲突、删除、回滚、旧客户端只看到非敏感元数据 |
| 权限 | 包内 effect、网络、backend、凭据字段全部被忽略或拒绝；包不能提升权限 | 恶意包声明 `write_irreversible`、open network、Kata/Firecracker 时仍服从租户策略 |
| 脚本 | 是否允许可执行脚本必须单独决定；若允许，只能走 D-24 沙箱脚本边界，不在宿主运行 | 超时、非零退出、取消、日志脱敏和资源限制 |
| MCP | 用户 MCP 不能仅凭包内容自动启用；需 Registry 登记、effect 审批、SecretStore 引用和 D-16 出网 | 未登记、越租户、无凭据、网络未授权均拒绝 |

## 验收清单

1. 创建、读取、更新、删除和绑定只作用于归属用户；名称不是授权标识；包版本发布后不可变。
2. 规范化路径、大小、文件数、digest 和内容类型校验覆盖正反例；包内不能写入 `.git`、宿主路径或其他租户前缀。
3. 交付到 gVisor、Kata、Cloudflare 和未来 Firecracker 时都使用同一文件边界；不支持的后端能力明确报错，不静默降级权限。
4. 同一包重复交付结果一致；内容或引用变更后 digest/version 改变；失败重试不产生第二份业务状态。
5. SecretStore 引用缺失、越权、吊销或过期时交付失败，任何错误、日志和事件都不回显原值。
6. 层级冲突按冻结规则处理；旧客户端忽略未知元数据字段，新客户端不把未知状态当成功。
7. 包被绑定的 Workspace 删除或租户删除时，引用关系可清理；不删除仍被其他合法绑定使用的内容。

## 复杂度决策

| 机制 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 内容 digest 与不可变版本 | 更新后旧会话加载到新内容 | Skill 会进入提示与沙箱文件，复现性需要固定输入 | 规范化文件清单 + 内容 digest；修改创建新版本 | 可复现、可审计 | 保存和交付各一次哈希 | 低 | 需要保留被引用版本 | 引用版本被删除 | D-27 首版 | KEEP |
| 多层覆盖 | 同路径来源不明导致权限或提示变化 | 计划提出 platform/tenant/user/workspace 分层 | 先固定顺序和冲突规则；没有明确规则时拒绝冲突 | 可解释的组合 | 合并时一次扫描 | 中 | 需记录来源元数据 | 错误覆盖顺序 | D-27 评审包 | DEFER 直到规则确认 |
| 包内可执行脚本 | 包执行任意宿主命令 | 用户明确提出 Skills，但未确认脚本语义 | 首版只允许文本；脚本另走 D-24 沙箱流程 | 缩小首版攻击面 | 无额外执行成本 | 低 | 无新的执行入口 | 用户误以为脚本会运行 | D-27 首版 | DEFER |
| 自动启用用户 MCP | 包内容绕过 Registry/effect/出网审批 | 当前 MCP 只有部署登记 | 需独立 Registry/SecretStore/D-16 契约 | 可扩展用户工具 | 每次调用需授权检查 | 高 | 凭据、审计、轮换 | 远端服务与凭据泄漏 | N-15 独立批次 | REJECT for D-27 |
| 预热/缓存包 | 重复解压增加启动时间 | 当前没有缓存验收，D-26 输入顺序仍有冲突 | 首版按实例交付，缓存后置 | 降低启动时间 | 会增加失效与磁盘占用 | 中 | 内容回收与版本失效 | 旧包误复用 | D-26 | DEFER |

## 依赖与阻塞

- EnvironmentConfig、Workspace binding、SecretStore 管理操作和错误模型必须先通过 S-01；D-27 不自行新建第二套身份或凭据模型。
- N-14 用户 Skill 的 Registry 条目、Planner Catalog、Context Builder 分区和审核流程需要单独契约；本文不把包交付等同于 Skill 已被 Planner 使用。
- N-15 用户 MCP 需要真实客户端、effect 审批、SecretStore 和 D-16 网络授权；在这些条件确定前保持 disabled capability。
- 真实包交付、跨后端一致性、镜像/快照集成和性能数字需要 Docker/Kata/Firecracker 或集群资源，代码仓库内只提交可重复的 fixture 与静态检查。

