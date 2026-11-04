# 全部计划的总表与 DAG（2026-11-03）

> 用户要求：把所有计划（含项目原有的）统计在一起，整理一份 DAG。
> 口径：agent-runtime `origin/main` 的 `7ec2453` 加本分支（`claude/agent-runtime-resume-ku809d`）上的计划草案；coding-agent 以它的 main 与 `docs/plan/` 为准。
> 来源：[work-dag-2026-11-03.md](work-dag-2026-11-03.md)（线与第一波 DAG）、[handoff-2026-11-03.md](handoff-2026-11-03.md)、[lane-merge-2026-11-03.md](lane-merge-2026-11-03.md)、[remaining-work-execution-plan-2026-11-02.md](remaining-work-execution-plan-2026-11-02.md)、[post-acceptance-work-items-2026-11-03.md](post-acceptance-work-items-2026-11-03.md)、[feedback-2026-11-01.md](feedback-2026-11-01.md)（N-xx）、[cloud-deploy.md](cloud-deploy.md) 与 [gate 清单](cloud-deploy-gates-2026-11-02.md)（D-01～D-17）、[firecracker-backend.md](firecracker-backend.md)（D-19～D-22、D-26、D-28）、[environment-config.md](environment-config.md)（D-23～D-27）、[sub-agent.md](sub-agent.md)、[eval-site.md](eval-site.md)、[workspace-read-latency-and-disk-2026-11-03.md](workspace-read-latency-and-disk-2026-11-03.md)、[auth-tokens-2026-11-03.md](auth-tokens-2026-11-03.md)，以及 coding-agent 的 `docs/plan/github-app.md`（G-01～G-06）。
> 本文只汇总与排依赖，不改各项的内容与决定；各项以原文档为准。状态有变时改原文档，再回来改这里。

## 1. 已完成（不再画进 DAG）

| 范围 | 内容 |
| --- | --- |
| 强档计划 | S-01～S-13；T-xx（Model Gateway、Registry、Policy Engine、工具、Telemetry、Learning Pipeline 等）；R-01、R-05 的大部分 |
| 云上部署 | D-01～D-17 的代码；cgent.link 单节点集群在线（Helm 第 23 版，R2 已接上，D-09）；线上验收按 gate 清单部分完成 |
| 反馈修复 | N-01～N-03、N-05～N-13、N-17 |
| 人工验收后的工作项 | ENV-01（只读查询）、MODEL-BUDGET-01/02、ROUTING-01、CONTEXT-01 |
| 第一波（任务 17～24） | 17 判分走 Gateway、22 网关等预算表、19 B1/B2 能力矩阵代码、21 coding-agent 环境配置与 Workspace 界面；C 线（ENV-02、N-10）与 E 线（N-04、N-19、N-20、N-16 的一部分）2026-11-03 人审后合进 main |
| F 线 | F1 模型目录同步、F2 Memory 的评测与线上检索器、F3 文件交付的 Runtime 一侧与 Reviewer 的 highlights、F4 注册防滥用与花费面板 |

## 2. 待部署（已在 main，没上线）

C、E 两条线；F1～F4；task-service 默认不限未终态 Task 数（`bb832bf`）；9 个新候选 Profile（只进 `evals` 租户的白名单）；首事件延迟面板。之后 coding-agent 合 `lane/d`（同时把 `.env` 上限改成 16 KiB、64 个），再部署 coding-web。

## 3. 未完成的工作项

档：近 = 建议近期做；中；远 = 等前置或资源。状态：待做、草案（等人确认）、人定（卡在人的决定或评审）、外部（卡在外部资源）、推迟。

