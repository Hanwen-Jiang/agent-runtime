# CLAUDE.md

本仓库实现 Agent Runtime（暂名）：一个多租户的 Agent 运行核心。每个 Task 都按固定的 Task Loop（Planner → Stage → Reviewer）执行，持久化执行用 Temporal；Go 写 Control Plane 与 Model Gateway，Python 写 Worker、Memory Service 与 Learning Pipeline。

## 先读什么

| 文件 | 作用 |
| --- | --- |
| `docs/design/agent-runtime-design.md` | 设计书，唯一权威。只读与当前任务相关的章节和附录 |
| `docs/plan/strong-tier-plan.md` | 实施计划：工作项编号（S-xx、R-xx）、顺序、验收、评审方式 |
| `docs/reference/runtime-quickref.md` | 速查：模块依赖、接口的开发实现、旧叫法对照 |
| `docs/reference/` 其余文件 | 调研依据，遇到边界情况时查 |

优先级：设计书 > 实施计划 > 参考文档。发现三者之间有冲突时，以设计书为准，并在回复里指出冲突。

## 术语

- 一律使用设计书附录 A 的名称：代码、注释、文档、提交信息都一样。
- 不使用调研文档里的旧叫法，例如 Orchestrator、Synthesizer、超步、精确节点、抽象任务、flow 配方。对照表见 `docs/reference/runtime-quickref.md` 第 7 节。
- 枚举取值按附录 A：
  - executor、effect、role、mode、quality、reasoning
  - Verdict：done、continue、repair、replan、rollback、reroute、escalate、abort
  - Triage：fast、light、full、replan

## 怎么接一个任务

1. 先确认任务对应计划里的哪个工作项（S-xx 或 R-xx）。不属于任何工作项时，先说明它应该挂在哪一项下面。
2. 读该工作项全文，以及它列出的设计书章节，重点是“难点与防错”和“验收”。
3. 先写验收测试，覆盖“验收”的每一条，以及“难点与防错”中能测的每一条。写完停下，等人确认。
4. 再写实现，直到测试全部通过。
5. 结束时列出没有覆盖的地方，以及与设计书不一致的地方。

被要求做评审时，用计划第 6.2 节的评审模板，只看 diff、设计书相关章节和测试结果。

## 硬性规则

**契约**

- 不修改 `/proto` 与 `/schemas`。需要改时停下来说明原因，由 S-01 的流程处理：修改后过 `buf breaking`，再人审。
- protobuf 只放骨架。Node 的 inputs、result 与模板参数是 JSON Schema 载荷，不写成 proto message（设计书 4.1）。

**依赖**

- 核心代码只依赖 Provider 接口，不直接引用任何具体产品的 SDK（Temporal 工作流代码除外）。
- 不引入设计书 4.15 之外的新依赖；需要时先说明理由。

**工作流代码必须确定**（Temporal 会重放事件历史）

- Python：不读系统时间、不用 random、不做 I/O、不调用模型。时间用 `workflow.now()`，随机数与 UUID 用 `workflow.random()`、`workflow.uuid4()`。
- Go：不用 `time.Now`、原生 goroutine、channel 与 select，改用 `workflow.Now`、`workflow.Go`、`workflow.Channel`、`workflow.Selector`。
- 不依赖 set 或 map 的遍历顺序；并发结果按 Node id 排序后合并。
- 改变命令序列的修改，必须加版本分支（`workflow.patched` 或 `workflow.GetVersion`），并用重放测试证明旧历史仍能回放。
- Python 的版本分支 id 一律定义在 `agentruntime_worker/workflows/patches.py`，并追加到 `AT_START` 末尾：新的运行在第一个工作流任务里一次问完。不要让版本分支在 local activity（Hook）之后才第一次问，回放时会走错路径（原因见该模块）。
- 模型调用、工具调用、外部 I/O 一律放进 activity。

**副作用**（附录 D.4）

- effect 为 write_reversible 或 write_irreversible 的 Node 必须带幂等键。
- 幂等键由 tenant_id、task_id、node_id 与输入哈希生成，不含 attempt 编号。
- write_irreversible 不自动重试，执行前必须有通过的 gate 记录。
- 写操作超时且结果未知时，先按幂等键对账，不直接重试。

