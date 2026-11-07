# 环境配置与多仓库工作区（D-23、D-24、D-25、D-27）

> 2026-11-07：D-25、D-27按本设计已实现部署并完成生产验收；当前证据与限制见[全量Goal执行记录](goal-full-20-2026-11-07.md)。

> 2026-11-01 起草；2026-11-02 按用户要求补齐云环境增强设计并发布到仓库。**状态：设计草案，未实现；本次文档补全与推送不等于契约或代码验收。** 第 1 节保留此前已确认决定；本次增量见第 1.1 节。实现仍按仓库流程先写验收测试；契约变更清单见第 5 节，走 S-01，本次不修改 `/proto`、`/schemas` 或兼容性基线。
> Firecracker 后端（D-19～D-22、D-26）在 [firecracker-backend.md](firecracker-backend.md)。依据：[沙箱平台](sandbox-platform.md)、[Workspace 层](workspace-layer.md)、[云上部署计划](cloud-deploy.md)，以及 2026-11-01 对当前代码的核对（第 3 节）。
> Codex / Claude 的版本、字段、凭据和脚本语义已单列在 [云环境调研](../reference/cloud-environments-codex-claude.md)。该文记录外部事实；本文拥有本系统的拟议行为与验收，不把竞品限制直接变成我们的要求。
> 术语：“环境配置”“联网模式”“启动脚本”“布局”是**暂名**。CLAUDE.md 要求一律用设计书附录 A 的名称，这几个词还不在里面，需要人更新设计书后统一（第 7 节）。

## 1. 决定（2026-11-01，用户确认）

用户的要求：用户可以对云上的环境做自己的创建配置——环境变量（`.env`）、联网权限（受控列表、禁止、全放行）、启动时自动执行的 bash 脚本；同一个工作区可以是空项目、连一个仓库，还应该可以授权连接多个仓库。逐条定下的取舍：

1. **全放行（`open`）= 所有公网域名**。内网、云元数据、IP 字面量永远拒绝（出网网关的 SSRF 防线不变），25 端口永远拒绝，审计与流量配额照记。**默认对所有租户关闭**，租户策略显式允许才能用。
2. **机密变量对模型可见**：命令里 `printenv` 就能读到。接受“尽力打码＋界面提示”，不承诺模型看不到。
3. **多仓库时根目录不是 git 仓库**；布局在创建工作区时定下，不做转换。
4. **只读仓库在 OAuth 登录下是策略层**：Runtime 拒绝对它提交与推送，但 OAuth 令牌本身不能按仓库收窄，所以不是凭据层的保证。GitHub App 的安装令牌（可限定仓库与权限）作为后续。
5. **firecracker 不可用时不降级到 gVisor**（见 firecracker-backend.md D-19）。

### 1.1 本次增强的变更清单（2026-11-02）

| 范围 | 保留 / 调整 | 原因与落点 |
| --- | --- | --- |
| 上述五项已确认决定 | 保留 | 第 2 项继续约束“直传机密变量”；网络密钥新增独立类别，不把原先的可见性承诺改写成不可见 |
| D-23 配置按工作区独有 | 调整为用户可复用环境，工作区保存选用配置 | 环境与 repo 解耦；同一环境能用于多个仓库，见第 4 节 |
| 凭据每个工作区复制一份 | 调整为个人凭据一次保存、环境显式引用 | 按租户与用户隔离；不自动注入全部个人密钥，见第 4.2 节 |
| D-23 网络授权 | 保留 Task 归属、现有收回路径和租户上限 | 环境提供默认需求；保存配置不代表永久批准未来任意任务 |
| D-24 setup 缓存与静默退回 | 首个交付每次新建实例运行，显式 setup 失败阻止首条业务命令 | 用户要求的前置安装必须成功；D-17 的自动项目镜像回退不变；缓存另属 D-26 |
| `digest`、`setup_nonce`、`sandbox_setup_cache` | 从首个交付移出 | 本阶段不做脚本缓存，无需并行缓存标识与状态表；保留简单 `version` 防并发覆盖 |
| D-25、D-27、Firecracker | 保留现有范围 | 不借本次对齐重写多仓库、个人配置包或后端设计；D-26 的输入顺序问题单列第 11 节 |

