# 环境配置与多仓库工作区（D-23、D-24、D-25、D-27）

> 2026-11-01 起草。**状态：草案，等人确认。** 确认后按仓库的流程走：先写验收测试、写完停下等人确认、再实现。**本稿不改 `/proto` 与 `/schemas`**：要改的地方在第 5 节列成清单，走 S-01，人审通过后再动。
> Firecracker 后端（D-19～D-22、D-26）在 [firecracker-backend.md](firecracker-backend.md)。依据：[沙箱平台](sandbox-platform.md)、[Workspace 层](workspace-layer.md)、[云上部署计划](cloud-deploy.md)，以及 2026-11-01 对当前代码的核对（第 3 节）。
> 术语：“环境配置”“联网模式”“启动脚本”“布局”是**暂名**。CLAUDE.md 要求一律用设计书附录 A 的名称，这几个词还不在里面，需要人更新设计书后统一（第 7 节）。

## 1. 决定（2026-11-01，用户确认）

用户的要求：用户可以对云上的环境做自己的创建配置——环境变量（`.env`）、联网权限（受控列表、禁止、全放行）、启动时自动执行的 bash 脚本；同一个工作区可以是空项目、连一个仓库，还应该可以授权连接多个仓库。逐条定下的取舍：

1. **全放行（`open`）= 所有公网域名**。内网、云元数据、IP 字面量永远拒绝（出网网关的 SSRF 防线不变），25 端口永远拒绝，审计与流量配额照记。**默认对所有租户关闭**，租户策略显式允许才能用。
2. **机密变量对模型可见**：命令里 `printenv` 就能读到。接受“尽力打码＋界面提示”，不承诺模型看不到。
3. **多仓库时根目录不是 git 仓库**；布局在创建工作区时定下，不做转换。
4. **只读仓库在 OAuth 登录下是策略层**：Runtime 拒绝对它提交与推送，但 OAuth 令牌本身不能按仓库收窄，所以不是凭据层的保证。GitHub App 的安装令牌（可限定仓库与权限）作为后续。
5. **firecracker 不可用时不降级到 gVisor**（见 firecracker-backend.md D-19）。

## 2. 对我之前说法的更正（对照代码后）

- 我之前建议“克隆和推送在宿主侧做、客户机里不放 Git 凭据”。实际上 git 一直在**承载工作区的沙箱里**跑（`services/py-worker/src/agentruntime_worker/workspace/operations.py`）：凭据只作为那一条 git 命令的 `GIT_CONFIG_*` 环境变量交给它，只对远端那台主机生效，不进 argv、日志、事件历史和 `.git/config`；仓库里的 hooks 不跑；远端地址取业务库、不读 `.git/config`；每次操作按租户策略给一条只放行远端主机的联网授权（`wsop-<operation_id>`），结束收回。**本稿沿用这套，不搬到宿主。**
- 我之前说“模型可以提出加仓库的需求，由人批准”。克隆需要用户的 Git 凭据，而 Runtime 不存它（coding-web 保管，每次请求带 `sealed_credential`），所以模型发起的加仓库必须有应用参与。**本期只做用户发起的加、摘仓库**，模型发起留到后面。

## 3. 现状（对照代码，2026-11-01）

Worker 的文件路径省略了前缀 `services/py-worker/src/agentruntime_worker/`。

