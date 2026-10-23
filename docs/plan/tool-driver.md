# ToolDriver、平台内工具与 MCP Gateway（中档工作项细化，TMP 计划 T-10）

> 实施计划第 7 节的中档工作项“ToolDriver 契约与平台内工具”（阶段 2）与“MCP Gateway”（贯穿），TMP 计划把两项合成 T-10。
> 依据：设计书 4.9（ToolDriver）、4.10（Sandbox）、3.2.4、附录 D.3、D.4、E.1、K.4、L.10；计划 S-05（effect 规则、审批双保险）；
> `libs/py-core/.../providers/tool_driver.py`（S-02 的接口与开发实现）；T-09 的细化（`docs/plan/policy-engine.md`，工具 effect 的缺口）。
> 状态：已实现（2026-10-23）。验收测试与第 6 节的取舍经人确认；第 4 节最后一条（真模型的人工演示）还没做，要供应商额度。

## 1. 现状

| 部分 | 现在 | T-10 之后 |
| --- | --- | --- |
| 工具的 effect | `resolve_catalog` 的工具部分是桩：一律 `read`（`stub_tool_effects`），不管工具实际做什么 | 取自 ToolDriver 登记的 effect；没登记的工具不出现在结果里，Stage Runner 按最高的 write_irreversible 处理（现有的 fail-safe） |
| tool_loop 的工具调用 | `call_tool` 只有桩（回显参数），从没真的调用过工具 | Node Executor 实现 `call_tool`：经 ToolDriver 调用，规则同 program 节点 |
| tool_loop 给模型的工具定义 | 只有名字，参数 schema 一律 `{"type": "object"}` | 名字、说明与 input_schema 取自 ToolDriver 的登记；没登记的不给 |
| 平台内工具 | 开发组装里一个也没有（ToolDriver 是空的） | 读写工作区文件、搜索、跑命令、取网页 |
| MCP | 没有 | MCP Gateway：按登记文件连 MCP Server（stdio），把登记了的工具补齐 effect 等字段后包装成内核契约 |

## 2. 第一期范围

**做**

1. **工具的 effect 来自 ToolDriver**（关掉 T-09 登记的缺口）：`ToolCatalog`（`agentruntime_worker.tools`）按租户列出工具，同名取版本号
   最大的；`resolve_catalog`（桩 Catalog 与 Registry Catalog 都一样）的工具部分改由它给出。没登记的工具不在结果里：Stage Runner
   把它当作 write_irreversible（展开时超过上限就拒绝，没超过的每次调用前都要审批，调用本身会 not_found）。
2. **`call_tool` 的真实现**（Node Executor）：
   - 工具按名字取当前登记的最大版本；没登记：denied，不可重试。
   - 工具登记的 effect 高于工作流批准的 effect（`ToolCallInput.effect`，展开时从 Catalog 取的）：denied，不调用（防止两次查询之间换了版本）。
   - 写操作的幂等键由代码按 tenant_id、task_id、这次调用的 id（`<Goal id>.<2i>`）与参数生成；write_irreversible 先查审批记录（D16 已有）。
   - 其余同 program 节点（S-05）：要权限的工具用 SecretStore 签发的短期凭据、用完吊销；写操作超时先按幂等键对账；大结果写 ObjectStore，只回摘要与引用。