下文 D-23 / D-24 与第 4、5、8 节已按此增量统一，旧草案中的按工作区独有配置和首版快照缓存表不再作为这两项的拟议实现。

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

### 4.1 可复用配置、工作内容、运行实例

下面是**概念形状**，具体消息名与字段经 S-01 定。首版环境只允许本人使用；名称用于显示，稳定 ID 用于引用。

```text
EnvironmentConfig（tenant + owner_user；与 repo 无关）
  id, name, description?, version, visibility: private
  env:     [{ name, value }]                  普通变量
  secrets: [{ name, credential_ref, delivery: direct | network }]
  network: { mode: deny | allowlist | open, hosts: [...], ports: [80, 443] }
  scripts: { setup, start, start_on_failure: warn | block }

Workspace 的选用配置（沿用现有工作区元数据，不另建版本历史表）
  environment_id, selected_version, resolved_config（只有普通值与凭据引用）

Session 继续使用自己的 Workspace；SandboxBroker 继续管理运行实例
```

- 用户新建会话时分别选择环境与工作内容：repo / branch、空项目或 D-25 的多仓库；本地入口仍选择文件夹。环境自身不保存仓库清单或 Git 凭据。
- 一个环境可绑定多个工作区；复制配置的是小规模非机密运行参数，不复制仓库或密钥值。保存选用配置是为了让环境编辑不在已有会话里突然更换工具链，无需保留全部历史版本或新的发布状态机。
- 编辑环境影响**新绑定**。已有工作区继续使用选用配置；用户显式应用更新或切换环境时要求工作区空闲，收回该工作区的旧网络授权、替换运行实例，再执行准备流程。无需后台扫描所有引用者。
- 删除环境**不要求**先改绑（2026-11-03 用户定，取代原先的“仍有绑定时拒删”）：还有会话在用时也可以删。每个会话保留建会话时的非机密快照（setup、start、联网档位；coding-web 存这份快照），环境的机密部分（`.env` 与网络密钥的引用）删除时立即删掉；交给 Runtime 的是会话的快照，加上配置还在时的机密部分，配置已删的会话拿不到机密。不级联删除工作区，也不以“解绑”让新工作区意外恢复租户旧变量。首版不提供运行中热切换。Runtime 一侧的做法见 [ENV-02 实施记录](env-02-implementation-2026-11-03.md) 第 5 节。
- 未绑定的旧工作区保留现有行为；可以显示“默认环境”作为当前默认配置入口，首次保存时才创建用户记录。没有已上线的 `workspace_configs` 数据需要自动迁移。
- 环境的创建、编辑、绑定和凭据引用都按服务端可信的租户与用户身份校验。首版 `private` 不允许另一用户借用；后端仍由租户策略选择，环境表单不能提升隔离或 effect 权限。

### 4.2 三类值与个人凭据

| 类别 | 保存与交付 | 可见性 |
| --- | --- | --- |
| 普通变量 | 环境配置中的字符串，合入命令环境 | 使用者及命令可读 |
| 直传机密 `direct` | 个人 SecretStore 项，运行 activity 按引用读取后注入 | 设置页不回显原值；命令可读，沿用第 1 节决定 2，打码仅尽力而为 |
| 网络密钥 `network` | 值只留在 SecretStore，受信任代理对指定 HTTPS 请求注入 | 不把原值交给 shell；不承诺任意外部服务响应都不会回显它 |

个人凭据的归属为 `(tenant_id, user_id, credential_id)`，由服务端生成作用域键。值和少量用途元数据放在同一 SecretStore 项；列出凭据只返回名称、类型、目标和是否已设置，不返回值。当前 [Python SecretStore](../../libs/py-core/src/agentruntime_core/providers/secret_store.py) 与 [Go SecretStore](../../libs/go-core/providers/secretstore/secretstore.go) 只有静态读取、短期凭据和数据密钥能力，**没有用户凭据写入、元数据列举、删除接口**；开发用 `seed_secret` 不是可直接开放的用户管理 API。第一阶段需补齐这些最小管理操作、用户归属校验及 Provider 一致性测试，不另建密钥库服务、业务库值副本或审批状态表。

