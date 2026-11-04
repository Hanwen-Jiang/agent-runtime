# ENV-02 云环境执行链：实施记录（2026-11-03）

> 依据：[人工验收基线](manual-acceptance-baseline-2026-11-03.md) Q16–37；[实现工作项](post-acceptance-work-items-2026-11-03.md) 的 ENV-02 行；[环境配置设计草案](environment-config.md) 第 4、6 节（D-23、D-24）。coding-agent 已有的配置 CRUD 在它的仓库 `internal/server/environments.go`（`384ea04`、`a319601`）。
> 状态：契约 2026-11-03 人审通过（照草案，S-01），随 C、E 两条线一起合进 main，兼容性基线已更新（连同 ENV-01 的 `environment.proto`）。见 `lane-merge-2026-11-03.md`。

## 1. 环境配置怎么到 Runtime（决定）

| 项目 | 记录 |
| --- | --- |
| Failure | coding-agent 已能保存与仓库无关的环境配置（名称、描述、setup、start、`.env`），但 Runtime 拿不到它：沙箱环境建好后不跑 setup、start，`.env` 不进命令，联网档位与网络密钥没有执行点，环境表也没有 setup 的事实可查（Q36、Q37）。 |
| Evidence | Q16、Q17：环境独立保存，启动会话时选 repo，可跨仓库复用；Q20：setup 在新会话的环境启动、代码执行之前；coding-agent 每个会话建一个自己的工作区（`createSession` 的 `from_workspace_id`），会话与工作区一一对应。 |
| 候选 | A. Runtime 另建环境配置库（CRUD、版本、绑定）；B. 每次提交 Task 带配置（经工作流输入到 Node Executor 与工具）；C. 建工作区时带一份配置快照，存在工作区的行上，SandboxBroker 建环境时读它。 |
| Minimum fix | **C**：`CreateWorkspaceRequest.environment`（`WorkspaceEnvironment`）。Runtime 按（租户，工作区）保存快照（`workspaces` 加 4 列，迁移 208），只有普通值与密钥的**名字**；`Workspace.environment_id` 回显 ID。删除环境配置用 `WorkspaceService.DeleteEnvironmentConfig`（Q35）。应用在建好会话的工作副本之后才交接（coding-agent 的 `environmentHandoff`，迁移来的 main 会话不新建工作区），所以另有 `WorkspaceService.SetWorkspaceEnvironment`（设置或清除，效果同建时给），两者是同一份快照。 |
| 为什么不是 A | 配置的 CRUD、同名校验（Q18）、描述上限（Q19）、仅自己（Q33）已在 coding-agent 验收；Runtime 再存一份就是第二套配置记录，还要同步。环境的**状态**仍只在 Runtime 的环境表（Q36）。 |
| 为什么不是 B | 要改 Task 工作流输入、Stage Runner 与 Node Executor（核心链路单写者，另一条线在改）；而且同一个会话的每个 Task 都得重复传，配置在会话中途变了还要定义语义。C 让“编辑环境只影响新会话”（设计草案 4.1）自然成立。 |
| Benefit | 一处契约改动；Worker 的 activity 里读一行业务库就能拿到配置；不新增服务、不新增工作流命令（WorkspaceOperationWorkflow 不变，`args` 多一个键）。 |
| Performance cost | 每次借环境（只在要执行业务命令时）多读一行 `workspaces`；网关每次查环境时多读一行（同样缓存 2 秒）。 |
| Implementation cost | 契约一处（含删除、事后绑定两个 RPC 与 EnvRecord 的 6 个字段）、两个迁移、Broker 的准备步骤、网关的档位与网络密钥。 |
| Operational cost | 无新组件；网络密钥要部署方在 SecretStore 平台作用域放一对网关 CA（`sandbox/egress-ca-cert`、`sandbox/egress-ca-key`）。 |
| New failure surface | 快照里的配置失效（例如引用的密钥被删）：请求时报错、不静默继续；网关 CA 泄露的影响面（只签发给配置了网络密钥的主机，见第 4 节）。 |
| Decision | **KEEP C**（2026-11-03 人审通过）。会话中途换环境、显式“应用更新”到已有工作区不在本项（设计草案 4.1 的显式应用更新另做）。 |