| 事实 | 出处 |
| --- | --- |
| `CreateWorkspaceRequest` 只有一个 `repo_url`（空则 `git init`）、`ref`、`credential`、`from_workspace_id`、初始 `files`（合计最多 1 MiB、200 个） | `proto/agentruntime/workspace/v1/workspace.proto` |
| `WorkspaceService` 的 RPC：`CreateWorkspace`、`ListWorkspaces`、`GetDiff`、`CommitWorkspace`、`PushWorkspace`、`ArchiveWorkspace`、`DeleteWorkspace`、`ListFiles`、`ReadFile`、`GetLog`、`DiscardChanges`、`SyncWorkspace`；**都没有仓库参数** | 同上 |
| 租户策略 `sandbox.network` 只有 `allowed`、`hosts`、`max_grant_minutes`、`approver`、`max_up_bytes`、`max_down_bytes`、`max_conns`、`max_rps`、`preview`；**没有“模式”**；`host_pattern` 不接受裸 `*` | `schemas/tenant_policy.schema.json` |
| 联网授权属于 Task：提交时的 `network_request` 由 task-service 核对写入；运行中的 `request_network` 逐次审批；`GrantStore.create(owner, request, policy, task_id=, approved_by=, workspace_id=, session_id=)`，时长按分钟 | `services/py-worker/src/agentruntime_worker/sandbox/grants.py` |
| 每条命令的环境变量走 `exec(env=…)`：至多 64 个、合计 64 KiB，名字规则见 `check_exec_env`，`AGENT_*` 不许 | `libs/py-core/src/agentruntime_core/providers/sandbox.py` |
| Broker 借环境时注入包镜像与代理变量，并注入租户的 `sandbox/test-env`（租户 SecretStore 里的 JSON 对象；不能盖掉镜像与代理的变量；span 只记名字 `sandbox.test_env`） | `sandbox/broker.py`、`sandbox/credentials.py` |
| git 在沙箱里跑，凭据与联网授权见上一节 | `services/py-worker/src/agentruntime_worker/workspace/operations.py` |
| 租户策略顶层 `workspace`：`max_workspaces`、`max_bytes`、`push_branches`（默认 `agent/*`） | `sandbox/policy.py` 的 `WorkspaceLimits` |
| 会话级沙箱按（租户，工作区，会话）复用；一个工作区同一时间只有一个可写环境 | sandbox-platform.md D-13 |
| `workspace_pushes` 记推送的 gate（幂等键、分支、sha、远端、批准人） | workspace-layer.md 第 2 节 |

## 4. 环境配置的模型

下面是**提议的形状**，不是契约；契约的具体字段在 S-01 里定。

```text
WorkspaceConfig（按工作区保存；带 version 与 digest）
  env:     [{ name, value, secret }]            secret = true 的值只写不读
  network: { mode: deny | allowlist | open, hosts: [...], ports: [80, 443] }
  scripts: { setup, start, start_on_failure: warn | block, setup_nonce }
```

规则：

- **只能收窄租户上限，不能放宽**（与 `effect_ceiling` 同一个原则）。保存时校验，超出就整份拒绝并说明，**不静默裁剪**。
- 配置**按工作区**保存，本期不设账号级的默认模板（免得两层互相覆盖），模板留到后面。
- 机密的值存**租户的 SecretStore**（名字 `workspace/<工作区>/env/<NAME>`），业务库的表里只有名字和 `secret` 标记；读回配置时机密的值永远是空的。
- 数据流：

```text
应用（coding-web）──Workspace API──► task-service：校验 ► 业务库（workspace_configs）＋ SecretStore（机密值）
Task 里的 shell_run ──► SandboxBroker 借环境：读配置 ► 合入 Lease.env ► 按联网模式签发授权
                                              ► 建好环境后跑 setup / start（D-24）
```

- 克隆、推送、同步这些 **Workspace API 的操作仍然各自拿一条只放行远端主机的授权**，与联网模式无关：模式是 `deny` 时 Task 里的命令连不了外网，但工作区照样能克隆和推送。

## 5. 契约变更清单（S-01，全部待人审）

本稿不动这些文件，只列出要改什么。人审通过后更新 `proto/breaking-baseline.binpb`。

| 文件 | 提议的改动 |
| --- | --- |
| `schemas/tenant_policy.schema.json` | `sandbox.network.modes`（`deny`、`allowlist`、`open` 的数组，缺省为 `["deny", "allowlist"]`）；`sandbox.env_config`（`max_vars` 默认 32、`max_bytes` 默认 16 KiB、`max_script_bytes`、`setup_timeout_s`、`start_timeout_s`，都只能调低部署的默认值）；`workspace.max_repos`（默认 10） |
| `schemas/defs.schema.json` | 新枚举 `network_mode` |
| `proto/agentruntime/workspace/v1/workspace.proto` | `WorkspaceConfig` 消息，`GetWorkspaceConfig`、`SetWorkspaceConfig`（带 `expected_version`）、`GetEnvironmentLog`；`Workspace.layout`、`repos`；`CreateWorkspaceRequest.layout`；`AttachRepo`、`DetachRepo`、`ListRepos`；`GetDiff`、`CommitWorkspace`、`PushWorkspace`、`SyncWorkspace`、`GetLog`、`DiscardChanges` 加 `repo` |
| `proto/agentruntime/workflow/v1/workspace.proto` | `WorkspaceOperationInput` 加 `repo`（不改工作流的命令序列，仍是一个操作一个 activity） |