一个凭据可被本人多个环境引用；只有选用配置明确引用的项参与运行。首版不做“所有个人值自动注入”或全局/项目/环境多层覆盖。普通变量和两类机密的名字不得冲突，也不得覆盖平台代理、包镜像和保留变量。

新增绑定环境不隐式继承租户 `sandbox/test-env`；旧的未绑定工作区继续沿用它，需要时由用户显式迁入个人凭据。轮换仅更新值，不改变引用或目标；更换类型、主机、注入位置时创建新凭据并显式改绑。后续解析使用新值，已拿到直传值的进程不会被追溯清除。删除凭据使后续解析报缺失，运行实例销毁不等于吊销服务端 token。

`.env` 是输入格式。导入普通/直传变量前确认分类；旧草案的“默认当机密”只代表 `direct`，不能据此承诺沙箱不可读。网络密钥还必须配置允许的主机与注入位置，不能从 `.env` 自动推断。界面不把真实值导出到仓库 `.env`。

### 4.3 网络范围与授权

- **只能收窄租户上限，不能放宽**。保存、绑定及 Task 获得网络授权时校验；超出整份拒绝，不静默裁剪。`open` 保留第 1 节已确认边界，不能用裸 `*` 假装实现。
- `deny` 禁止 Task 的业务出网，保留现有包镜像与平台必要通信；Git 克隆、推送、同步仍各拿专用 Workspace 操作授权。界面写清这些例外。
- `allowlist` 可以提供“常用包源”预设；保存为可见的有效主机名单，手工更新预设，不引入定时抓取竞品名单的服务。
- 使用环境等于声明该配置里的网络需求；真正批准仍按现有 `approver` 校验当前提交者。管理员要求不能由环境所有者的保存动作绕过。授权属于 Task，结束收回；需要延长时只在执行入口按当前政策处理，不增加后台续签循环。
- 显式应用更严格的工作区配置时，沿用 D-16 的收回通知，5 秒内断开被收回授权的连接；只编辑可复用环境不改变旧绑定。

### 4.4 网络密钥的最小支持范围（D-23 第二阶段）

首批采用**按主机注入请求头**：个人凭据包含精确主机、header 名称、可选 prefix 和 SecretStore 值引用。仅 HTTPS:443，无通配符；同一目标 header 的重复绑定在保存时拒绝。程序照常访问服务，不要求另建占位符替换协议。Codex 式环境变量占位符留待确有 SDK 兼容需求时增加，不能同时承诺两套未验证机制。

有效使用条件是：当前 Task 授权允许该目标、选用环境引用该凭据、任务用户有权使用它，且目标匹配凭据自身的主机范围。保存密钥不会自动扩大网络名单；`deny` 下网络密钥也不能绕过业务出网限制。代理只在这些条件成立后注入，并逐个检查重定向后的独立请求。

当前 `go-egress-gateway` 的 CONNECT/SNI 透传不能修改 HTTPS 请求头。透明调用需要仅对配置目标启用 TLS 终结与上游 TLS 验证，以及客户机 CA 信任交付；证书固定客户端和自带不受信任证书库的程序暂不承诺支持。若实际只需固定 API，现有 ToolDriver 在沙箱外发请求更小，但它不等价于透明 curl / SDK 能力。

实现选型必须以首个真实服务及客户端验证成本；能力未就绪时该选项不可用，不能降级成直传值。请求注入与凭据解析留在代理/SecretStore，沿用当前环境凭据和 Task 授权；不在计费、Planner、Reviewer 之间传播密钥状态。目标能力涵盖 setup 与运行阶段，但只有第二阶段验收通过后才能启用。

## 5. 契约变更清单（S-01，全部待人审）

本稿不动这些文件，只列出要改什么。人审通过后更新 `proto/breaking-baseline.binpb`。