3. **tool_loop 给模型的工具定义**：`tool_loop_step` 按 Goal Node 的 `tools` 从 `ToolCatalog` 取说明与 input_schema；没登记的不给模型。
4. **平台内工具**（`agentruntime_worker.tools.platform`，登记进开发用的 `LocalToolDriverProvider`，平台作用域，所有租户可见）：

   | 工具 | effect | 做什么 |
   | --- | --- | --- |
   | `repo_read` | read | 读工作区里的文本文件，可按行取一段 |
   | `repo_list` | read | 列工作区里的目录 |
   | `repo_grep` | read | 在工作区里按正则搜索，返回文件、行号与那一行 |
   | `repo_edit` | write_reversible | 把一段文字唯一地替换成另一段，或写整个文件（可新建） |
   | `shell_run` | write_reversible | 在工作区里跑一条命令（argv 列表，不经 shell），带超时；可能改动工作区，所以按写操作 |
   | `web_fetch` | read | HTTP(S) GET 一个网页，只取文本，有大小上限 |

   - 工作区是 `<工作区根>/<tenant_id>/<workspace_id，没有时为 default>`；路径一律相对工作区，绝对路径、`..`、反斜杠、经符号链接出界都拒绝（invalid）。
     工具从调用的作用域拿租户与 workspace（`ToolInvocation` 增加 `workspace_id`，S-02 的接口只加字段），参数改变不了它。
   - `shell_run` 的子进程环境只留 PATH 等必需的变量，Worker 的其他环境变量（含密钥）不带进去；输出截断；超时返回 `timed_out`，不抛异常。
     这是开发实现，与 Sandbox 的本地子进程实现一样仅限开发环境（设计书 4.10）。
   - `web_fetch` 只访问登记的主机（`AR_TOOL_FETCH_HOSTS`，`*` 表示任何公网主机）；解析出的地址是内网、回环、链路本地等一律拒绝（防 SSRF）；
     不跟随重定向（返回状态码与 Location，要去就再调一次，每次都重新检查）。
   - 配置：`AR_TOOL_WORKSPACE_ROOT` 设了才登记工作区类工具（repo_*、shell_run）；`AR_TOOL_FETCH_HOSTS` 设了才登记 `web_fetch`。
5. **MCP Gateway**（`agentruntime_worker.tools.mcp`）：读登记文件（`AR_MCP_TOOLS_FILE`），按 stdio 启动每个 MCP Server（JSON-RPC 2.0，
   initialize → tools/list → tools/call，自己实现，不引入新依赖），把**登记了**的工具包装成内核契约登记进 ToolDriver：
   - 登记文件给出 effect（必填，MCP 没有这个字段）、内核里的名字与版本、超时、可选的租户范围；input_schema 与说明取自 Server；
     Server 声明了 outputSchema 就校验输出。没登记的 MCP 工具不登记、不能调用；登记了但 Server 没有的，跳过并记日志。
   - 调用经 `LocalToolDriverProvider` 的同一套规则（参数校验、写操作要幂等键、幂等记录、超时、输出校验），处理函数转给 MCP 会话。
     `isError` 的结果是 invalid（code tool_error）；会话断了这次是 transient，下次调用重启会话（会话由网关集中管理，不进内核）。
   - 第一期 MCP 工具不能声明权限（凭据怎么交给 MCP Server 另行设计），声明了就是登记文件不合格。
6. **组装**：Worker 进程内只有一个开发用的 ToolDriver（平台内工具与 MCP 工具都登记在里面），Node Executor、`resolve_catalog`、`tool_loop_step`
   共用；MCP 会话在 Worker 启动时建立、退出时关闭。

**不做（登记到后续）**

- MCP Gateway 作为单独部署的边缘服务、HTTP（Streamable HTTP）传输的 MCP Server、MCP 工具的权限与凭据。
- 工具版本固定到 Task（`PinnedVersions` 没有工具；第一期按调用时登记的最大版本，配合第 2 条的 effect 复核）。
- 真正的 Sandbox 后端（WASM、gVisor、microVM）；`shell_run` 的资源限制（CPU、内存）与网络隔离。
- 记忆检索、内部业务 API 这类平台内工具（随 Memory Service 与业务对接再加）。
- 用真模型跑 S-11 那类长任务的演示（TMP 计划 T-10 的“完成”）：要供应商的额度，见第 4 节最后一条。

## 3. 难点与防错