| 编号 | 内容 | 出处 | 依赖 | 状态 | 档 |
| --- | --- | --- | --- | --- | --- |
| DEP-R | 部署 Runtime 下一版（第 2 节） | lane-merge | — | 待做 | 近 |
| DEP-W | coding-agent 合 lane/d，部署 coding-web | lane-merge | DEP-R | 待做 | 近 |
| EV-TB | 查 Terminal-Bench 0/5 的原因，再跑 40 题 | handoff | — | 待做 | 近 |
| EV-SWE | SWE-bench 补跑 matplotlib-13989 | handoff | — | 待做 | 近 |
| EV-DE | DeepEval 把工具调用轨迹放进用例 | handoff | — | **代码完成**（`c1a307a`）；线上重判待验收，见 [记录](ev-de-tool-evidence-2026-11-04.md) | 中 |
| MX-RUN | 能力矩阵第一轮（贵模型走淘汰赛） | work-dag 19-run | DEP-R（新 Profile） | 待做（花钱） | 中 |
| MX-Q | S-09～S-11 格子的改编题：选题与补标签 | work-dag | — | 人定 | 中 |
| MX-PUB | 能力分人审后经 Registry 发布 | work-dag 19-publish | MX-RUN、MX-Q | 人定 | 中 |
| SITE | 评测网站（主页、结果、数据集） | eval-site（H 线） | — | 代码已完成（`5035d85`、`2abcba2`，见 eval-site.md 末节）；上线要人建只读令牌、部署新的 learning 镜像 | 中 |
| MEM | Memory 上线（Milvus、真 embedding、Worker→Memory），LongMemEval 验收 | P1-D、F2 | DEP-R | 待做（要资源） | 中 |
| EV-REAL | 真模型下规划质量与子 Agent 拆分的评测 | lane-merge | DEP-R | 人定（花钱） | 中 |
| WEB-PLAN | 网页：计划区（`plan_updated`、快照里的 plan） | lane-merge（N-04） | DEP-W | 已完成（coding-agent `7983507`，没部署；快照里的 plan 随 WEB-SNAP） | 中 |
| WEB-SUB | 网页：并行子 Agent 的进度 | lane-merge（N-16） | DEP-W | **进行中**（第 8 节） | 中 |
| WEB-SNAP | 网页：用快照画首屏、处理 409 | lane-merge（N-19） | DEP-W | 待做 | 中 |
| F3-E2E | 文件交付的端到端 | n18-file-delivery-f3 | DEP-W | 已完成（`ca4fc2e`，没部署） | 中 |
| SEC-API | 用户密钥的写入、列举、删除接口（S-01） | lane-merge（ENV-02） | 契约人审 | 人定 | 中 |
| WEB-SEC | 网页：网络密钥页 | coding-agent | SEC-API | 待做 | 中 |
| NET-OPEN | `open` 档位的公网授权（S-01） | lane-merge | 契约人审 | 推迟 | 远 |
| D-25 | 多仓库工作区（S-01） | environment-config | 契约人审 | 草案 | 远 |
| D-27 | 个人配置包（skills 等） | environment-config | — | 占位 | 远 |
| N16-R | N-16 剩下的：brief、来源卡片与综合节点、预算份额、mid 档、评测对照（E 线，单写者） | sub-agent | — | 草案 | 中 |
| DELTA | `token_delta`、`thinking_delta` 的推送入口与平台令牌 | lane-merge（N-20） | 安全评审 | 人定 | 中 |
| N-14 | 用户自定义 Skill（Registry 新条目，S-01） | feedback 第 4 节 | 契约人审 | 草案 | 远 |
| N-15 | 用户自己的 MCP Server（S-01） | feedback 第 4 节 | 契约人审 | 草案 | 远 |
| R-04 | SecretStore 的生产实现 | post-acceptance、P1-B | — | 待做（要资源） | 远 |
| R-06 | Learning Pipeline 的成本与版本固定 | P1-B | — | 待做 | 远 |
| G-01～G-06 | GitHub App：密钥与配置 → 收件箱 → 身份与令牌 → 事件到动作 → 运维；G-06 自动触发在 G-03、G-04 之后；上线至少 G-01～G-03 | coding-agent github-app | — | G-01 已完成（coding-agent `175100d`，没部署）；G-02 已完成（coding-agent `a3b1331`，没部署）；G-03 **进行中**（第 8 节；上线要 Client secret）；其余待做 | 中 |
| D-19 | 后端登记的启用开关与能力 | firecracker-backend | — | 已完成（`f9f0c4d`，没部署） | 中 |
| D-28 | sandbox-agent 按原接口改写为 Rust | firecracker-backend | — | 草案（Rust 依赖清单要人审） | 中 |
| D-20 | fc-manager（Rust）与 vsock agent | firecracker-backend | D-19、D-28 | 草案 | 中 |
| D-21 | 客户机 rootfs 与镜像缓存 | firecracker-backend | D-20 | 草案 | 远 |
| D-22 | 出网与预览经 vsock | firecracker-backend | D-20 | 草案 | 远 |
| FC-REAL | 独服上真 VM 的验收（耗时、密度） | firecracker-backend | D-21、D-22 | 待做 | 远 |
| D-26 | 预热快照 | firecracker-backend | D-20、D-21 | 草案 | 远 |
| PERF-00 | 分段量 Workspace 操作与历史加载的耗时；含 WK-LITE-03（进出 Worker 的字节数） | workspace-read 第 2、7 节 | — | Runtime 一侧已完成（`d180ae3` 测试、`fb971d2` 实现、`460b167` 面板检查；没部署）；coding-agent 的历史加载待做 | 近 |
| DISK-A | WS-DISK-01 ① 挂卷也执行大小上限、③ 卷用量告警 | workspace-read | — | 已完成（`55ebf81`，没部署；超限按设计稿拒绝写类工具，“拒绝还是只警告”仍可由人改） | 近 |
| DISK-B | WS-DISK-01 ② 冷热分层（先存档成功再删本地） | workspace-read | DISK-A | 草案 | 远 |
| TSP-1 | TS-PROD-01 ① 生产模式没有 Postgres DSN 拒绝启动 | workspace-read | — | 已完成（`00ef868`，没部署） | 近 |
| CACHE-01 | Cache 的 Valkey 适配器与部署 | workspace-read | — | 已完成（`5a7334f`，没部署） | 中 |
| TSP-2 | TS-PROD-01 ②③ Cache 必须是 Valkey、连接池、task-service 多副本 | workspace-read | TSP-1、CACHE-01 | 已完成（`08e6745`，没部署；部署前先建 Valkey 的 Secret、重跑 `deploy/data/install.sh`） | 中 |
| HIST-01 | 事件按 event_id 持久化，按 Session 批量取（S-01）；网页增量加载 | workspace-read | 契约人审 | 草案 | 中 |
| SBX-01 | Sandbox Service：设计改动稿 → 一期只读 → 二期借还与 exec（核心链路）→ 三期对账与构建 | workspace-read | 设计改动稿人审 | 草案 | 中 |
| WS-READ-01 | 只读操作经 Sandbox Service，不进工作流、不新建沙箱 | workspace-read | SBX-01 一期 | 草案 | 中 |
| WS-READ-02 | Workspace 操作单独一个队列与 Worker 池 | workspace-read | — | 草案 | 中 |
| WS-READ-03 | 文件树与 diff 按工作区 generation 缓存 | workspace-read | （多副本时 CACHE-01） | **进行中**（第 8 节） | 远 |
| WS-BLOB-01 | 存档块的节点本地 LRU 读缓存 | workspace-read | D-20（Firecracker 的块服务） | 草案 | 远 |
| STOR-SEC | 存储直连的安全要求：PUT 签 checksum、If-None-Match、Content-Length，日志打码，精确主机放行 | workspace-read 第 8 节 | — | 已完成能先做的（`b65ab01`：`ObjectStore.SignedUpload`，两种语言；出网网关审计与日志不带查询串的测试）。真实 R2 上三个头是否强制执行要跑 `AR_LIVE_S3`（写真实的桶，要人点头）；精确主机放行与“只在那一次操作里开授权”随 WS-PULL-01 做 | 中 |
| WS-PULL-01 | agent 按清单拉块、只传缺的块 | workspace-read 第 6 节 | D-28、STOR-SEC；Firecracker 下另要 D-20、WS-BLOB-01 | 草案 | 远 |
| WK-LITE-01 | 命令输出由 agent 直接外置 | workspace-read 第 7 节 | D-28、STOR-SEC | 草案 | 远 |
| WK-LITE-02 | 交付文件由 agent 直传 | workspace-read 第 7 节 | D-28、STOR-SEC、DEP-R（F3） | 草案 | 远 |
| ATOK-T | AGENT-TOK-01：测用户命令能否从 `/proc/1/environ` 读到 agent 令牌 | auth-tokens | — | 已完成（`a194d8b`；风险属实，见第 8 节） | 近 |
| ATOK-F | AGENT-TOK-01 的修复（Go 版；Rust 版在 D-28 里同一条验收） | auth-tokens | ATOK-T | 已完成（`69a9a30`，Go 版；没部署） | 近 |
| AUTH-01 | Ed25519 签名的标准 JWT，Runtime 只持公钥；第三方令牌的加密密钥独立 | auth-tokens | — | 已完成（代码；没部署，切换步骤见 auth-tokens 第 4 节） | 中 |
| AUTH-02 | 浏览器只拿可撤销的会话 id，调 Runtime 时现签短期令牌 | auth-tokens | AUTH-01 | 已完成（coding-agent `953fc29`；没部署） | 中 |
| AUTH-03 | Ingress 的生产形态：身份服务或签发服务、网关的全局限流、PKCE | auth-tokens | AUTH-02、TSP-2 | 人定：PKCE、JWKS、全局限流已做（coding-agent `c3d8845`）；身份服务选型见 auth-tokens 第 5 节 | 远 |
| REAL-01 | 线上全组件验收（穿插着跑） | post-acceptance、gate 清单 | 各项部署 | 外部（部分：Cloudflare 桥令牌、预览证书） | 中 |
| SBX-RS-01 | Rust 版 agent 的原型实测 | workspace-read 第 5 节 | — | 取消（改为 D-28） | — |