| 文件 | 提议的改动 |
| --- | --- |
| `schemas/tenant_policy.schema.json` | `sandbox.network.modes`（`deny`、`allowlist`、`open` 的数组，缺省为 `["deny", "allowlist"]`）；`sandbox.env_config`（`max_vars` 默认 32、`max_bytes` 默认 16 KiB、`max_script_bytes`、`setup_timeout_s`、`start_timeout_s`，都只能调低部署的默认值）；`workspace.max_repos`（默认 10） |
| `schemas/defs.schema.json` | 新枚举 `network_mode` |
| task-service 的环境配置 API（消息及文件归属由 S-01 定） | 私有环境 CRUD、个人凭据写入/列举元数据/删除、工作区绑定与显式应用更新、`expected_version` 防覆盖、`GetEnvironmentLog`；不让机密值进入工作流载荷 |
| `libs/py-core/.../providers/secret_store.py` 与 `libs/go-core/providers/secretstore/secretstore.go` | 补用户凭据的写入、元数据列举、删除管理能力；服务端按租户与用户限定范围，管理结果不回显原值；两种语言的契约与一致性测试一起更新 |
| `proto/agentruntime/workspace/v1/workspace.proto` | 工作区选用配置的非机密引用信息；`Workspace.layout`、`repos`；`CreateWorkspaceRequest.layout`；`AttachRepo`、`DetachRepo`、`ListRepos`；`GetDiff`、`CommitWorkspace`、`PushWorkspace`、`SyncWorkspace`、`GetLog`、`DiscardChanges` 加 `repo` |
| `proto/agentruntime/workflow/v1/workspace.proto` | `WorkspaceOperationInput` 加 `repo`（不改工作流的命令序列，仍是一个操作一个 activity） |

存储变更：可复用环境的一份配置记录；现有工作区元数据增加选用配置；D-25 的 `workspace_repos`、`workspace_pushes.repo`；`open` 所需的 `sandbox_grants.scope`。具体表名与迁移号在实现时定，不再并列创建 `workspace_configs`、配置历史表或首版 `sandbox_setup_cache`。个人凭据值复用 SecretStore，不进入业务表。新增状态的成本见第 10 节。

## 6. 工作项

档位与评审按 [强档实施计划](strong-tier-plan.md) 第 1 节。

### D-23 可复用环境、个人凭据与联网模式（强档，含契约变更）

**目标**：用户保存私有环境，分别选择环境与工作内容；个人凭据可跨本人环境引用。联网模式为 `deny`、`allowlist`、`open`，只能收窄租户上限。分两阶段验收，第一阶段不开放网络密钥选项。

**第一阶段交付**：

- 契约与存储按第 4、5 节。环境 CRUD、个人直传凭据管理、绑定与显式应用更新；工作区保存所选非机密配置，复用现有实例生命周期。
- Broker 借环境时读取工作区选用配置，将普通变量与直传值合入 `Lease.env`。平台变量永远不可覆盖；保留名单由 `sandbox.credentials` 实际注入的名字决定。新绑定不继承 `sandbox/test-env`；未绑定旧工作区保持原行为。
- 联网需求由 task-service 按当前 Task、提交者身份与租户 `approver` 校验。`approved_by` 记录实际获准的身份，`reason` 记录环境 ID 与选用版本；不以保存人替代审批人。授权时长遵守租户 `max_grant_minutes`、Task 与实例生命周期，额度沿用现有 `max_*`；到期后的命令入口重新校验，需要再次批准时沿用现有申请流程，不自动延长已过期批准。
- 授权增加 `scope: hosts / public`。`public` 只匹配公网域名，IP 字面量、私有网段、云元数据、CGNAT 与 25 端口仍被拒，`host_pattern` 不增加裸 `*`。收回沿用现有网关通知与连接终止路径。
- 配置读失败不使用默认值顶替；暂时不可用走已有重试，缺失、无权或失效引用明确报错。不能因授权失败默认放行。
- 运行 activity 才读取直传值，不让原值进入配置表、工作流载荷或平台构建镜像。输出写入 stdout、stderr、trace 前按完整值尽力打码。少于 8 字符的值、编码/拆分后的值沿用原草案的限制并在界面说明；不能保证用户代码不把直传值写到自己的文件或外部服务。