业务库的新迁移（不是 S-01，但要人审；迁移号接现有最大号之后，实现时查）：`workspace_configs`、`workspace_repos`、`workspace_pushes.repo`、`sandbox_grants.scope`、`sandbox_setup_cache`。

## 6. 工作项

档位与评审按 [强档实施计划](strong-tier-plan.md) 第 1 节。

### D-23 环境配置：环境变量与联网模式（强档，含契约变更）

**目标**：用户按工作区配环境变量（普通、机密）和联网模式（`deny`、`allowlist`、`open`），只能收窄租户上限。

**交付**：

- 契约（第 5 节）。task-service 的 Workspace API：`GetWorkspaceConfig`、`SetWorkspaceConfig`；表 `workspace_configs`（租户作用域，行级安全）；机密值写租户 SecretStore。
- Worker：Broker 借环境时读配置，合入 `Lease.env`。优先级：平台注入的变量（代理、包镜像）永远最高，不可覆盖——保留名单**由 `sandbox.credentials` 实际注入的名字决定**，不另抄一份；其次是工作区配置的变量；再其次是租户的 `sandbox/test-env`。
- 联网：Broker 借环境时按模式给当前 Task 签发授权。`deny` 不签；`allowlist` 的主机落在租户 `sandbox.network.hosts` 里，超出的在保存时拒绝；`open` 要租户的 `modes` 含 `open`。`approved_by` 记保存配置的用户，`reason` 记配置版本。授权时长取租户 `max_grant_minutes` 与环境剩余寿命的较小者，借环境时发现不足 5 分钟就续签。字节、连接数、频率的上限沿用租户 `sandbox.network` 的 `max_*`。
- 出网网关与授权表：授权加 `scope`（`hosts` 或 `public`）。`public` 只匹配域名，IP 字面量、私有网段、云元数据、CGNAT 仍按 SSRF 防线拒绝；25 端口写死拒绝（`AR_EGRESS_DENY_PORTS`，默认 `25`）。`host_pattern` 不改，**不把 `*` 做成特殊主机**。
- 模式改动立即生效：`SetWorkspaceConfig` 后，task-service 收回该工作区上不再被允许的授权并通知网关（沿用 `FinishTask` 收回与 `AR_EGRESS_GATEWAY_ADMIN_URL` 的做法，按 `workspace_id` 收回）。
- 打码：`shell_run` 的 stdout、stderr 与 trace 在写出之前，把本次注入的机密值整串换成 `***`（放在 Policy Engine 的 Hook 里还是 `shell_run` 自身，写验收测试时核对哪个更合适）。

**难点与防错**

- **机密不进**：环境表、span（只记名字，同 `sandbox.test_env`）、Temporal 事件历史（机密值只在 activity 里从 SecretStore 读，不经工作流载荷）、镜像、快照、`.git/config`、日志。验收里要有扫描。
- **打码只是尽力而为**：值太短（少于 8 个字符）不打码，避免误伤，保存时对太短的机密给出警告但不阻止；base64、URL 编码后的值不处理。这个限制要写进界面与文档。
- **用户自己放进机密的令牌，Runtime 管不了它拿去做什么**：例如把 GitHub 的 PAT 放进机密变量、再开 `open` 联网，命令就能绕过“只推 `agent/*`、由人点按钮”。界面要提示“不要把能推送的令牌放在这里”。
- **读配置失败是 `transient`（重试）**，不用默认值顶替；联网部分失败时按 `deny` 处理，绝不默认放行。
- 保存时的校验：变量名（沿用 `check_exec_env` 的规则与保留名）、个数与字节上限、`allowlist` 主机的格式与包含关系、`expected_version` 并发冲突。
- 只读 Task 不会建环境，也就不会签授权（沿用 D-16）；授权属于 Task，Task 结束时收回（沿用）。
- `open` 下仍然逐连接审计、仍然计流量配额。

**验收**