## 4. DAG

方框的颜色：绿 = 近期建议、蓝 = 部署、白 = 待做或草案、六边形 = 人定或评审、灰 = 推迟或卡外部资源、橙 = 正在实现（第 8 节）。只画未完成的；已完成的见第 1 节。

```mermaid
flowchart LR
  classDef near fill:#d9f2d9,stroke:#2e7d32;
  classDef deploy fill:#dbe9ff,stroke:#1f5fbf;
  classDef human fill:#fff3cd,stroke:#b8860b;
  classDef later fill:#eeeeee,stroke:#888888,stroke-dasharray:4 3;

  subgraph DEPLOY["部署"]
    DEPR["DEP-R 部署 Runtime 下一版<br/>C/E 线、F1～F4、新 Profile"]
    DEPW["DEP-W 合 lane/d、部署 coding-web"]
    REAL["REAL-01 线上验收（穿插）"]
  end

  subgraph EVAL["评测"]
    EVTB["EV-TB 查 T-Bench 0/5 → 40 题"]
    EVSWE["EV-SWE 补跑 matplotlib"]
    EVDE["EV-DE 工具轨迹已实现<br/>线上重判待验收"]
    MXQ{{"MX-Q 改编题选题"}}
    MXRUN["MX-RUN 能力矩阵第一轮"]
    MXPUB{{"MX-PUB 能力分人审与发布"}}
    SITE["SITE 评测网站"]
    MEM["MEM Memory 上线 + LongMemEval"]
    EVREAL{{"EV-REAL 真模型规划与子 Agent 评测"}}
  end

  subgraph WEB["网页（coding-agent）"]
    WPLAN["WEB-PLAN 计划区"]
    WSUB["WEB-SUB 子 Agent 进度"]
    WSNAP["WEB-SNAP 快照首屏"]
    F3E2E["F3-E2E 文件交付端到端"]
    WSEC["WEB-SEC 网络密钥页"]
    G1["G-01 密钥与配置"] --> G2["G-02 收件箱与对账"] --> G3["G-03 身份、安装与令牌"] --> G4["G-04 事件到动作"] --> G5["G-05 运维与界面"]
    G6["G-06 自动触发"]
    GDEP["App 上线（≥ G-03）"]
  end

  subgraph ENV["环境与契约（Runtime）"]
    SECAPI{{"SEC-API 用户密钥接口 S-01"}}
    NETOPEN["NET-OPEN open 档位"]
    D25["D-25 多仓库工作区"]
    D27["D-27 个人配置包"]
  end

  subgraph CORE["核心链路（E 线，单写者）"]
    N16R["N16-R 子 Agent 剩余"]
    DELTA{{"DELTA token/thinking 推送"}}
    N14["N-14 用户 Skill"]
    N15["N-15 用户 MCP"]
  end

  subgraph SEC["凭据与认证"]
    R04["R-04 SecretStore 生产实现"]
    R06["R-06 Learning 成本与版本"]
    ATOKT["ATOK-T agent 令牌泄露测试"] --> ATOKF["ATOK-F 修复"]
    AUTH1["AUTH-01 Ed25519 + 密钥分离"] --> AUTH2["AUTH-02 可撤销会话 + 短期令牌"] --> AUTH3["AUTH-03 Ingress 生产形态"]
  end

  subgraph PERF["性能、磁盘与多副本"]
    PERF0["PERF-00 量耗时与 Worker 字节"]
    DISKA["DISK-A 挂卷大小上限 + 告警"] --> DISKB["DISK-B 冷热分层"]
    TSP1["TSP-1 生产不回落 SQLite"]
    CACHE["CACHE-01 Valkey 适配器"]
    TSP2["TSP-2 Valkey 必填 + 多副本"]
    HISTC{{"HIST-01 契约 S-01"}} --> HIST["HIST-01 事件持久化 + 网页增量"]
    SBXD{{"SBX-01 设计改动稿"}} --> SBX1["SBX-01 一期 只读"] --> SBX2["SBX-01 二期 借还与 exec"] --> SBX3["SBX-01 三期 对账与构建"]
    WSR1["WS-READ-01 读不进工作流"]
    WSR2["WS-READ-02 单独队列"]
    WSR3["WS-READ-03 按版本缓存"]
  end

  subgraph FC["沙箱与 Firecracker"]
    D19["D-19 后端启用开关"]
    D28["D-28 sandbox-agent 改 Rust"]
    D20["D-20 fc-manager（Rust）+ vsock"]
    D21["D-21 rootfs 与镜像缓存"]
    D22["D-22 出网与预览经 vsock"]
    FCREAL["FC-REAL 独服真 VM 验收"]
    D26["D-26 预热快照"]
    STOR["STOR-SEC 存储直连安全"]
    BLOB["WS-BLOB-01 节点块缓存"]
    PULL["WS-PULL-01 agent 按清单拉块"]
    WL1["WK-LITE-01 输出直接外置"]
    WL2["WK-LITE-02 交付文件直传"]
  end

  DEPR --> DEPW
  DEPR --> MXRUN
  DEPR --> MEM
  DEPR --> EVREAL
  DEPR --> REAL
  MXQ --> MXPUB
  MXRUN --> MXPUB
  DEPW --> WPLAN & WSUB & WSNAP & F3E2E
  SECAPI --> WSEC
  G3 --> G6
  G4 --> G6
  G3 --> GDEP

  TSP1 --> TSP2
  CACHE --> TSP2
  TSP2 --> AUTH3
  CACHE -.-> WSR3
  SBX1 --> WSR1
  PERF0 -.-> SBXD
  PERF0 -.-> WSR2

  D19 --> D20
  D28 --> D20
  D20 --> D21 & D22
  D21 --> D26
  D20 --> D26
  D21 --> FCREAL
  D22 --> FCREAL
  D20 --> BLOB
  D28 --> PULL & WL1 & WL2
  STOR --> PULL & WL1 & WL2
  BLOB -.-> PULL
  DEPR --> WL2
  D28 -.-> ATOKF

  class DEPR,DEPW deploy;
  class EVTB,EVSWE,PERF0,DISKA,TSP1,ATOKT,ATOKF near;
  class MXQ,MXPUB,EVREAL,SECAPI,DELTA,HISTC,SBXD human;
  class NETOPEN,D25,D27,N14,N15,R04,DISKB,WSR3,BLOB,D26,FCREAL later;

  classDef doing fill:#ffd8a8,stroke:#d9480f,stroke-width:2px;
  class G3 doing;
```