**第一阶段验收**：

1. 环境 E 分别用于 repo A、repo B 和空项目；工作区文件与任务授权互不串用，同一个人凭据无需重复存值。默认入口首次保存前不制造额外用户记录。
2. 跨租户、同租户另一用户的读取、修改、绑定及凭据引用被拒；名称不作为授权标识。个人凭据列表与环境读回不回显原值。
3. 保存拒绝坏名字、重复变量、保留变量、超量/超字节、越出租户边界的主机或模式；`expected_version` 不符报 `conflict`。
4. 编辑环境不改变旧绑定；新绑定采用新版本。显式应用更新或切换只在工作区空闲时成功，旧授权收回、旧实例替换；删除环境时仍有绑定也允许：绑定的会话保留非机密快照、立即失去 `.env` 与网络密钥，不删除工作区（2026-11-03 用户定）。
5. 普通值可读；直传测试值可由进程读取，支持的完整值输出被打码；配置表、平台日志、span、Temporal 载荷中没有该测试原值。另测短值/编码的已知限制，不作“任何日志绝不泄漏”的断言。
6. 新绑定不会意外继承租户 `sandbox/test-env`；旧工作区仍可用。凭据轮换后新解析拿到新值；删除后报缺失，已注入进程的旧值不承诺消除。
7. `deny` 禁业务出网，包镜像及独立 Workspace Git 操作按既有规则可用；`allowlist` 只通名单；`open` 仅在租户明确启用时通公网，D-16 的 SSRF 拒绝用例、端口限制、审计与配额仍通过。
8. 管理员审批要求不能由保存环境绕过；只读 Task 不签授权，Task 结束收回；过期授权不能继续使用。显式应用收紧配置时，5 秒内断开被撤销连接。
9. 对每个实际启用且声明支持这项功能的后端运行相同用例；未支持的后端明确拒绝选用，不能静默忽略配置。首个交付在现有后端完成，不等待 Firecracker。

**第二阶段交付与验收**：按第 4.4 节增加个人网络密钥及请求头注入。用一个实际服务/客户端与受控假服务完成以下验证，再开放选项：

1. 假 token 不通过平台注入进入沙箱 env、配置文件或命令输出；只有允许目标收到所配置的头。凭据写入与代理日志也不记录原值；假服务不回显 token。
2. 非 HTTPS、非 443、错误主机、跨用户引用、无 Task 授权、未选用的凭据都不能注入；保存密钥不增加网络可达范围。重定向到另一主机时不携带原主机密钥。
3. 在 setup 与运行两个阶段分别验证客户端 CA 信任及正常调用；证书固定等未支持客户端明确报不兼容，不能回退为原值注入。
4. Task 结束、授权被收回或凭据删除后的新请求不能再注入；代理故障不能绕过外置交付。记录实际客户端的额外延迟与代理资源消耗，确认第一批服务可接受。

**依赖**：D-16、D-12、S-01；D-19 的能力登记可随后接入。第二阶段另需选定真实服务和 HTTPS 注入方案，不阻塞第一阶段。

### D-24 启动脚本：setup 与 start（强档，含契约变更）

**目标**：用户配置 Bash `setup` 与 `start`；新实例准备完成后才运行首条业务命令。首个交付不做 setup 结果缓存，不依赖 D-26。

**交付**：

- 脚本字段归 D-23 的可复用环境（第 5 节一并审）。Worker 在实例建好、工作区恢复后，以工作区根目录为 cwd，依次执行 setup、start，再允许业务命令。多仓库根目录不是 git 仓库，脚本自行选择子目录。
- 使用 `bash -lc` 在沙箱里运行，不在宿主执行；按现有实例租约串行完成一次准备，会话复用同一实例时不重复运行。实例重建会重跑，用户脚本应能处理已存在的依赖与工作区文件。
- setup 只有普通变量和平台必要变量，不注入直传机密或租户 `sandbox/test-env`。D-23 第二阶段完成后可用受控网络密钥；start 和业务命令可用该环境允许的直传值及网络密钥。
- setup 失败或超时阻止首条业务命令，返回失败步骤与日志；不悄悄切到基础环境。用户显式重试或修正后重建实例，不在失败实例上无条件循环重跑。start 失败默认警告并继续，`start_on_failure: block` 时阻止业务命令。
- 结果沿用 `shell_run` 的 `environment`，含两个步骤的状态、耗时、有限大小的日志尾部；`GetEnvironmentLog` 读取最近一次记录，不新增脚本任务队列或执行状态机。机密打码遵守 D-23 的具体边界。
- 租户的脚本字节上限、setup 默认 10 分钟与 start 默认 60 秒超时只能收窄部署上限；准备阶段保持已有 activity 心跳。启动命令必须在时限内返回，长期服务用后台方式启动；不引入新的进程监督服务。