**租户隔离**

- 每次 Provider 调用都带租户上下文。
- 租户由服务端根据认证身份确定；请求里的 tenant_id 只用来校验一致。
- 检索与查询的租户过滤条件由服务端拼接，永远不由模型生成。
- 缺少租户条件的查询直接拒绝。

**其他**

- 凭据与密钥不得出现在日志、trace 和错误信息里；节点代码只拿 SecretStore 签发的短期凭据。
- Node 的 rationale 只写入 trace，不传给下游。
- Reviewer 的输入只有 Stage 结果（含本轮执行的 Stage）、Task State 与验收条件，不传入 Planner 的规划过程。
- 核心链路 Task Loop → Stage Runner → Node Executor 同一时间只由一个 agent 修改。

## 仓库结构

按设计书 4.15 建立（S-01）。`/tests/contract`、`/tests/e2e` 与 `/tests/deploy` 不在 4.15 中，经人确认后加入。

```text
/proto                      # protobuf，Buf 生成代码并做兼容性检查
/proto/gen/go               # buf 生成的 Go 代码（独立 Go 模块），提交进仓库，不手改
/proto/gen/python           # buf 生成的 Python 代码（uv 包 agentruntime-proto），提交进仓库，不手改
/schemas                    # Node 与模板的 JSON Schema；schemas/examples 是正例与反例
/services/go-task-service
/services/go-model-gateway
/services/py-worker         # Worker；Context Builder 是其中的库 agentruntime_worker.context（分区、Compaction Ladder、S-11 的增量摘要与 Compaction Probe）
/services/py-memory         # Memory Service（S-12b、S-13）：作用域 ACL、写入与检索流水线、晋升、级联删除与回滚；gRPC 服务 python -m agentruntime_memory
/services/py-learning       # Learning Pipeline；评测集与评测 harness（S-08）：agentruntime_learning.evals，评测集在 evalsets/task-loop（S-11 的长任务样本在其中的 context_samples）；最小闭环（T-16）：agentruntime_learning.pipeline
/deploy                     # Helm 与环境配置；Chart 在 deploy/helm/agent-runtime（T-15），Collector 配置在其中的 files/otel-collector.yaml
/libs/py-core               # Python 共享库 agentruntime-core：Provider 接口、错误模型、租户绑定、一致性测试（S-02）；Postgres、Qdrant 适配器与密钥环（S-12b）；Milvus 适配器（T-18）
/libs/go-core               # Go 共享库：同上（S-02）
/tests/contract             # 跨语言、跨服务的契约验收测试（S-01；S-02 的接口一致性比对也在这里）
/tests/e2e                  # 端到端验收测试：Temporal 开发服务 + task-service + Python Worker（S-03）
/tests/e2e/faults           # 故障注入与恢复套件（S-07）；故障 Worker 与外部 ledger 在 tests/e2e/e2e_support
/tests/deploy               # 部署验收（T-15）：helm lint、helm template 的结构检查与 Collector 配置
/.github/workflows          # CI：ci.yml 在 PR 与 main 上按步骤分 job 调用 scripts/check.py（T-14）；nightly.yml 每晚跑 tests/e2e（含故障注入套件与越权用例）、全部 Go 测试（含 Postgres 行级安全），以及 py-core 与 py-memory 的测试（Postgres + Qdrant）
/products/coding            # 在线多租户 coding agent 的 MVP（2026-10-24 经人确认加入，计划见 docs/plan/coding-agent-mvp.md）：coding-web 服务（Go）与网页，经 Task API 调 Runtime
/scripts                    # check.py：本地与 CI 共用的检查脚本，只用标准库（T-14）
/docs                       # 设计书、实施计划、参考文档；docs/note 是各模块的学习笔记（末尾附面试速查）
```

Go 模块路径前缀暂定 `github.com/agentruntime/agent-runtime`，schema 的 `$id` 前缀暂定 `https://agentruntime.example/schemas/`，都是占位，定下真实地址后统一替换。