虚线是“建议先有”而不是硬依赖：PERF-00 的数字决定 SBX-01、WS-READ-02 的轻重；WS-READ-03 在多副本时才需要 CACHE-01；Firecracker 下 WS-PULL-01 经块缓存取块；ATOK-F 的 Rust 版随 D-28 一起做。

## 5. 不能同时改的文件（单写者与重叠）

| 冲突点 | 涉及 | 规则 |
| --- | --- | --- |
| 核心链路（`workflows/task.py`、`stage_runner.py`、`node_executor.py`） | N16-R、DELTA、SBX-01 二期（`shell_run`、`repo_*` 改调服务） | CLAUDE.md：同一时间只由一个 agent 改；SBX-01 二期排在 N16-R 之后，或由 E 线来做 |
| Broker 与后端登记（`agentruntime_worker/sandbox/`、`tools/sandboxes.py`） | D-19、SBX-01、DISK-A、WS-READ-02 | 同一时间一条线；D-19 先做 |
| coding-agent 的 `oauth.go`、`accounts.go`、`store.go` | G-01～G-03、AUTH-01、AUTH-02 | 由 G 线一起做，或 AUTH 排在 G-03 之后 |
| sandbox-agent | D-28、ATOK-F、WS-PULL-01、WK-LITE-01/02 | D-28 期间 Go 版只修 ATOK-F，新接口只加在 Rust 版 |
| `/proto`、`/schemas` | SEC-API、NET-OPEN、D-25、N-14、N-15、HIST-01 | 每项单独成组，走 S-01 |