**边界**：

- 脚本使用与 `shell_run` 相同的 effect、隔离、资源和网络边界，不新增 effect 通道；不声称解析脚本即可保证安全。
- D-17 的项目定义检测与自动镜像构建回退保持原样。本项只规定**用户显式配置的前置脚本**，二者失败语义不同且在结果中区分。
- 仓库内容不会自动覆盖网络、密钥或脚本配置；日后若支持，只能先作为用户可接受的建议。
- I/O 在 activity 内；若实际实现改变 Temporal 命令序列，仍须版本分支与回放测试。
- D-26 快照另行处理准备输入与缓存边界（第 11 节），不把本阶段完成等同于快照验收。

**验收**：

1. repo / 空项目均按“恢复工作区 → setup → start → 首条命令”执行，cwd 正确；失败时首条命令未执行。
2. 同实例复用不重跑；重建实例重跑。gVisor 等无 snapshot 的后端也完成整个流程，无缓存表或预热池依赖。
3. setup 看不到直传值，start 可以读取；网络密钥只有第二阶段通过后在两个阶段可用，原值始终不由平台交给脚本。
4. setup 非零退出、超时、取消都返回明确结果；用户显式重试可恢复。start 的 warn / block 行为分别可测。
5. 越权访问宿主、内网、其他租户失败；死循环等被既有资源与超时限制终止；日志有上限且满足 D-23 脱敏口径。
6. D-17 自动构建失败仍按原约定回退 universal；不得把该回退误用于本项显式 setup。

**依赖**：D-23 第一阶段；D-26 可后补，接入前须处置第 11 节列明的边界。

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

### D-27 个人配置包（强档，含契约变更）——简化设计（2026-11-06，Claude 起草，待开工）

> 2026-11-06 重写。原稿（2026-11-02）想用一个通用包装 Skill、指令、MCP 与多层覆盖；之后 N-14（用户 Skill）与 N-15（用户 MCP）各自做完上线，
> 那几块已有归宿，D-27 收窄为“用户的个人偏好”。前置基线（[skills-config-package-baseline-2026-11-02.md](skills-config-package-baseline-2026-11-02.md)）里的
> 归属、路径、大小、digest 规则照用。

**目标**：用户配一次自己的偏好，之后每个会话、每个 Task 自动带上。

**包里三样**

| 部分 | 内容 | 生效方式 |
| --- | --- | --- |
| 个人指令 | 纯文本，上限约 4 KB（例：用中文回复、提交信息用英文、Python 用 pytest） | 作为用户数据放进 Planner 与 tool_loop 的上下文（与 Skill 正文同一分区，低于系统指令），在 Task 目标之后；与本次明确要求冲突时以本次要求为准 |
| 个人文件 | 最多约 10 个、共 256 KB 的小文本配置（`~/.gitconfig`、`~/.npmrc`、`~/.config/ruff.toml` 等） | Sandbox Service 借出环境时写进 `$HOME`，不进工作区；四种后端都有写文件能力，统一在服务里处理 |
| 默认组合 | 新会话默认启用的我的 Skill、MCP 连接、环境配置（只存引用） | coding-web 建会话时预先勾选，用户可在会话里改 |

**明确不放进来**

