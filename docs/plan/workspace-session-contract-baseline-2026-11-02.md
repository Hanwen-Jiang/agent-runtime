# Workspace 与会话投影契约基线（D-06/P1-C，2026-11-02）

> 这是 coding-agent 迁移和 Runtime EnvironmentService 实现前的验收基线，不是 proto/schema 定案。先固定可观察行为，再由 S-01/D-08 人审字段、错误码和兼容性；真实 Cloudflare bridge、R2、Kata 和跨仓库 API 未提供时记录 `blocked_external`。

## 范围和边界

- Workspace 仍由 Runtime/Workspace 所有；会话 UI 只读取经过租户和会话授权的投影，不创建第二份文件状态。
- `tree`、`read`、`log`、`discard`、`copy` 的路径均相对 workspace 根目录解析；拒绝 `..`、绝对路径、NUL、越过根目录的符号链接和隐藏的 host mount。
- `read` 有明确的大小/行数上限，超限返回可处理的错误；`log` 默认是有限窗口，不能把整份运行日志或 secret 带入 SSE。
- `discard` 是可重复的会话工作区清理动作；只清理当前授权 workspace 的未提交内容，不能删除另一个租户或另一个 session 的文件。
- `copy` 显式指定源和目标 workspace；目标存在、跨租户、嵌套路径或超出配额时拒绝。复制不会隐式复制 SecretStore 值或 Git 凭据。
- `/env` 只投影 Runtime EnvironmentService 已确认的状态和时间戳。当前仓库没有 EnvRecord/ledger 查询 API，因此不能推断 `ready`、`reused` 或 `expiry`；在该 API 未落地前，验收结果为 `blocked_external`。
- 旧 SSE 客户端继续接收已有事件名和 `Last-Event-ID` 续传；新增字段必须可忽略，未知状态不能被客户端当作成功。

## 验收用例

用例文件是可执行清单：`tests/contract/fixtures/workspace_session_contract_cases.json`。每个用例必须能映射到一个 API/事件断言和一个正向或拒绝路径。

| 领域 | 最小断言 |
| --- | --- |
| 文件访问 | tree/read 只返回当前 workspace 的允许节点，大小和行数上限稳定 |
| 日志 | log 支持有限窗口/游标，响应和事件不包含 SecretStore 值 |
| 生命周期 | discard 幂等；copy 显式授权、可审计，不能越过 workspace 根或租户边界 |
| 隔离 | tenant、user、session、workspace 四元范围不能交叉读取或修改 |
| 路径安全 | `..`、绝对路径、NUL、符号链接逃逸和 host mount 均拒绝 |
| 环境投影 | 未接通 EnvironmentService 时返回明确 `blocked_external`，不能猜测 ready/reused/expiry |
| 兼容性 | 旧 SSE 事件名/Last-Event-ID 可继续消费；未知字段可忽略，未知状态不当作成功 |

## 设计决策记录

| 项目 | 记录 |
| --- | --- |
| Failure | 越权 workspace 读写、路径穿越、日志/环境投影泄密、旧客户端因新字段或状态崩溃、无 Runtime 查询 API 却显示错误环境状态 |
| Evidence | `docs/plan/cloud-deploy-gates-2026-11-02.md` 将 D-06 标记为 coding-agent 新 API/真实 bridge/R2/Kata 待验收；当前 coding-agent 仅有沙箱配置、workspace/git、runtime health 和 session metadata，没有 EnvRecord/ledger 查询 API |
| Minimum fix | 先冻结上述正反例和错误边界；实现阶段复用现有 Workspace、Task State、SecretStore 与 SSE 游标，只增加必要的查询/复制接口，不新增环境状态机或后台对账表 |
| Benefit | 让跨仓库实现可逐条验收，阻止 UI 把未确认状态当成成功，并在真实资源到位前暴露越权和泄密回归 |
| Performance cost | tree/read/log 需要一次授权和根目录校验；log/read 采用上限避免大载荷；copy 的成本与复制字节数线性相关 |
| Implementation cost | Runtime 与 coding-agent 需要对齐 API/错误码，SSE 需要保留旧事件兼容；需补 R2/bridge/Kata 集成测试 |
| Operational cost | 需要记录请求主体、workspace/session 范围和拒绝原因；真实 bridge、R2、Kata 与 EnvironmentService 资源未提供时无法完成线上 gate |
| New failure surface | API/事件版本漂移、复制超时/部分失败、符号链接和挂载边界处理、旧客户端对未知状态处理不一致 |
| Release slice | S-01/D-08 人审后先本地 contract/unit，再 coding-agent 迁移；随后 kind gate；最后真实 Cloudflare bridge、R2、Kata 和 EnvironmentService 验收 |
| Decision | `ACCEPT` 作为前置验收包；Runtime/coding-agent 实现和 proto/schema 变更 `DEFER` 到契约批准及真实依赖可用，不以静态测试冒充完成 |

## 通过条件和阻塞条件

1. 新增契约测试唯一、可重复、无外部网络依赖，并能在本地读取本基线中的所有关键边界。
2. 生成代码前必须完成人工 S-01/D-08 审查、`buf format`、`buf lint`、`buf breaking` 和兼容性基线。
3. EnvironmentService 查询、真实 Cloudflare bridge/R2/Kata 或 coding-agent API 缺失时，状态只能记录 `blocked_external`，不能写入猜测性的状态字段。
4. 任何实现不得把 workspace 文件、日志、环境变量或 SecretStore 值写入 SSE、错误、trace 或复制产物。