## 6. 建议的近期顺序

1. **DEP-R → DEP-W**：解开最多下游（网页四项、能力矩阵、Memory 上线、真模型评测、REAL-01）。
2. **便宜的安全项，可以和 1 并行**：DISK-A（盘满会拖垮同节点的数据库）、TSP-1、ATOK-T（确认后再 ATOK-F）。
3. **PERF-00**：先有数字，再定 SBX-01、WS-READ-02 的轻重。
4. **评测收尾**：EV-TB、EV-SWE；MX-Q 人定后跑 MX-RUN。
5. **两条长线并行起步**：G-01～G-03（App 上线的前提）；D-19 与 D-28（Firecracker 的前提，D-28 的 Rust 依赖清单先人审）。

## 7. 要人定的

- 契约人审（S-01）：SEC-API、HIST-01；之后的 NET-OPEN、D-25、N-14、N-15。
- 设计改动：SBX-01（设计书 4.10）；Rust 进技术栈（4.15，D-20、D-28 的依赖清单）；Ingress（4.14，AUTH-03）；附录 A 的环境配置术语（ENV-02）。
- 花钱：MX-RUN、EV-REAL、EV-TB 的 40 题。
- 其他：MX-Q 的选题与标签；DELTA 的内部入口（安全评审）；F4 告警的通知渠道；DISK-B 的闲置天数与 `max_bytes` 超限的处理；Valkey 是否持久化。