1. 保存时被拒并说明原因：超过个数或字节上限、保留名、坏名字、`allowlist` 里有不在租户 `hosts` 内的主机、租户 `modes` 不含 `open` 却设了 `open`；`expected_version` 不对时 `conflict`。
2. 机密：读回为空；命令里 `printenv NAME` 读得到，但 stdout 与 trace 里是 `***`；环境表、span、Temporal 事件历史、Worker 与 task-service 的日志里搜不到机密值。
3. 保留变量（代理、包镜像）不能被覆盖；工作区变量覆盖租户 `sandbox/test-env` 的同名项。
4. `deny`：没有授权，连外网失败，包镜像可用。`allowlist`：只能访问列出的主机。`open`：能访问任意公网域名，内网、元数据、IP 字面量、25 端口被拒；D-16 的 SSRF 套件在 `open` 下仍全部被拒。
5. 模式从 `allowlist` 改成 `deny` 后，5 秒内已建立的连接被断开。
6. 只读 Task 不签授权；Task 结束授权收回；授权到期前自动续签。
7. 审计：`open` 下每个连接仍有审计记录，字节计入租户的每日配额。
8. 隔离：工作区 A 的变量与授权不会出现在 B；别的租户读不到配置与机密（越权用例）。
9. 上述 1–8 对每个 `enabled` 的后端（gvisor、firecracker）都成立（参数化）。

**依赖**：D-19、D-16、D-12；S-01 流程。

### D-24 启动脚本：setup 与 start（强档，含契约变更）

**目标**：用户配两种脚本：`setup`（一次性、可缓存、不带机密）和 `start`（每次建环境都跑）。

**交付**：

- 契约：脚本字段并进 D-23 的 `WorkspaceConfig`（第 5 节一并审）。
- Worker：`agentruntime_worker.sandbox.env_scripts`，在环境建好、工作区恢复之后、第一条命令之前运行；脚本用 `bash -lc` 在沙箱里跑，**不在宿主执行**。
- `setup` 的缓存键：运行环境镜像 digest、脚本哈希、锁文件哈希、`setup_nonce`（界面上的“重新执行”按钮改它）。记在业务库的 `sandbox_setup_cache`。后端有 `snapshot` 能力时结果由 D-26 缓存；**没有这个能力的后端（gVisor 旁路）每次建环境都跑 setup**——慢，但结果一样。
- 状态与日志：进 `shell_run` 结果的 `environment`（`setup` 与 `start` 各自的状态、耗时、打码后的日志尾部），也可经 `GetEnvironmentLog` 取最近一次。
- 租户策略的 `env_config.setup_timeout_s`（默认 10 分钟）、`start_timeout_s`（默认 60 秒）只能调低部署默认值。

**难点与防错**

- **`setup` 阶段不给机密**（用户的机密变量和租户的 `sandbox/test-env` 都不注入），因为它的结果会被缓存甚至快照；`start` 阶段给全部。
- **失败语义沿用 D-17**：`setup` 失败或超时，退回不带 setup 的基础环境，在 `environment` 里写明原因；同一个键不反复重试，改了脚本才重跑。`start` 失败默认警告并继续，`start_on_failure: block` 时命令失败（错误码 `sandbox_start_failed`）。
- 脚本是用户的代码，按 `shell_run` 同一级的权限运行；只有 `shell_run` 被允许（effect 上限够）时才会建环境，所以**不新增 effect 通道**。恶意脚本的防线是后端的隔离、联网授权和资源上限，不是脚本检查。
- 脚本能写的位置由后端的可写层决定（工作目录、`/tmp` 等）；写在可写层里的结果在有 `snapshot` 时随快照保留，没有时每次重跑。
- 工作流的确定性：若用 Temporal 编排，I/O 一律在 activity 里，不改已有工作流的命令序列（要改就加版本分支）。
- **待定 1**：缓存未命中时，第一条命令是等 `setup` 跑完，还是像 D-17 那样先用基础环境？我的建议是**等**（上限为 `setup_timeout_s`，期间保持 activity 心跳），因为 `pip install -r requirements.txt` 没跑完就让模型去跑代码，只会让它在“缺模块”的错误里空转。写验收测试时请确认。
- 仓库自带的配置：D-17 对 `devcontainer.json`、`.tool-versions`、`Dockerfile` 的识别不变。**仓库里新增的脚本、联网与机密配置本期不识别**；以后若做，只能作为“提议”，用户在界面上接受后才变成工作区配置（仓库内容不可信）。

**验收**

1. `start` 每次建环境跑一次，结果出现在 `environment` 里；会话级复用时不重复跑。
2. `setup`：第一次跑；同键第二次命中缓存（有 `snapshot` 的后端，记录两次耗时）；改脚本、改锁文件、改 `setup_nonce` 都会重跑。
3. `setup` 阶段 `printenv` 里没有机密，`start` 阶段有。
4. `setup` 失败、超时都退回基础环境并写明原因，不反复重试，改脚本后重试。
5. 恶意脚本（读宿主、访问内网、读别的租户的数据）失败；fork 炸弹和死循环被资源上限与超时挡住，不影响同宿主的其他环境。
6. 日志里没有机密，且有大小上限。
7. 无 `snapshot` 能力的后端（gvisor）上功能正确（每次重跑）。