`/libs` 经人确认后建立（S-02）。Provider 客户端一律按请求绑定租户：工厂 `bind(tenant)` / `Bind(tenant)` 得到客户端，之后的调用不再传租户，全仓统一。

## 常用命令

工具链：Go 1.27、uv 0.12（Python 3.13）、buf 1.73、Git 2.56、Temporal CLI 1.9.1、PostgreSQL 17、Qdrant 1.19.1、Docker（Milvus 3.0.2 的测试用；本机是 Docker Desktop，WSL 2 后端）、Helm 3.19（部署测试；可选 otelcol-contrib 校验 Collector 配置）。本机装在 `%USERPROFILE%\.local` 下（`bin`、`go`、`mingit`、`python`、`pgsql`、`qdrant`），不要装到 AppData：Claude 桌面应用里写入 AppData 的文件会被重定向到应用私有目录，用户自己的终端看不到。DurableEngine 的一致性测试用 PATH 上的 `temporal` 启动开发服务，不让 SDK 自行下载。Postgres 业务库的测试（S-12a：`libs/go-core/adapters/postgres` 与 task-service 的 `internal/taskstore`）用 PATH 上（或 `AR_POSTGRES_BIN` 指定目录里）的 `initdb`、`pg_ctl`、`psql` 自己拉起临时实例；本机要把 `%USERPROFILE%\.local\pgsql\bin` 放进 PATH。Python 侧的 Postgres 适配器（S-12b）同样自己拉起临时实例；Qdrant 适配器与 Memory Service 的测试用 PATH 上（或 `AR_QDRANT_BIN` 指定位置）的 `qdrant` 拉起临时实例，本机要把 `%USERPROFILE%\.local\qdrant` 放进 PATH。Milvus 适配器（T-18）的测试用 PATH 上（或 `AR_DOCKER_BIN` 指定位置）的 `docker` 拉起临时的单机版容器（内嵌 etcd、本地存储，数据不挂载宿主目录，测完删除）；镜像默认 `milvusdb/milvus:v3.0.2`，可用 `AR_MILVUS_IMAGE` 覆盖。Docker Desktop 要先启动；它的 `resources\bin` 要在 PATH 上（`docker-credential-desktop` 也在那里）。缺这些工具时测试失败，设 `AR_ALLOW_MISSING_TOOLS=1` 改为跳过。