## 2. setup 与 start（Q20–24）

- 只在要执行业务命令的借用上准备（`SandboxBroker.acquire(prepare=True)`，即 `shell_run`）：新建的沙箱环境建好、工作区恢复之后，依次跑 setup、start，之后才执行业务命令（含 shell_run 的检查点 git 命令）。Workspace API 自己的 git 操作不准备、也不注入 `.env`（用户变量不能影响带凭据的 git）。
- 用 `bash -s`（脚本从标准输入给，不进 argv），当前目录是工作区根；setup 默认 10 分钟、start 默认 60 秒（`SandboxBroker.setup_timeout`、`start_timeout`）。start 要在时限内返回：后台服务用 `nohup … >/dev/null 2>&1 &`。
- 同一个环境复用（同一会话）不重跑；环境表的行上有 `setup_state`，复用时按它判断，不靠进程内的记忆。
- **失败门禁**：setup 非零退出或超时，这个环境不启动：start 与业务命令都不执行，环境表记 `setup_state=failed`、`setup_error`（`environment_setup_failed` / `environment_setup_timeout`），环境销毁；shell_run 报同一个错误码（`invalid`，不自动重试），说明里带退出码与有限长度的输出尾部（平台注入的凭据值先遮掉）。
- start 失败只记 `start_state=failed` 并在结果里说明，不阻止业务命令（设计草案 D-24 的默认 warn）。
- **缓存（Q21）**：缓存键 `sha256("agentruntime-setup-v1\0" ‖ sha256(脚本) ‖ "\0" ‖ 基础镜像 digest)`。缓存的是**文件系统**（参考 [云环境调研](../reference/cloud-environments-codex-claude.md) 第 44 行），而 Sandbox 接口没有通用的“文件系统快照”，所以：
  - 部署配了 D-17 的环境构建（K8s，或登记了 `custom_images: true`）时，setup 在环境里第一次成功后，按缓存键起一次构建（`FROM <基础镜像@digest>` 加上跑这段脚本），产物进 `sandbox_images`（D-17 的同一张表，`kind=setup`）。之后的新环境命中就直接用这个镜像建，`setup_state=cached`，不再跑 setup；start 照跑。
  - 只缓存成功：setup 失败不起构建；构建失败记 failed，下一个新环境照常在环境里跑 setup（成功后再起构建）。
  - 没有环境构建的后端（本地开发、Cloudflare）：每个新环境都跑 setup（`setup_state=succeeded`），环境表里照样记缓存键。
  - 缓存键不含仓库内容（Q17 跨仓库复用）：脚本对工作区的改动（例如在工作区里装依赖）随工作区本身保存，命中缓存的另一个工作区不会有；脚本应当只做与仓库无关的准备。这一点在界面上要写明（coding-agent）。
- 环境表（Q36、Q37）：`sandbox_envs` 加 `environment_id`、`setup_state`（none、running、succeeded、cached、failed）、`setup_key`、`setup_finished_at`、`setup_error`、`start_state`（迁移 207）。EnvironmentService 照原样投影；`ready` 另要求 setup 为 none、succeeded 或 cached。Worker 没迁移 207 时 Go 读旧的列，按 none 处理（那时还没有 setup）。

## 3. `.env`（Q25–27）

- 一行一个 `KEY=value`；行首尾空白去掉，`#` 开头与空行忽略；值原样（不做引号与变量展开）。名字 `[A-Za-z_][A-Za-z0-9_]*`，不重复，最多 64 个、合计 16 KiB（与平台变量一起不超过 exec env 的 64 KiB）。
- 不能盖掉平台注入的变量（代理、包镜像、CA 证书包、`AGENT_*`）：`env_reserved_name`。名字像机密的（含 SECRET、TOKEN、PASSWORD、PASSWD、CREDENTIAL(S)、APIKEY、API_KEY、PRIVATEKEY、PRIVATE_KEY 段）拒绝：`env_secret_name`，与 coding-agent 的校验同一口径。错误信息不带值。
- 交给 setup、start 与每条业务命令；环境的使用者看得到。机密只经网络密钥（第 4 节），值不进 `.env`、不进沙箱的环境变量。
- coding-agent 的 `.env` 上限现在是 64 KiB：建议改成 16 KiB、64 个，否则超出的配置在建会话时被 Runtime 拒绝（`env_too_large`）。