**依赖**：D-23；D-26 提供缓存（可后补）。

### D-25 多仓库工作区（强档，含契约变更）

**目标**：工作区可以是空项目、连一个仓库，或授权连接多个仓库；多仓库时每个仓库在自己的子目录里。

**交付**：

- 模型：`Workspace.layout` 取 `root` 或 `multi`。
  - `root` 是现有形态：根目录就是仓库（空项目时是没有远端的 `git init`，连一个仓库时有远端）。老工作区保持不变。
  - `multi`：根目录只是容器，不是 git 仓库；仓库挂在子目录，可以 0 到 N 个，事后可加可摘。
- 每个仓库一条记录：`{name, url, ref, path, access: read|write, primary}`。`path` 规则：`[a-z0-9][a-z0-9._-]{0,62}`，不含 `.git`，仓库之间不重叠、不嵌套，不与保留名冲突。
- Workspace API（契约，第 5 节）：`AttachRepo`、`DetachRepo`、`ListRepos`；`GetDiff`、`CommitWorkspace`、`PushWorkspace`、`SyncWorkspace`、`GetLog`、`DiscardChanges` 加 `repo`。`root` 布局可省略；`multi` 省略则 `invalid`。
- **创建只建空容器，仓库逐个 `AttachRepo`**，由应用顺序发起：N 个仓库一起克隆会让一个 activity 太久，也让部分失败难以表达。部分失败时已经挂上的保留，失败的那个单独报错。
- 凭据：每次 `AttachRepo` 或需要远端的操作带该仓库自己的 `GitCredential`（沿用 `sealed_credential`，Runtime 不存）；一次操作只解开它涉及的那个仓库的凭据。
- 表：`workspace_repos`；`workspace_pushes` 加 `repo` 列，并进推送的幂等键。
- 授权的强制在 Runtime：`access: read` 的仓库，提交与推送被 Workspace API 拒绝（`denied`，错误码 `repo_read_only`）；推送仍要符合 `workspace.push_branches`；每个仓库的推送各自写 gate、各自对账。
- 租户策略 `workspace.max_repos`（默认 10），并计入 `workspace.max_bytes`。
- 存档、恢复、复制（`from_workspace_id`）、删除：整个布局一起处理。
- 联网：克隆、同步、推送各自的授权只放行该仓库的主机，并且主机要在租户 `sandbox.network.hosts` 范围里（沿用 `repo_url` 的现有要求）。

**难点与防错**

- **兼容**：`root` 布局的行为与数据不动；`WorkspaceOperationWorkflow` 仍是一个操作一个 activity，不改命令序列。**这个工作流目前没有 golden 事件历史**（2026-11-01 查过 `services/py-worker/tests/histories`）：改它的输入之前先补一份（设 `AR_UPDATE_GOLDEN_HISTORIES=1` 生成，人审后提交），再证明改动后的代码能回放它。
- **远端一律取业务库**（沿用）：每个仓库都成立；沙箱里的代码改 `.git/config` 的 `origin` 影响不了推送目标。
- **只读是策略层的保证**（决定 4）：界面与文档要写明，别让用户以为是凭据层的硬隔离。
- 命令能进入任何仓库目录，但提交与推送只经 API；沙箱里没有 Git 凭据（只有操作时的一次性变量）。
- 同一工作区上的多仓库操作按“一个工作区一个可写环境”（D-13）顺序化。
- `multi` 工作区里目录不重叠的规则要防路径逃逸：`..`、绝对路径、符号链接、大小写不敏感文件系统上的重名。
- **待定 2**：空项目事后绑定远端（给 `root` 布局、无远端的工作区设 `repo_url`）。现有契约没有这个操作。我的建议是不做，要连仓库就新建 `multi` 工作区或重新创建；写验收测试时请确认。
- **不在本期**：模型发起的加仓库（见第 2 节）、GitHub App 令牌、布局转换。

**验收**