- 密钥：继续用“网络密钥”页；文件里要用密钥只能引用，保存时扫常见令牌格式，命中就拒绝。
- 可执行脚本：安装依赖、启动服务归环境配置的安装脚本；包里的文件只写入，不执行。
- 任何权限：联网、沙箱等级、effect 上限仍只来自租户策略，包里写了无效。
- 多层覆盖：不做。平台层、团队层的共享内容走 Skill；个人配置只有用户自己这一层。

**存放与版本**：照 N-14 的做法。Registry 里新增一类条目，只有用户层，每人一份、只有本人能读写；每次保存出新版本，旧版本不变；Task 提交时固定当时的版本
（同 `tasks.skill_pins`），中途改配置不影响正在跑的 Task，结果可复现。包的 digest 记进 trace。

**冲突规则**：环境配置或项目自带的同名文件优先，个人文件不覆盖它们，结果里提示一句。

**要改的地方**

| 位置 | 改动 |
| --- | --- |
| 契约（S-01） | Registry 加一类条目与读写接口，写法照抄 N-14 的 SkillService（`skill.v1`） |
| task-service | 提交 Task 时固定版本 |
| Worker | 个人指令进 Planner 与 tool_loop 的上下文 |
| Sandbox Service | 借出环境时把个人文件写进 `$HOME` |
| coding-web | 账号页加“我的配置”：指令文本框、文件列表、默认组合的勾选 |

**不再需要人定**：原稿的四个问题——脚本（不允许）、覆盖顺序（不做分层）、用户 MCP（走 N-15，已完成）、交付方式（写文件，不用块盘）——都已按上面定下。

### 10.1 可复用环境与工作区选用配置

| 项目 | 记录 |
| --- | --- |
| Failure | 按 repo / 工作区复制配置造成重复设置；只存环境 ID 又会让后续编辑无提示改变旧会话的准备参数 |
| Evidence | 用户明确提出跨仓库复用；当前工作区与实例都不是用户保存的配置资源 |
| Likelihood / impact | 跨 repo 复用是直接场景；编辑是否影响旧会话关系到可预期性，实际故障频率未量化 |
| Minimum fix | 一份可复用配置；工作区现有元数据留所选小配置与版本，凭据只有引用；显式应用更新复用实例重建 |
| Benefit | 跨项目复用且旧绑定行为明确；不需要完整版本历史、发布状态或配置变更广播 |
| Performance cost | 绑定时读一份配置并校验；运行读工作区现有元数据，无新增后台扫描 |
| Implementation cost | CRUD、版本冲突、绑定/更新、私有归属校验及 D-23 的针对性验收 |
| Operational cost | 每环境一条配置、每工作区一份小参数；沿用现有持久化与备份 |
| New failure surface | 失效引用、配置删除与并发更新；删除只清机密部分、会话保留快照（2026-11-03 用户定），并发更新由版本冲突处理 |
| Release slice | D-23 第一阶段，连同 D-24 构成首个完整流程 |
| Removal condition | 若已有统一配置资源能满足同样语义，合并进去；不保留并行记录 |
| Decision | `PROPOSE`；仅存 ID 无法保留旧绑定语义，完整历史库没有当前需求 |

### 10.2 个人凭据与外置请求注入

| 项目 | 记录 |
| --- | --- |
| Failure | 每环境重复存值难以复用；直传值可由命令读取，不能提供“真实密钥在沙箱外”的能力 |
| Evidence | 用户提出个人通用密钥与代理注入；当前 `test_env` 返回原值，CONNECT 代理不解密，SecretStore 缺管理接口 |
| Likelihood / impact | 原值可读是确定的机制属性，泄漏频率未知；影响取决于 token 权限 |
| Minimum fix | 第一阶段在 SecretStore 内补个人值及管理操作、环境显式引用；第二阶段只做选定客户端的精确主机 HTTPS 头注入 |
| Benefit | 个人值可复用；受支持的网络调用不把原值交给脚本，仍保留已接受的直传选项 |
| Performance cost | 按实际引用读取凭据；透明注入增加 TLS CPU、内存及延迟，需对首个服务实测，不预报数字 |
| Implementation cost | 双语言 Provider 管理契约、用户归属、代理目标校验及客户端 CA 信任；固定 ToolDriver 比透明代理小，需先比较真实调用需求 |
| Operational cost | 复用 SecretStore 的存储与备份；透明方案另需信任材料、轮换和代理诊断，无独立凭据业务表 |
| New failure surface | 错误用户/目标绑定、重定向误带密钥、证书固定不兼容与代理故障；测试限定已支持范围 |
| Release slice | 个人直传凭据第一阶段；外置网络密钥第二阶段，不阻塞前者 |
| Removal condition | 若实际需求均可用现有受信任工具完成，取消透明注入；后端原生能力满足相同语义时替换自建部分 |
| Decision | 个人凭据和外置交付目标 `PROPOSE`；透明代理具体选型在真实服务确定前 `DEFER`，不得先做通用 TLS 框架 |