## 4. 联网档位与网络密钥（Q28–32）

- 档位只在 Task 的联网授权与租户策略**之内**再收窄：`deny` 一律拒绝业务出网（`environment_network_denied`；包镜像与 Workspace API 的 git 操作不经它）；`restricted` 只放行名单里的主机（`environment_host_not_allowed`），每一项显式写 `include_subdomains`；`open` 不另加限制。删除了配置的工作区照快照的档位（第 5 节）。
- **没有做的**：`open` 让 Task 不经逐主机授权就能连所有公网域名，要租户策略的 `sandbox.network.modes` 与授权的 `scope: public`（设计草案第 5 节、10.3），是另外的契约，DEFER。现在 `open` 的上限仍是 Task 拿到的授权。
- 网络密钥：`SecretHeader{host, include_subdomains, secret_name, header}`。网关只在 CONNECT 到这台主机的 443、且客户端发的是 TLS 时终结 TLS（用部署方的网关 CA 现签这台主机的证书，只签名单里的主机），逐个请求核对 Host 与 CONNECT 一致、去掉沙箱自己写的同名头、加上 `Authorization: Bearer <值>`（默认）或配置的 header 名（值为原文），再以校验上游证书的 TLS 发出去。值从 SecretStore 租户作用域的 `user-secrets/<建工作区的用户>/<secret_name>` 现读（不缓存；读不到就拒绝，Q85）；不写查询参数；明文 HTTP 不加；重定向到别的主机的请求按那台主机自己的配置。
- 沙箱信任网关 CA：Worker 在准备环境时把 `sandbox/egress-ca-cert`（只有证书）与系统证书包拼成 `/tmp/agentruntime-ca.pem`，经 `SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`、`GIT_SSL_CAINFO` 交给命令。证书固定（pinning）或自带证书库的客户端不支持，会报证书错误，不回退为把值交给沙箱。
- 没配网关 CA 时，配了网络密钥的主机一律拒绝（`secret_injection_unavailable`），不裸发。

## 5. 删除（Q35；2026-11-03 用户定）

还有会话在用时也可以删。每个会话保留建会话时的非机密快照（setup、start、联网档位；coding-web 存这份快照并交给 Runtime），配置的机密部分（`.env` 与网络密钥的引用）删除时立即删掉。Runtime 把快照分两列存（`workspaces.environment` 与 `workspaces.environment_secrets`，迁移 208），所以删除就是清一列：

- `WorkspaceService.DeleteEnvironmentConfig`：task-service 直接在业务库里，对调用者（建会话的用户）用这个环境建的工作区清 `environment_secrets`、记 `environment_deleted_at`，返回个数，幂等。不销毁环境、不阻止新的环境：会话之后的新环境照样按快照跑 setup、start，只是命令拿不到 `.env` 的变量；出网网关按快照的档位放行，不再加网络密钥的头（缓存最多 2 秒）。已经在跑的进程里已有的变量收不回来。
- 配置删除之后再交接（`SetWorkspaceEnvironment` 或建工作区时，同一个 `environment_id`）只收非机密的快照；带了 `env` 或 `secret_headers` 报 `environment_deleted`，删除的标记保留。
- 不新增状态机或后台任务；环境的状态仍只在环境表。

## 6. 成本与新的失败面