1. 能创建三种工作区：空的 `root`、连一个仓库的 `root`、`multi`；老工作区的行为不变；改动前补上的 golden 事件历史，改动后仍能回放。
2. `multi`：挂 A、B 两个测试仓库（`file://`，`AR_WORKSPACE_ALLOW_LOCAL_REMOTES=1`），各在自己的子目录；对 A 的 diff、提交、推送不影响 B。
3. `multi` 里省略 `repo` → `invalid`。
4. `access: read` 的仓库提交与推送被拒（`repo_read_only`）；不符合 `push_branches` 的分支被拒；每个仓库独立写 gate、独立对账（同 sha 不重复推）。
5. 路径冲突、嵌套、保留名、超过 `max_repos`、超过 `max_bytes` 都被拒并说明原因。
6. 在沙箱里改仓库 A 的 `origin`，不影响推送的目标。
7. 存档再恢复后两个仓库的内容与未提交改动一致；复制工作区复制全部仓库。
8. 凭据：事件历史、日志、错误里没有明文；操作 A 时不解开 B 的凭据（把 B 的 `sealed_credential` 故意弄坏，A 的操作仍成功）。
9. 越权：别的租户、别的用户看不到这个工作区及其仓库（越权用例）。

**依赖**：D-06；S-01 流程。

### D-27 个人配置包：skills 等（强档，含契约变更）——只占位

**目标**：用户、租户、平台各自的 skills、指令文件、MCP 工具登记等，以内容寻址的包存放，建环境时只读交付，分层覆盖。

**已定的边界**：

- skill **不能提升权限**：effect 上限、联网、沙箱等级只来自租户策略和 D-23 的配置，包里写了也无效。
- 机密只用引用，不写进包里。
- 包的 digest 记进 trace。
- 大小、文件数、路径有限制（同 `InitialFile` 的口径）。
- skill 的文字进 Context Builder 时放在比系统指令低一级的分区。

**细化前要人定的**：

1. skill 能否带可执行脚本（我的建议：能，只在沙箱里跑，信任等级同 D-24 的脚本）。
2. 分层覆盖的顺序与冲突规则。
3. 与 Registry 的关系：MCP 登记现在只由部署方人工登记且 effect 必须人工登记，用户自带的 MCP 怎么过关。
4. 交付方式：有 `block_attach` 能力的后端用只读块盘，其他后端写文件。

本期不实现，先不写验收。

## 7. 对设计书的建议改动（待人更新原文档）

设计书是 Claude Docs 的导出副本，不直接改。建议改动如下，确认后我再按 [design-changes-workspace.md](design-changes-workspace.md) 的格式写成可直接替换的原文：

| 编号 | 改什么 | 设计书位置 |
| --- | --- | --- |
| W12 | Sandbox 的后端加 `firecracker`；后端登记的 `enabled`、`capabilities`；“旁路不自动降级”的口径 | 4.10、附录 L.11 |
| W13 | 新增“环境配置”：环境变量、联网模式、启动脚本；租户策略的 `network.modes`、`env_config` | 4.10 新增；3.1.2 |
| W14 | 出网网关：`public` 授权（公网域名）、25 端口常拒、vsock 通路 | 4.10.3 |
| W15 | Workspace 层：布局（`root`、`multi`）、多仓库、`repo` 参数、`access` | 4.10.1 |
| 附录 A | 新术语：环境配置、联网模式、启动脚本（`setup`、`start`）、布局 | 附录 A |

## 8. 应用侧（coding-agent 仓库）要做的

本稿不改 coding-agent 仓库，列出来供它的计划跟进。边界不变：只经 Task API 与 Workspace API，不碰工作区目录；工作区路径由服务端拼；界面叫“工作区”。

- 创建向导：空项目、连一个仓库、连多个仓库（勾选授权哪些、每个标只读或可写）。
- 环境设置页：变量表（支持导入 `.env`，**导入时默认全部当机密**，可逐个改成普通）、联网模式（三选一，`allowlist` 时编辑主机列表）、`setup` 与 `start` 脚本编辑器和最近一次的日志。
- 提示文案（中英文两份都要）：机密在命令里能被读到；不要把能推送的令牌放进变量；只读仓库在 OAuth 下是策略层；不认代理的程序在沙箱里联不了网。
- 网页不用构建工具和外部 CDN；服务端的新文字用 `msg`/`writeMsg` 并补 `messages_en.go`。
- 令牌仍只在服务端加密存放，随每次请求以 `sealed_credential` 带给 Runtime。

## 9. 没覆盖的

账号级的配置模板；模型发起的加仓库；GitHub App 令牌；devcontainer 的 `features`；事后给空项目绑定远端；仓库自带配置的“提议”；`open` 之外的端口策略；个人配置包的实现（D-27）。