### 10.3 全公网授权与延后项

| 项目 | 记录 |
| --- | --- |
| Failure | 当前逐域名单不能表达已确认的全公网意图；裸 `*` 又不符合现有 host_pattern 契约 |
| Evidence | 第 1 节决定 1 已确认；现有 SandboxSpec 仅有 none / allowlist |
| Minimum fix | 在现有授权增加 hosts / public 范围，继续同一路径检查域名、IP、端口、额度与收回 |
| Benefit | 实现明确的产品选项，不引入永久环境授权或绕过网关 |
| Performance cost | 每连接增加一种匹配分支；不增加外部请求、后台任务或完整远程扫描 |
| Implementation cost | 授权契约、网关分支和复用 D-16 的拒绝/配额/收回测试 |
| Operational cost | 现有授权记录多一个范围字段，审计与配额通道不变 |
| New failure surface | public 分支遗漏已有拒绝条件；用同一组 SSRF 用例验证 |
| Release slice | D-23 第一阶段，仍默认对租户关闭 |
| Removal condition | 若统一授权接口以后能原生表达相同范围，合并该字段，不保留重复判断 |
| Decision | `PROPOSE`，落实已确认要求；不扩展任意端口或 VPN |

setup 缓存、预热池、组织共享、OIDC、VPN 与多级覆盖没有本次新增的使用量或兼容性证据，均不追加为首版门槛。D-26、D-27 等已存在的后续工作项保留，不因本次调研自动扩大。

## 11. 交付顺序与单列差异

1. **D-23 第一阶段 + D-24 + 应用入口**：现有后端上完成私有环境、个人直传值、三种网络意图、独立选 repo 和显式脚本；按第 6 节先写验收测试，S-01 处理契约。Firecracker、快照及多仓库不是此流程的前置条件。
2. **D-23 第二阶段**：先选一个真实服务与客户端，确认透明请求需求，再验证 HTTPS 注入、用户归属与 setup / runtime 两阶段。未通过前不展示为已支持。
3. **D-25** 保留独立多仓库交付；**D-26 / D-27** 及第 9 节高级能力按各自范围推进，不隐含变成第一阶段依赖。

以下差异单列，不以切片名义修改权威设计书或 Firecracker 草案：

- **D-26 输入顺序冲突**：[Firecracker 草案](firecracker-backend.md) 第 5 节的 D-26 要求在工作区导入前快照 setup；D-24 的项目脚本要在工作区恢复后运行。D-26 又把锁文件纳入缓存键，却未明确从何处获得；“setup 产物含用户内容”也不能同时被称为“不含租户数据”。在实现缓存前须明确区分基础准备与项目准备，或只缓存不依赖工作区的部分，并单独确认该取舍。本次没有改写 D-26。
- **长期密钥边界**：设计书 4.12 / SecretStore 接口说明要求节点不拿长期密钥，而本稿第 1 节已接受用户直传变量可读。W13 需把“用户主动交给命令的直传值”与“平台凭据/外置网络密钥”写清，不能用日志打码消解这一差异。本次保留已确认决定，不宣称权威原文已同步。
- **实施前的限定输入**：网络密钥首批服务、客户端及 CA 信任方式待选；setup / start 首版已定为 Bash；环境默认需求按 Task 审批，已在 D-23 明确。其余优化不再阻塞首个完整流程。