## 8. 正在实现（认领）

开工前先在这里登记并推送，做完改成“已完成”并写上提交；别的会话看到“进行中”的不要同时改它独占的文件。

| 编号 | 线 | 谁 | 开始 | 独占的文件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| EV-DE | 评测线 | Codex 桌面会话 | 2026-11-04 | `services/py-learning` 的 `evals/deliverables.py`、`evals/bridges/deepeval_bridge.py`、`evals/frameworks.py` 的 Windows 环境白名单、`benchmarks/swebench.py` 的事件采集扩展点及对应评测测试、deliverables README | 已完成代码与离线验收（认领先推送 `3a47cfa`，实现 `c1a307a`）；线上重判待验收。见 [实施记录](ev-de-tool-evidence-2026-11-04.md) |
| TSP-1 | TSP 线（TSP-1 → CACHE-01 → TSP-2），直接在 main 上提交 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | `services/go-task-service/internal/config`、`cmd/task-service/main.go` 的 Provider 选择 | 已完成（`aee014a` 测试、`00ef868` 实现；没部署） |
| CACHE-01 | TSP 线 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | 新包 `libs/go-core/adapters/valkey`（标准库自写 RESP 客户端，不引新依赖）；`deploy/data` 里新加的 Valkey；task-service 的 Cache 选择（`internal/config`、`cmd/task-service/main.go`） | 已完成（`377bcd8` 测试、`5a7334f` 实现；没部署，部署前要先建 `valkey-agentruntime`、`agent-runtime-valkey` 两个 Secret） |
| TSP-2 | TSP 线 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | task-service 的 `internal/config`、`cmd/task-service/main.go`；Chart 的 `task-service.yaml` 与 values 里 task-service、`secrets.valkeyUrl` 的部分；`libs/go-core/adapters/postgres` 的连接池选项；`tests/e2e` 里新加的两副本用例 | 已完成（`c956594` 测试、`08e6745` 实现；没部署）。TSP 线到此为止：下游 AUTH-03 还要等 AUTH-02 |
| ATOK-T | ATOK 线（ATOK-T → ATOK-F），直接在 main 上提交 | Claude 桌面会话（Opus 5.5，另一个） | 2026-11-03 | `services/go-sandbox-agent`（`cmd/sandbox-agent`、`internal/agent`）；`libs/py-core/tests/adapters/test_k8s_sandbox.py` 新加的一条 | 已完成：`a194d8b` 测试、`69a9a30` 修法（agent 启动时 `PR_SET_DUMPABLE=0`）。GitHub 的 Linux runner（uid 1001）上修法前测试读到了令牌、修法后通过；kind 的那一条修法前也读到了。没部署 |
| PERF-00 | PERF 线（PERF-00 的 Runtime 一侧，含 WK-LITE-03；coding-agent 的历史加载另做），直接在 main 上提交 | 同上 | 2026-11-03 | `services/py-worker` 的 `workspace/operations.py`、`workspace/archive.py`、`tools/platform.py`（只加计时与字节计数，不改行为）、新模块 `workspace/timing.py`；`deploy/data/dashboards/agent-runtime.yaml`（`sandbox/broker.py` 与 py-core 最后没有改） | Runtime 一侧已完成（`d180ae3` 测试、`fb971d2` 实现、`460b167` 面板检查；没部署，基线等部署后取一周）；coding-agent 的历史加载没人认领 |
| AUTH-01 | AUTH 线（AUTH-01 → AUTH-02 → AUTH-03），直接在 main 上提交；先做 Runtime 一侧，coding-agent 一侧（签发与第三方令牌的独立密钥）在确认 G 线没在改 `oauth.go`、`accounts.go`、`store.go` 之后接着做 | Claude 桌面会话（Opus 5.5，ATOK 与 PERF 的那个） | 2026-11-03 | 新包 `libs/go-core/adapters/jwtingress`；task-service 的 Ingress 选择（`internal/config` 里 `AR_INGRESS_*` 的部分、`cmd/task-service/main.go` 的 Ingress 构造与新的 `keygen` 子命令）；Chart 里 task-service 的 Ingress 配置与 `taskService.ingress`；coding-agent 的 `internal/server/server.go`（令牌的签发与校验）、`accounts.go`（第三方令牌的加密密钥）、`cmd/coding-web/main.go` 的环境变量 | 已完成：Runtime 一侧 `885a13f` 测试、`0e9ad9f` 实现；coding-agent 一侧 `ece577f` 测试、`c5e5af3` 实现（JWT 私钥与第三方令牌的独立密钥都是可选的，不配时行为不变）。没部署：切换步骤见 [auth-tokens 第 4 节](auth-tokens-2026-11-03.md) |
| AUTH-02 | AUTH 线，直接在 coding-agent 的 main 上提交 | 同上 | 2026-11-03 | coding-agent 的 `internal/server/server.go` 里 Cookie 与 `authed` 的部分、`accounts.go` 登录成功后建会话的那一处、新文件 `internal/server/browser_sessions.go`、`messages_en.go` 三条文案、`cmd/coding-web/main.go` 的会话有效期变量（网页的静态文件不动） | 已完成（coding-agent `4bda335` 测试、`953fc29` 实现；没部署）：Cookie 里只有会话 id，退出登录与“退出所有设备”（`POST /api/sessions/revoke-all`）立即生效，调 Runtime 时现签 5 分钟的令牌；旧的令牌 Cookie 在有效期内换成会话 |
| AUTH-03 | AUTH 线，两个仓库的 main | 同上 | 2026-11-03 | coding-agent 的 OAuth（`internal/server/oauth.go` 的授权地址与换令牌、`accounts.go` 的回调里核 state 的那一处）与新的 JWKS 端点；`docs/plan/auth-tokens-2026-11-03.md` 的身份服务评估 | 不用人定的部分已完成（coding-agent `03b944b` 测试、`c3d8845` 实现：GitHub、GitLab、Gitea、Google 的 PKCE，`/.well-known/jwks.json`；全局限流在 AUTH-01 里经 Valkey）。剩下身份服务的选型与设计书 4.14，人定，评估见 [auth-tokens 第 5 节](auth-tokens-2026-11-03.md)。AUTH 线到此为止 |
| G-01 | G 线（G-01 → G-02 → G-03 …，coding-agent 的 `docs/plan/github-app.md`），直接在 coding-agent 的 main 上提交 | 同上 | 2026-11-03 | coding-agent 新文件 `internal/server/githubapp.go`（App 的配置与校验）、`accounts.go` 的令牌密钥（`CODING_TOKEN_KEY_PREVIOUS`）、`cmd/coding-web/main.go` 的 `CODING_GITHUB_APP_*`、`CODING_GITHUB_WEBHOOK_SECRET*` 变量、`deploy/k8s/coding-web.yaml` 的私钥挂载 | 已完成（coding-agent `58fec67` 测试、`175100d` 实现；Runtime 的 Chart 检查 `test_github_app_key_is_not_in_the_runtime_chart`；没部署）。与计划不同：私钥单独放 Secret `coding-github-app`（`coding-oauth` 是 envFrom 的），见 github-app.md 的 G-01 |
| G-02 | G 线 | 同上 | 2026-11-03 | coding-agent 新包 `internal/github`（收件箱、处理与对账，Postgres）、`internal/server` 里挂 Webhook 路由的一处、`cmd/coding-web` 的 `github-replay` 子命令与 `CODING_GITHUB_DSN`、`go.mod` | 已完成（coding-agent `92d8421` 测试、`a3b1331` 实现；没部署，上线要建库并给 `CODING_GITHUB_DSN`）。第 6 条的“处理函数执行两次结果相同”等 G-04，告警指标等 G-05 |
| G-03 | G 线 | 同上 | 2026-11-03 | coding-agent 新文件 `internal/github` 里的安装与令牌（`installations.go`、`tokens.go`）、`internal/server` 的 Setup URL 与 `authorizeRepo`（新文件 `githubrepo.go`），以及 `publish.go`、`projects.go` 里取 Git 凭据的地方；Runtime 一侧第 9 条（工作副本 `.git/config` 拿不到令牌）的测试 | 进行中 |
| D-19 | Firecracker 线（D-19；D-20 之后要等 D-28 的 Rust 依赖清单人审），直接在 main 上提交 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | `services/py-worker` 的 `tools/sandboxes.py`、`sandbox/broker.py` 里选后端的部分（`_create`、`_candidates`）、`sandbox/reconcile.py`；Chart 的 `worker.shell.backends` 样例；`scripts/dev_stack.py` 的登记样例 | 已完成（`51716da` 测试、`f9f0c4d` 实现；没部署）。验收 6（一致性测试按启用的后端各跑一遍）没做：一致性测试按适配器在 py-core 里跑，不按登记文件 |
| DISK-A | DISK 线（DISK-A；DISK-B 要人定闲置天数），直接在 main 上提交 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | `services/py-worker` 的 `tools/platform.py` 里写类工具（`repo_edit`、`shell_run`）前后的大小核对、新模块 `workspace/size_guard.py`、`sandbox/reconcile.py` 里报卷用量；`deploy/data/values/grafana.yaml` 的告警与 `dashboards/agent-runtime.yaml` 的面板 | 已完成（`8db9053` 测试、`55ebf81` 实现；没部署）。量大小放在写类工具前后（不改核心链路），没超时同一 Worker 进程内至少隔 30 秒量一次 |
| STOR-SEC | STOR 线（STOR-SEC；WS-PULL-01、WK-LITE-01/02 还要等 D-28），直接在 main 上提交 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | ObjectStore 的预签名：`libs/py-core` 的 `adapters/sigv4.py`、`s3_object_store.py`、`s3test.py`、`dev/object_store.py`、`providers/object_store.py` 与一致性测试；`libs/go-core` 的 `adapters/s3objects`、`providers/objectstore`、开发实现与一致性测试；出网网关审计不带查询串的测试 | 已完成能先做的（`be35ec2` 测试、`b65ab01` 实现）；剩下的见第 3 节 |
| WS-READ-03 | READ 线（WS-READ-03；WS-READ-02 要改 names.proto 的队列，走 S-01；WS-READ-01 等 SBX-01），直接在 main 上提交 | Claude 桌面会话（Opus 5.5） | 2026-11-03 | 业务库 `workspaces` 的 `generation`（迁移 210，`workspace/archive.py`）；`workspace/operations.py` 的分发、`tools/platform.py` 的写类工具之后加一；task-service 的 `internal/workspaceapi` 读操作的缓存与 `cmd/task-service/main.go` 的接线 | 进行中 |
| F3-E2E | F 线（F3-E2E → WEB-PLAN → WEB-SUB），直接在 main 上提交 | Claude 桌面会话（Opus 5.5，F 线） | 2026-11-03 | agent-runtime 新加的 `tests/e2e/test_file_delivery.py`；`tests/e2e/e2e_support/api.py` 的 `submit` 加 `workspace_id`；`services/py-worker/src/agentruntime_worker/activities/stubs.py` 加一个 `deliver_file` 桩计划、桩 tool_loop 可按节点输入先调工具（只加，不改已有的桩计划） | 已完成（`ca4fc2e`：dev_stack 起整套，桩计划调真的 deliver_file；两条用例都过；没部署） |
| WEB-PLAN | F 线（F3-E2E → WEB-PLAN → WEB-SUB），直接在 coding-agent 的 main 上提交 | Claude 桌面会话（Opus 5.5，F 线） | 2026-11-03 | coding-agent 的 `internal/web/static/app.js` 里计划区的部分（新函数，接 `plan_updated`）、`i18n.js` 的 `plan.*` 文字、`style.css` 的计划区样式、新测试 `internal/web/plan_test.go`；不碰 AUTH-01 的 `server.go`、`accounts.go`、`main.go` | 已完成：测试 `f8ff7fd`、实现 `7983507`（coding-agent）；真浏览器用例过；没部署 |
| WEB-SUB | F 线 | Claude 桌面会话（Opus 5.5，F 线） | 2026-11-03 | coding-agent 的 `internal/web/static/app.js` 里子 Agent 进度的部分（新函数，按 node_id 归组 sub_agent 的 tool_status、context_usage）、`i18n.js` 的 `subagent.*` 文字、`style.css` 的进度样式、新测试 `internal/web/subagent_test.go`；不碰 AUTH 与 G 线的 server 文件 | 进行中：验收测试已写（coding-agent `b4bf4a6`，含真浏览器的用例，实现前跳过），等人审后实现 |
