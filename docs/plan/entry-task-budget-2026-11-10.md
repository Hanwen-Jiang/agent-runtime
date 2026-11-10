# 接入点 Task 预算下调（S-01 / EP-01）

日期：2026-11-10。用户已批准为接入点计划增加最小 Runtime 契约扩展；原计划“Runtime 不改”在本项范围内被这一授权替代。没有修改原设计书导出，也没有更新 `proto/breaking-baseline.binpb`。

## 行为

`SubmitTaskRequest` 增加 `optional int64 budget_usd_micros = 12`，JSON 名为 `budgetUsdMicros`。金额单位为百万分之一美元，JSON 客户端推荐发送十进制字符串，例如 `"1000000"` 表示 1 美元。

| 输入 | 行为 |
| --- | --- |
| 未填 | 使用首次准入时的租户预算；保持旧客户端的重复提交语义 |
| 显式 0 或负值 | `invalid_argument`，不创建 Task |
| 正值高于租户非零美元预算 | `permission_denied` / `budget_exceeds_policy`，不创建 Task |
| 正值不高于租户上限 | 克隆租户 `Budget`，只下调 `usd_micros`，保留 `tool_calls` 与 `wall_clock` |
| 租户美元预算为 0（不设限） | 接受正的请求上限，得到有限预算 |

金额进入已有的 `TaskWorkflowInput.Budget`。Python 的初始 Task State 用它覆盖不可信 `inputs.constraints.budget_usd`；Stage Runner 的美元检查、子 Task 份额以及 Reviewer 使用同一预算。因此不依赖 coding-web 每十秒检查后的取消来建立预算边界。沿用 Runtime 既有的调用间预算检查语义；不宣称消除正在执行中的一次模型调用的计费超额。

## 重复提交与数据迁移

Task Store 的迁移 9 增加 `tasks.submission_hash` 与 `tasks.submission_budget` 两个文本列。它们与 Task 在同一次原子插入中固定；并发同 ID 的提交读取胜出行，不能覆盖其预算。

- 显式使用新预算字段的请求固定完整请求的确定性 protobuf 指纹（认证已决定租户，所以忽略请求中可省略的 `tenant_id`）。改变预算、移除预算、改变指令、结构化约束或其他请求内容，均返回 `already_exists` / `submission_mismatch`。
- 完全相同的请求在信号投递失败后仍可重试；重试使用第一次记录的有效预算，不重新扩大它。JSON 对象键序不影响服务器的确定性 protobuf 编码。
- 未使用新字段的客户端保持原有的重复提交行为，例如用同一 ID 重试时返回最初的 Task，不改已有命令或联网授权。
- 旧记录和未显式设预算的记录不能在重试时补上新字段；返回 `submission_budget_unpinned`，调用方需另用新的提交 ID。
- 老记录的新增列默认为空，迁移不修改原 Task。旧客户端继续可用。部署先升级 Runtime，再启用 coding-web 的显式预算；不能在旧服务拒绝新字段后静默省略上限重试。

没有改动 Temporal 工作流代码、命令顺序或历史载荷结构；传入的是工作流原有的 `Budget` 字段。

## 验证

- `buf generate --path proto/agentruntime/taskapi/v1/task_api.proto`：按仓库固定插件生成 Go / Python / pyi，服务方法未改变，因此 Connect 和 Python gRPC 文件没有差异。
- `buf lint`、`buf build`、`buf breaking --against proto/breaking-baseline.binpb`、`buf format --diff --exit-code` 均通过。
- `go test ./services/go-task-service/internal/taskapi ./services/go-task-service/internal/taskstore`：现有套件通过；在已有用例中补充预算下调、保留工具预算、零/负/超额拒绝、首次信号失败后重试固定预算、拒绝更换预算或命令。Task Store 套件覆盖 SQLite 和真实 Postgres。旧迁移检查改用旧版列集合插入旧记录，再升级到新 schema。
- `go vet` 对 taskapi / taskstore 通过。
- 临时 Connect HTTP/JSON 联调探针（`-race`）通过：使用 coding-agent 同样的 JSON（字符串 `budgetUsdMicros`），确认引擎收到 1 美元预算；旧客户端、零/负/超额、相等上限、不设限租户、JSON 键序与 `1`/`1.0` 重排、严格同请求重试均符合契约。16 个同时相同预算请求只产生一个 Task；16 个混合预算请求只有胜出预算的 8 个成功，另外 8 个冲突，引擎始终只有原 Task/预算。探针已从仓库移除，脚本与结果留在工作区 `.ui-work/entry-task-budget-wire-20261110/`。
- Python 契约检查 `test_proto_conventions.py`：34 项通过。
- Python 手动探针：optional presence、protobuf JSON 字符串编码、Task State 覆盖伪造输入预算、Stage Runner 在已花金额达到预算后拒绝下一步均通过。
- Python `test_replay.py --asyncio-mode=auto`：5 项通过，包含黄金历史及交换命令顺序后的失败检测。
- Go Session 黄金历史回放通过。macOS `/var` 与 `/private/var` 的临时目录别名会使原 mutation 自检在编译临时模块时误报不在 workspace；用真实路径作为 `TMPDIR` 运行同一自检后通过。

未新增长期测试文件，没有更新兼容性基线；本文件与 diff 留给 S-01 审阅。上线与真实接入点端到端验收由 coding-agent 的交付记录汇总。