| 项目 | 记录 |
| --- | --- |
| Performance | 新环境多一次 setup（命中缓存时省掉）与 start；有网络密钥的主机每个请求多一次 TLS 终结与一次 SecretStore 读；叶子证书按主机在进程内缓存。 |
| Operational | 网关 CA 的生成、轮换与保管（SecretStore 平台作用域）；setup 镜像占环境镜像仓库的配额（D-17 的 `max_images`、`idle_days` 照管）。 |
| New failure surface | setup 脚本慢或失败直接挡住会话（按设计）；网关 CA 被拿到可冒充名单里的主机（只限这个部署的沙箱信任它）；HTTP/2 不终结（ALPN 只给 http/1.1）。 |
| Decision | KEEP（2026-11-03 人审通过）；`open` 的公网授权、密钥写入的公开接口、显式应用更新为后续。 |

## 7. 没覆盖的、与设计不一致的

- 用户密钥的写入、列举与删除：2026-11-04 起是 task-service 的 UserSecretService（SEC-API，`f3b02df`）；coding-agent 的“网络密钥”页（WEB-SEC，coding-agent `609e986`）接它，默认关着（`CODING_NETWORK_SECRETS=1`）：环境配置里还不能引用密钥（第 89 行的 `secret_headers`，D-23 第二阶段）。
- 设计草案 D-24 原定“首个交付不做缓存、setup 失败时可重试修正”；Q21 改为缓存成功结果，本记录按 Q21。D-24 的 `start_on_failure: block` 没做（coding-agent 的配置里没有这一项）。
- 设计草案 4.2 的“直传机密”（命令可读）与 Q27“密钥始终在环境外”冲突：按 Q27 不做直传，只有网络密钥。
- 设计书附录 A 还没有“环境配置”“联网档位”“安装脚本”“启动命令”这些术语（设计草案第 7 节 W13），需要人更新设计书。
- host、docker 两种 shell_run（不经 Sandbox 接口的开发方式）不准备环境。

## 8. 给 coding-agent 的接法（`internal/server/envhandoff.go` 的 `environmentHandoff.Bind`）

在会话的工作副本建好之后调 `agentruntime.workspace.v1.WorkspaceService/SetWorkspaceEnvironment`（与 Task API 同一个地址，同一个令牌）：

| 请求字段 | 取自 coding-agent |
| --- | --- |
| `tenant_id` | `identity.TenantID`（只校验一致） |
| `workspace_id` | `Session.WorkspaceID` |
| `environment.environment_id` | `Environment.ID` |
| `environment.setup_script`、`start_command`、`env` | `Environment.SetupScript`、`StartCommand`、`Env`（原文；`.env` 上限按 Runtime 的 64 个、16 KiB） |
| `environment.network.mode` | `deny` → `NETWORK_MODE_DENY`；`allowlist` → `NETWORK_MODE_RESTRICTED`；`open` → `NETWORK_MODE_OPEN` |
| `environment.network.hosts[]` | `AllowedHosts` 的每一项：`*.example.com` 写成 `{host: "example.com", include_subdomains: true}`（Runtime 的这一项同时匹配 example.com 本身），其余 `{host, include_subdomains: false}` |
| `environment.secret_headers[]` | `{host, include_subdomains, secret_name, header}`；coding-agent 现在没有网络密钥的配置，先不给 |

应答的 `workspace.environment_id` 回显 ID，成功即可把 `Session.EnvironmentHandoff` 记成已交接；`InvalidArgument` 的稳定错误码（`invalid_env_line`、`env_reserved_name`、`env_secret_name`、`env_too_large`、`invalid_network_host`、`invalid_secret_header`、`environment_script_too_large`、`invalid_environment`）可直接映射成界面文字。删除环境时调 `WorkspaceService/DeleteEnvironmentConfig{tenant_id, environment_id}`（清调用者自己交接过的工作区上的机密部分，会话照常）；配置已删的会话再交接时只给快照里的 `environment_id`、`setup_script`、`start_command`、`network`，不给 `env` 与 `secret_headers`（给了报 `environment_deleted`）。setup 的结果与就绪只读 `EnvironmentService` 的 `EnvRecord`（`setup_state`、`setup_error`、`start_state`、`environment_id`、`setup_key`、`setup_finished_at`，`ready` 已含 setup 门禁）。shell_run 的结果另带 `environment_config: {environment_id, setup, start}`；setup 失败时 Task 里的工具错误码是 `environment_setup_failed` / `environment_setup_timeout`。