仓库在网络共享盘（`Z:` 即 `\\172.31.0.1\VMShare`）上时，Temporal Python SDK 的原生模块不能从共享盘加载，虚拟环境必须放在本地盘：

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE\.local\venvs\agent-runtime"
```

| 做什么 | 命令 |
| --- | --- |
| 安装 Python 依赖 | `uv sync --all-packages --locked` |
| 一次跑完检查（T-14） | `uv run --no-sync python scripts/check.py`：pr 档是除故障注入套件外的全部（proto、contract、go、py-core、py-memory、py-learning、py-worker、e2e），`--tier full` 再加 faults；`--only`、`--skip` 挑步骤，`--list` 看计划，`--fail-fast` 遇错即停，`--junit-dir` 写报告。缺工具的步骤失败并写明缺什么（`AR_ALLOW_MISSING_TOOLS=1` 时跳过）；仓库在网络盘上又没设 `UV_PROJECT_ENVIRONMENT` 时拒绝跑 Python 步骤。`--clone <目录>` 把当前提交克隆到本地目录再跑，用克隆自己的 `.venv`，不动共用的虚拟环境。CI（`.github/workflows/ci.yml`）在 PR 与 main 上按步骤分 job 调用同一个脚本 |
| proto lint 与格式 | `buf lint`；`buf format --diff --exit-code` |
| proto 兼容性检查 | `buf breaking --against proto/breaking-baseline.binpb`（基线是人审通过的契约；仓库在共享盘上时 buf 无法克隆 `.git`） |
| 生成代码 | `buf generate`（远程插件，需要联网），生成结果一并提交 |
| 契约验收测试（S-01） | `uv run --package agentruntime-contract-tests pytest tests/contract` |
| Python 共享库与一致性测试（S-02） | 在 `libs/py-core` 下：`uv run --package agentruntime-core --all-extras pytest tests` |
| Go 构建与测试（含 `libs/go-core` 的一致性测试与越权用例） | 在 `go.work` 列出的每个模块目录里运行 `go build ./...`、`go vet ./...`、`go test ./...`（要 PATH 上有 `temporal` 与 Postgres 二进制，见上） |
| Memory Service 测试（S-12b、S-13） | 在 `services/py-memory` 下：`uv run --package agentruntime-memory --all-extras pytest tests`（开发后端与 Postgres + Qdrant 后端各跑一遍；只跑开发后端加 `-m "not backends"`）。独立运行：`python -m agentruntime_memory`（环境变量见该模块：`AR_DEV_SIGNING_KEY` 必填，`AR_RELATIONAL_DSN`、`AR_QDRANT_URL` 换成生产后端） |
| Learning 测试（S-08） | 在 `services/py-learning` 下：`uv run --package agentruntime-learning --all-extras pytest tests` |
| 评测（S-08、S-09、S-10） | `uv run --package agentruntime-learning python -m agentruntime_learning.evals run --subject baseline`（被测对象：`baseline`、`oracle` 或 `<模块>:<工厂函数>`；默认开发集，`--split holdout` 只给汇总；`--require-confirmed` 要求标注全部经人确认）。报告写在 `services/py-learning/.eval-reports`，自动与同一被测对象的上一份报告比较。真模型有随机性：`--repeat 3` 跑三次，另写一份汇总（中位数与范围，单次报告都保留）；`--gate s09` 或 `--gate s10` 按中位数判 S-09、S-10 的门槛并写出差距。`context` 子命令也有 `--repeat`。提示只在开发集上调；评测集的标注改动要经人确认。评测 S-09 的 Planner：`--subject agentruntime_learning.evals.worker_subject:planner_subject`；评测 S-10（Planner 加 Reviewer）：`--subject agentruntime_learning.evals.worker_subject:worker_subject`；两者都要先设 `AR_EVAL_GATEWAY_FACTORY=<模块>:<工厂函数>`（工厂函数返回 Model Gateway）。S-11 的压缩评测是 `context` 子命令：`… python -m agentruntime_learning.evals context --subject lexical`（离线基线，只检查链路）；真模型用 `--subject agentruntime_learning.evals.worker_subject:context_subject`（同样要 `AR_EVAL_GATEWAY_FACTORY`）。长任务样本在 `evalsets/task-loop/context_samples`，标注要经人确认。真模型评测经 Model Gateway：`AR_EVAL_GATEWAY_FACTORY=agentruntime_worker.executor.grpc_gateway:from_env`（另设 `AR_MODEL_GATEWAY_ADDRESS`，网关的启动见下面 Model Gateway 一行或 `scripts/dev_stack.py`）。不经网关的离线对照：`AR_EVAL_GATEWAY_FACTORY=agentruntime_learning.evals.provider_gateway:make`，直连一个 OpenAI 兼容的供应商（只做评测用得到的部分，不传缓存断点），地址与密钥读 `%USERPROFILE%\.agentruntime\model-provider.env`（`AR_MODEL_PROVIDER_BASE_URL`、`AR_MODEL_PROVIDER_API_KEY`，可选 `AR_MODEL_PROVIDER_MODEL_STRONG`、`_MID`、`_LIGHT` 与对应的单价 `AR_MODEL_PROVIDER_PRICE_STRONG` 等（“输入,输出[,缓存读]”，美元每百万 token；默认模型有内置的标价）；同名环境变量优先）。这个文件放在仓库外面，不提交 |
| Worker 测试（S-03） | 在 `services/py-worker` 下：`uv run --package agentruntime-worker --all-extras pytest tests` |
| Model Gateway（T-05、T-06） | 在 `services/go-model-gateway` 下：`go run ./cmd/model-gateway dev-secret model-provider/api-key`（密钥从标准输入读，写进 `AR_SECRETS_FILE` 的平台作用域）；`go run ./cmd/model-gateway serve`（`AR_MODEL_PROFILES_FILE` 指向 `deploy/dev/model-profiles.json`，另要 `AR_MODEL_PROVIDER_BASE_URL`、`AR_SECRETS_FILE`；默认监听 127.0.0.1:8091，同一端口是 gRPC 与 `/healthz`，计量记录写标准输出；`AR_MODEL_PROVIDER=mock` 离线）。Worker 设 `AR_MODEL_GATEWAY_ADDRESS=127.0.0.1:8091` 后 Node Executor、Planner、Reviewer、tool_loop 都经它；评测用 `AR_EVAL_GATEWAY_FACTORY=agentruntime_worker.executor.grpc_gateway:from_env`。适配层的真实端点测试：设 `AR_LIVE_MODEL_PROVIDER=1`（另要 `AR_MODEL_PROVIDER_BASE_URL`、`AR_MODEL_PROVIDER_API_KEY`），`libs/go-core` 下 `go test ./adapters/openaicompat -run Live` |
| Registry（T-08） | task-service 设 `AR_REGISTRY_ADDR=127.0.0.1:8092`（单独的内部端口，不能与 `AR_HTTP_ADDR` 相同，也不能再配 `AR_PINNED_VERSIONS_FILE`）就启用 Registry：新 Task 的固定版本由它给出，`AR_SCHEMAS_DIR`（默认 `schemas`，相对工作目录）是它校验 document 用的 /schemas。发布令牌放在 `AR_SECRETS_FILE` 的平台作用域，名为 `registry/publish-token`；`Publish`、`Promote` 带 `authorization: Bearer <令牌>`，读方法不要令牌。开发种子：`task-service registry-seed <文件>`（格式见 `internal/registry/seed.go`；只要 `AR_DATA_DIR` 或 `AR_RELATIONAL_DSN` 与 `AR_SCHEMAS_DIR`，幂等）。Worker 设 `AR_REGISTRY_ADDRESS` 后 `resolve_catalog` 与真正的 Planner 的 Node Catalog、Recipe 改读 Registry。细化见 `docs/plan/registry.md` |
| Policy Engine（T-09） | Worker 的 Hook 一律由 `agentruntime_worker.policy` 执行（没有开关），审计与 trace 的记录写标准错误。租户策略与 Recipe 声明的 Hook 只在 Worker 设了 `AR_REGISTRY_ADDRESS` 时才读得到；这时 Stage Runner 还会在执行前按 Registry 登记的模板核对 Atomic Node 的 executor 与 effect。细化见 `docs/plan/policy-engine.md` |
| 工具（T-10） | Worker 进程内一个开发用 ToolDriver，Node Executor 的 `call_tool`、`resolve_catalog` 的工具 effect 与 tool_loop 给模型的工具定义都取自它的登记（同名取最大版本；没登记的工具按 write_irreversible 处理，调用被拒）。`context_read` 总是登记：按 obj:// 路径取回 Context Builder 外置的内容与 Node 的外置结果，真正的 Planner 给 tool_loop 的 Goal Node 加上它。`AR_TOOL_WORKSPACE_ROOT` 设了才登记 `repo_read`、`repo_list`、`repo_grep`、`repo_edit`、`shell_run`（工作区是 `<根>/<租户>/<workspace，没有时为 default>`；`shell_run` 不隔离，仅限开发环境）；`AR_TOOL_FETCH_HOSTS`（逗号分隔，`*` 或 `*.后缀`）设了才登记 `web_fetch`（内网地址拒绝，不跟随重定向）；`AR_MCP_TOOLS_FILE` 指向 MCP 登记文件（格式见 `agentruntime_worker.tools.mcp`；只支持 stdio，effect 必须人工登记，不能声明权限），Worker 启动时建立会话、退出时关闭。细化见 `docs/plan/tool-driver.md` |
| Telemetry（T-15） | 设了 `OTEL_EXPORTER_OTLP_ENDPOINT`（Collector 的 OTLP/HTTP 基地址，形如 `http://127.0.0.1:4318`）时 Worker 与 Model Gateway 经 OpenTelemetry 实现发给 Collector（`agentruntime_core.adapters.otel`、`libs/go-core/adapters/oteltelemetry`；可选 `OTEL_SERVICE_VERSION`、`OTEL_DEPLOYMENT_ENVIRONMENT`）；没设时 Worker 写标准错误，网关不记 span。span 按附录 J.1：`chat`、`execute_tool {工具}`、收尾补记的 `invoke_workflow` 与 `runtime.round`。Collector 配置在 `deploy/helm/agent-runtime/files/otel-collector.yaml`。细化见 `docs/plan/deploy-telemetry.md` |
| 部署测试（T-15） | 在 `tests/deploy` 下：`uv run --package agentruntime-deploy-tests pytest`（要 PATH 上的 `helm`；有 `otelcol-contrib` 时另校验 Collector 配置）。直接看：`helm lint --strict deploy/helm/agent-runtime`、`helm template ar deploy/helm/agent-runtime` |
| Learning Pipeline（T-16） | `uv run --package agentruntime-learning python -m agentruntime_learning.pipeline run --tenant <租户> --family <任务族> --traces <文件>… [--format otlp_json\|console] [--dry-run]`：读 trace（Collector 的 OTLP JSON 或控制台 Telemetry 的 JSON 行），归纳候选 Recipe，按附录 J.3 的非劣效门槛评测，过了才经 Registry 发布（`AR_REGISTRY_ADDRESS`，发布令牌取 `AR_SECRETS_FILE` 平台作用域的 `registry/publish-token`）；失败归因可选经 `AR_MODEL_GATEWAY_ADDRESS`，`--budget-usd` 单独记预算。报告写 `--report-dir`。细化见 `docs/plan/learning-pipeline.md` |
| 端到端测试（S-03） | 在 `tests/e2e` 下：`uv run --package agentruntime-e2e-tests pytest`（自己拉起 Temporal 开发服务、task-service 与 Worker；含故障注入套件，约 15 分钟）；跳过故障注入套件加 `-m "not faults"` |
| 本地拉起整套（T-13） | `uv run --no-sync python scripts/dev_stack.py up [--provider mock] [--decisions stub] [--tenant-effect t13=write_reversible]`：编译 task-service 与 model-gateway，种 Registry（`deploy/dev/registry-seed.json`），拉起 Temporal、task-service（Registry 打开）、Model Gateway、Worker（默认真 Planner、Reviewer、tool_loop，经网关）；`status`、`token --tenant --user`、`kill worker`、`start worker`、`env`、`down`。运行目录默认 `~/.agentruntime/dev-stack`；真模型时地址与密钥只读 `~/.agentruntime/model-provider.env`，密钥经 `dev-secret` 的标准输入进开发 SecretStore。离线验收是 `tests/e2e/test_dev_stack.py`。细化见 `docs/plan/live-e2e.md` |
| Coding agent（MVP，`products/coding`） | `uv run --no-sync python scripts/dev_stack.py up --coding --sandbox docker`（再加 `--provider mock --decisions stub` 离线），打开输出里的 Coding 地址，用开发登录（租户 + 用户名）进入。coding-web 的环境变量见 `products/coding/cmd/coding-web`；它与 Worker 共用工作区，新租户首次登录时把 effect 上限写成 write_reversible。`--sandbox docker` 时 Worker 设 `AR_TOOL_SHELL_SANDBOX=docker`：shell_run 在一次性容器里跑（镜像 `AR_TOOL_SHELL_IMAGE`，默认 python:3.13-slim；不联网、限资源），工作区根要在 Docker 能挂载的本地盘上。Go 测试：在 `products/coding` 下 `go test ./...`。计划见 `docs/plan/coding-agent-mvp.md` |
| 真模型端到端（T-13） | 在 `tests/e2e` 下：`$env:AR_LIVE_MODEL_PROVIDER = "1"; uv run --package agentruntime-e2e-tests pytest live`（花钱、不进 CI；每条用例按计量不超过 3 美元，2026-10-24 两条共约 7 分钟、0.3 美元）。密钥读 `%USERPROFILE%\.agentruntime\model-provider.env`，经 `scripts/dev_stack.py` 写进本次运行的开发 SecretStore。CI 修复样本在 `live/workspaces/ci_rounding`；端到端日志写 `tests/e2e/.live-reports/`（不提交） |
| 故障注入与恢复套件（S-07） | 在 `tests/e2e` 下：`uv run --package agentruntime-e2e-tests pytest faults`；夜间任务见 `.github/workflows/nightly.yml`。故障由故障计划注入（`e2e_support/faults.py`），外部副作用记在外部 ledger（`e2e_support/ledger.py`） |
| 更新 golden 事件历史 | 设 `AR_UPDATE_GOLDEN_HISTORIES=1` 再跑测试：端到端测试写 Session 工作流与 S-03 的 Task 工作流历史（`services/go-task-service/internal/session/testdata/histories`、`services/py-worker/tests/histories`），py-worker 测试写 Stage Runner、Node Executor 与 Verdict 去向各条路径的历史（`services/py-worker/tests/histories/stage_*.json`、`exec_*.json`、`review_*.json` 等）。只补缺的，不覆盖已有的 golden（它们证明旧版本的历史仍能回放）；要重新生成某一份先删掉它。结果人审后提交 |
| 重放事件历史 | Go：在 `services/go-task-service` 下 `go run ./cmd/task-service replay --dir <目录>`；Python：`uv run --package agentruntime-worker python -m agentruntime_worker.replay --dir <目录>` |
| 本地 Temporal 开发服务 | `temporal server start-dev` |
| 本地跑整套 | `task-service serve`（环境变量见 `internal/config`，至少要 `AR_TEMPORAL_ADDRESS`、`AR_DEV_SIGNING_KEY`；租户的 effect 上限默认 read，要跑写节点用 `AR_DEV_TENANT_POLICIES_FILE` 指向租户策略文件）；`python -m agentruntime_worker --queues task,decision,model,program`；令牌用 `task-service dev-token --tenant t1 --user u1`（取消别人提交的 Task 或回复它的问题、审批，要加 `--scope task.approve`；提交者本人不用）。两个进程的 `AR_OBJECT_STORE_DIR` 与 `AR_SECRETS_FILE` 要指向同一处：超过阈值（`AR_CLAIM_CHECK_THRESHOLD_BYTES`，默认 64 KiB）的 Temporal 载荷由 Claim-Check 卸载到 ObjectStore（S-06），两边要互相取回；回放工具（`replay`）也读这两个变量。`AR_PLANNER=model` 换成真正的 Planner（S-09；默认 `stub` 是桩 Planner，端到端测试用它；没设 `AR_MODEL_GATEWAY_ADDRESS` 时真正的 Planner 拿到的是桩模型，规划会回退到保守规划）。`AR_REVIEWER=model` 换成真正的 Reviewer 与收尾写回（S-10；默认 `stub`；没设 `AR_MODEL_GATEWAY_ADDRESS` 时只按规则判定；经验候选只记在进程内）。`AR_TOOL_LOOP=model` 换成经 Model Gateway 的 tool_loop，上下文由 Context Builder 组装（默认 `stub`）；阈值按 `AR_CONTEXT_EFFECTIVE_TOKENS`（模型实测的有效长度，默认 32000）算，Model Profile 接好后改从 Registry 取。业务库默认是 `AR_DATA_DIR` 下的 SQLite；设 `AR_RELATIONAL_DSN` 改用 Postgres（行级安全，S-12a），连接的角色不能是超级用户或带 BYPASSRLS |

契约变更走 S-01 流程：改 proto 或 schema → `buf generate` → 跑契约测试 → 人审通过后用 `buf build -o proto/breaking-baseline.binpb` 更新兼容性基线。

依赖解析受根 `pyproject.toml` 的 `exclude-newer` 约束，只取发布满两周的版本；升级时有意识地往后挪这个日期。

重放是硬门槛：`go test` 与 `pytest` 都会回放 golden 事件历史，并验证调换 `replay-mutation:A/B` 两行后重放必须失败。改变命令序列的修改要加版本分支；golden 历史只在有意识地更新时重新生成。

代码托管在 GitHub（私有仓库，`gh` 已登录）；每完成一个小阶段就提交并推送。

## 文档

- `docs/design/agent-runtime-design.md` 是从 Claude Docs 文档导出的副本，不直接修改。需要修改设计时，在回复里写出建议的改动，由人更新原文档后重新导出。
- 计划与参考文档可以修改，但改动要在回复里说明。
