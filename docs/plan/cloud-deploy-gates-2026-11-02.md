# 云部署验收 Gate 清单（2026-11-02）

> 这份清单把 [cloud-deploy.md](cloud-deploy.md) 的 D-02～D-17 从“已有代码/ kind 验证”投影成可执行的下一步。命令只使用仓库已有脚本或测试入口；真实资源未提供时记录 `blocked_external`，不把静态检查当线上通过。
>
> 2026-11-03 更正：本清单写成时没有连到集群，把“新机器和域名”当成了缺的资源。实际上 cgent.link 的单节点集群（美国独服，有 KVM）2026-10-28 已上线，之后多次升级（[cloud-deploy.md](cloud-deploy.md) 第 10 节；线上基线见 [工作 DAG](work-dag-2026-11-03.md)）。下表的状态按第 10 节的记录改过：资源已有但 gate 没有在线上跑的写“没有记录”，不算通过；仍是 `blocked_external` 的只有 Cloudflare 桥的令牌（真端点要花钱）与预览地址的付费证书。**再更正**：R2 早已开通（Postgres 的 WAL 归档与每日备份、Langfuse 都在用），之前把它列为缺的资源是照着过时的记录写的。

| 项目 | 本地/Kind gate | 真实资源 | 成功判据 | 当前状态 |
| --- | --- | --- | --- | --- |
| D-02 集群安装与入口 | `bash deploy/cluster/install.sh`；`bash deploy/cluster/verify.sh`；`python scripts/sse_probe.py --help` | 新机器、gVisor/Kata 内核、Traefik、Cloudflare DNS | install/verify 全部通过；经 Traefik 的 SSE 可持续收事件 | **passed**（2026-10-28，cgent.link）：install.sh 与 verify.sh 全过（gVisor 与 Kata 的内核、沙箱断网、没令牌）；经 Cloudflare 与 Traefik 的 SSE 逐条到达 |
| D-03 数据组件 | `python scripts/check_chart_values.py`；`pytest -q tests/deploy/test_cloud.py tests/deploy/test_collector_config.py` | PostgreSQL、Temporal、Qdrant、Redpanda、Langfuse、Tempo、Grafana、R2 | 所有组件 Ready；角色无超级用户/BYPASSRLS；从 R2 恢复并完成 Collector 三路导出 | 部分：组件在 cgent.link 上运行，业务库三个角色都不是超级用户、没有 BYPASSRLS；VictoriaMetrics 与 Grafana 2026-10-30 装上。R2 已开：WAL 持续归档、每日备份成功（2026-11-02、11-03），Langfuse 用 R2 并已收到约 3 万条事件；恢复演练没有记录；Collector 三路导出（Tempo、Langfuse、Redpanda）都在发，失败 0 |
| D-04 Helm/RBAC/扩缩容 | `python scripts/kind_rehearsal.py`；`pytest -q tests/deploy/test_helm_chart.py` | 真 K8s、Ingress/Traefik、业务 Postgres、外部访问 | kind 九项彩排等价场景在真集群通过；Registry 不可公网访问 | 部分：Runtime 2026-10-28 起在线上，经 Cloudflare 的 HTTPS、Workspace API、真模型 Task 跑到 done、SSE 都通过；源站的 443、6443、NodePort 从公网连不上。KEDA 扩缩容、删 Pod 后恢复等彩排场景与 Registry 端口没有在真集群上单独记录 |
| D-05 Sandbox 后端 | `AR_TEST_KIND=1 python -m pytest -q tests/e2e`（按环境过滤）；`AR_KIND_GVISOR=1` 重跑 | Kata RuntimeClass、嵌套虚拟化节点 | gVisor/Kata 的 shell、隔离、回收、配额和失败回退均通过 | 部分：线上 gVisor 与 Kata 都有（verify.sh 的内核与断网检查通过；2026-10-31 两边的取消与撤销直接调 Provider 通过）；回收、配额、失败回退没有在线上单独记录 |
| D-06 Workspace/存档 | `pytest -q tests/deploy/test_cloud.py` 及 Workspace/沙箱相关测试 | 真 Cloudflare bridge、R2、Kata、coding-agent 新 API | archive/restore、跨会话隔离、diff/commit/push 生命周期完整闭环 | coding-agent 已改用 Workspace API（2026-10-28 起，Workspace 界面 2026-11-03 补齐）；Kata 已有，Kata 上的存档恢复没有记录；真实桥/R2 `blocked_external` |
| D-07 Cloudflare bridge | TypeScript bridge 的 `tsc`、`wrangler deploy --dry-run`；Python bridge 单测 | Linux/WSL、真实 Cloudflare account、容器端点 | 容器创建/exec/file/destroy 真实闭环，鉴权和路由符合预期 | 假桥已过；真端点 `blocked_external` |
| D-08 策略契约 | `pytest -q services/go-task-service/internal/taskapi`（有 Go 工具链时）；schema contract tests | 人工 S-01 审查、buf 工具链 | effect ceiling 与 sandbox backend 通过 breaking baseline，生成代码可编译 | `blocked_external`：契约人审 |
| D-09 ObjectStore/R2 | Python/Go ObjectStore 一致性测试、Claim-Check 测试 | 真 R2、无共享卷的 task-service/Worker | SigV4、前缀、过期、跨进程大载荷 Claim-Check 全通过 | 假服务已覆盖；R2 已有（备份与 Langfuse 在用），但 task-service 与 Worker 仍用共用卷，没有切到 S3 适配器，没有记录 |
| D-10 在线演示/观测 | `python scripts/check_chart_values.py`；Telemetry/TraceQL 静态测试 | 公网域名、Tempo、Grafana、Cloudflare | 按手册完成任务；Task 与 sandbox 成本、`runtime.sandbox` span 可查询 | 部分：公网域名与 Cloudflare 已有，评测看板公开（evals.cgent.link），Grafana 质量面板与告警已装；照 demo.md 走一遍、真 Tempo 上的 TraceQL 查询没有记录 |
| D-11 环境表/对账 | `pytest -q services/py-worker/tests/test_sandbox_ledger.py`；`AR_TEST_KIND=1` | 真 Postgres、K8s Sandbox bridge | 状态序列、回收、丢失修复、租户隔离、单次 reconcile 在真库真桥通过 | SQLite/假桥已有；线上用 Postgres 业务库与 K8s 后端在跑，gate 的各场景没有在线上单独记录 |
| D-12 配额/回退 | `pytest -q services/py-worker/tests/test_sandbox_quota.py` | 人审后的 schema、Postgres、多个后端 | 并发配额、日/月预算、transient 回退、min isolation 在真库通过 | 代码/测试已有；契约与真库 `blocked_external` |
| D-13 会话复用 | `pytest -q services/py-worker/tests/test_sandbox_sessions.py` | Kata、Session close 信号、可测节点 | 同工作区/会话复用、丢失换新、20 条命令冷启动和 Session close 数据可复现 | 复用测试已有；线上有 Kata，20 条命令的冷启动还没量；Session close 信号仍待 |
| D-14 运行时镜像 | `pytest -q tests/deploy/test_images.py services/py-worker/tests/test_sandbox_runtime.py` | 可拉取基础镜像的 Docker/K8s 节点 | 五种语言真实编译/测试、预拉覆盖、冷启动数字记录 | 静态检查已有；线上节点已有（沙箱镜像在服务器上构建并导入 k3s），五种语言的编译测试与冷启动数字没有记录 |
| D-15 包镜像 | Go pkg-mirror 单测与契约测试；Worker env 注入测试 | 官方 PyPI/npm/Go 源、Grafana/Prometheus、K8s 网络策略 | 两次安装第二次不走上游；配额、清除、篡改、凭据脱敏和面板通过 | 假官方源已有；包镜像已部署在 cgent.link，对真官方源的两次安装与面板没有记录 |
| D-16 出网网关 | Go egress、task-api、Worker egress 测试；Chart 静态检查 | 真 DNS/TLS 站点、Cloudflare/Traefik、Tempo | SSRF、授权/收回、连接/流量配额、预览和 `runtime.egress` TraceQL 全通过 | 单测/假桥已有；出网网关已部署在 cgent.link，Workspace API 的克隆、推送按策略经它联网；SSRF、配额与 `runtime.egress` 的 TraceQL 没有在线上记录；预览地址要付费证书，`blocked_external`；契约仍待审 |
| D-17 环境镜像构建 | `pytest -q services/py-worker/tests/test_sandbox_environments.py services/py-worker/tests/test_oci_registry.py` | Kata BuildKit、恶意样例、私有 registry、节点预拉 | 恶意 Dockerfile 拒绝；两次构建命中缓存；镜像拉取、日志脱敏和 blob GC 通过 | 本地/假构建已有；线上有 Kata，真 BuildKit 构建与 registry 没有记录 |

## 执行顺序

1. 先跑不依赖外部资源的静态和 kind gate，保存命令、commit、镜像标签和结果。
2. S-01/D-08/D-12/D-16 的人工评审完成后，再更新 proto/schema baseline 并运行生成代码检查。
3. D-02 已在线上通过；其余在 cgent.link 上按 D-04→D-05/D-11→D-09/D-16→D-13/D-14/D-15/D-17→D-10 的顺序执行；每个 gate 单独记录 `passed` 或 `blocked_external`（D-07 等 Cloudflare 桥令牌；D-09 与 D-06 的存档不再缺资源，只是没切过去）。
4. D-06、D-23/D-24 与 coding-agent 的契约迁移独立验收，不把跨仓库改动混入本仓库的 kind gate。