- **effect 不能被低估**：Catalog 的工具 effect 取自登记；没登记按最高处理；调用时再按登记复核一次，高于批准的就拒绝。
- **审批不可绕过**：不可逆工具每次调用前审批（D16）；`call_tool` 没有审批记录就拒绝（双保险）；MCP 工具的 effect 由人登记，登记不全不能用。
- **工作区不出界**：路径规范化后必须仍在本租户、本 workspace 的目录下；符号链接也算。
- **SSRF**：只去登记的主机；按解析出的地址拒绝内网；不跟随重定向。
- **凭据不外泄**：`shell_run` 的子进程拿不到 Worker 的环境变量；工具输出、错误、日志里不出现凭据。
- **幂等**：写工具的幂等键由代码生成、与调用 id 绑定；同一键重放不重复执行；MCP 工具同样走幂等记录。
- **确定性**：工具调用都在 activity 里；工作流只看 Catalog 给的 effect 与调用结果，命令序列不变（不加版本分支）。

## 4. 验收（测试已写，待人确认）

| 验收 | 测试 |
| --- | --- |
| 平台内工具：登记的名字、版本、effect 与 schema；读、列、搜、改、跑命令、取网页的行为；工作区隔离与出界拒绝；`shell_run` 的环境与超时；`web_fetch` 的主机名单、内网拒绝、不跟随重定向、大小上限；没配置就不登记 | `services/py-worker/tests/test_platform_tools.py` |
| MCP Gateway：登记的工具补齐字段后可调用；没登记的不能调用；登记了 Server 没有的跳过；登记文件不合格报错；参数校验、写操作幂等、超时、`isError`、会话崩溃后重启、租户范围、退出时关闭会话 | `tests/test_mcp_gateway.py`（假的 MCP Server：`tests/mcp_fake_server.py`） |
| `ToolCatalog` 与 `resolve_catalog`：工具 effect 取自登记、同名取最大版本、没登记的不给；桩 Catalog 与 Registry Catalog 都如此 | `tests/test_tool_catalog.py` |
| `call_tool`：调用登记的工具；没登记、effect 高于批准都拒绝且不调用；写操作的幂等键；不可逆工具没有审批记录拒绝；权限与凭据；大结果外置；写操作超时先对账 | `tests/test_call_tool.py` |
| tool_loop 用真工具跑完一个 Task：读 → 改 → 跑测试 → 取网页 → 完成；不可逆工具每次调用前问人 | `tests/test_tool_loop_tools.py`（Temporal） |
| tool_loop 给模型的工具定义来自登记 | `tests/test_tool_loop_model.py` |
| 组装：Worker 的 `call_tool` 是 Node Executor 的；按配置登记平台内工具与 MCP 工具；Catalog 用同一个 ToolDriver | `tests/test_worker_entry.py`、`tests/test_config.py` |
| ToolDriver 的处理函数拿到绑定的 workspace | `libs/py-core` 的 ToolDriver 一致性测试 |
| 人工演示（不进自动测试）：设 `AR_TOOL_LOOP=model` 与 Model Gateway，用至少三个真工具跑一个 S-11 那类长任务（例如 `ctx_coding_ci_rounding`）；要供应商额度 | 结果记进 TMP 计划 |

## 5. 依赖

S-02（ToolDriver、SecretStore、ObjectStore 的接口与开发实现）、S-05（Node Executor 的工具规则）、D16（tool_loop 的不可逆工具审批）、
T-08（Registry Catalog）、T-09（Policy Engine）。

## 6. 写测试时定下的取舍（待人确认）

1. tool_loop 里的工具按名字调用、取调用时登记的最大版本，不固定到 Task；调用时按登记复核 effect，高于批准的就拒绝。
2. 工作区按租户与 workspace 划分，同一 workspace 的不同 Task 共用（没有 workspace 的都用 default）。
3. `shell_run` 按 write_reversible（它能改工作区）；没有资源限制与网络隔离，仅限开发环境。
4. `web_fetch` 默认不登记；设了主机名单才登记，内网地址一律拒绝，不跟随重定向。
5. MCP Gateway 第一期在 Worker 进程内、只支持 stdio、不支持权限；MCP 工具的 effect 必须人工登记。
6. Registry Catalog 的构造参数由 `tool_effects`（同步函数）改为 `tools`（`ToolCatalog`，按租户异步查询）；T-08 的 Worker 测试随之改用新参数。
