# 云部署验收 Gate 清单（2026-11-02）

> 这份清单把 [cloud-deploy.md](cloud-deploy.md) 的 D-02～D-17 从“已有代码/ kind 验证”投影成可执行的下一步。命令只使用仓库已有脚本或测试入口；真实资源未提供时记录 `blocked_external`，不把静态检查当线上通过。

| 项目 | 本地/Kind gate | 真实资源 | 成功判据 | 当前状态 |
| --- | --- | --- | --- | --- |
| D-02 集群安装与入口 | `bash deploy/cluster/install.sh`；`bash deploy/cluster/verify.sh`；`python scripts/sse_probe.py --help` | 新机器、gVisor/Kata 内核、Traefik、Cloudflare DNS | install/verify 全部通过；经 Traefik 的 SSE 可持续收事件 | `blocked_external`：需要新机器和域名 |
| D-03 数据组件 | `python scripts/check_chart_values.py`；`pytest -q tests/deploy/test_cloud.py tests/deploy/test_collector_config.py` | PostgreSQL、Temporal、Qdrant、Redpanda、Langfuse、Tempo、Grafana、R2 | 所有组件 Ready；角色无超级用户/BYPASSRLS；从 R2 恢复并完成 Collector 三路导出 | `blocked_external` |
| D-04 Helm/RBAC/扩缩容 | `python scripts/kind_rehearsal.py`；`pytest -q tests/deploy/test_helm_chart.py` | 真 K8s、Ingress/Traefik、业务 Postgres、外部访问 | kind 九项彩排等价场景在真集群通过；Registry 不可公网访问 | `blocked_external` |
| D-05 Sandbox 后端 | `AR_TEST_KIND=1 python -m pytest -q tests/e2e`（按环境过滤）；`AR_KIND_GVISOR=1` 重跑 | Kata RuntimeClass、嵌套虚拟化节点 | gVisor/Kata 的 shell、隔离、回收、配额和失败回退均通过 | gVisor kind 已有；Kata `blocked_external` |
| D-06 Workspace/存档 | `pytest -q tests/deploy/test_cloud.py` 及 Workspace/沙箱相关测试 | 真 Cloudflare bridge、R2、Kata、coding-agent 新 API | archive/restore、跨会话隔离、diff/commit/push 生命周期完整闭环 | 契约与 coding-agent 迁移仍待；真实桥/R2 `blocked_external` |
| D-07 Cloudflare bridge | TypeScript bridge 的 `tsc`、`wrangler deploy --dry-run`；Python bridge 单测 | Linux/WSL、真实 Cloudflare account、容器端点 | 容器创建/exec/file/destroy 真实闭环，鉴权和路由符合预期 | 假桥已过；真端点 `blocked_external` |
| D-08 策略契约 | `pytest -q services/go-task-service/internal/taskapi`（有 Go 工具链时）；schema contract tests | 人工 S-01 审查、buf 工具链 | effect ceiling 与 sandbox backend 通过 breaking baseline，生成代码可编译 | `blocked_external`：契约人审 |
| D-09 ObjectStore/R2 | Python/Go ObjectStore 一致性测试、Claim-Check 测试 | 真 R2、无共享卷的 task-service/Worker | SigV4、前缀、过期、跨进程大载荷 Claim-Check 全通过 | 假服务已覆盖；真 R2 `blocked_external` |
| D-10 在线演示/观测 | `python scripts/check_chart_values.py`；Telemetry/TraceQL 静态测试 | 公网域名、Tempo、Grafana、Cloudflare | 按手册完成任务；Task 与 sandbox 成本、`runtime.sandbox` span 可查询 | `blocked_external` |
| D-11 环境表/对账 | `pytest -q services/py-worker/tests/test_sandbox_ledger.py`；`AR_TEST_KIND=1` | 真 Postgres、K8s Sandbox bridge | 状态序列、回收、丢失修复、租户隔离、单次 reconcile 在真库真桥通过 | SQLite/假桥已有；真资源 `blocked_external` |
| D-12 配额/回退 | `pytest -q services/py-worker/tests/test_sandbox_quota.py` | 人审后的 schema、Postgres、多个后端 | 并发配额、日/月预算、transient 回退、min isolation 在真库通过 | 代码/测试已有；契约与真库 `blocked_external` |
| D-13 会话复用 | `pytest -q services/py-worker/tests/test_sandbox_sessions.py` | Kata、Session close 信号、可测节点 | 同工作区/会话复用、丢失换新、20 条命令冷启动和 Session close 数据可复现 | 复用测试已有；Kata/信号 `blocked_external` |
| D-14 运行时镜像 | `pytest -q tests/deploy/test_images.py services/py-worker/tests/test_sandbox_runtime.py` | 可拉取基础镜像的 Docker/K8s 节点 | 五种语言真实编译/测试、预拉覆盖、冷启动数字记录 | 静态检查已有；真实镜像/节点 `blocked_external` |
| D-15 包镜像 | Go pkg-mirror 单测与契约测试；Worker env 注入测试 | 官方 PyPI/npm/Go 源、Grafana/Prometheus、K8s 网络策略 | 两次安装第二次不走上游；配额、清除、篡改、凭据脱敏和面板通过 | 假官方源已有；真源/集群/面板 `blocked_external` |
| D-16 出网网关 | Go egress、task-api、Worker egress 测试；Chart 静态检查 | 真 DNS/TLS 站点、Cloudflare/Traefik、Tempo | SSRF、授权/收回、连接/流量配额、预览和 `runtime.egress` TraceQL 全通过 | 单测/假桥已有；真连通性 `blocked_external`，契约仍待审 |
| D-17 环境镜像构建 | `pytest -q services/py-worker/tests/test_sandbox_environments.py services/py-worker/tests/test_oci_registry.py` | Kata BuildKit、恶意样例、私有 registry、节点预拉 | 恶意 Dockerfile 拒绝；两次构建命中缓存；镜像拉取、日志脱敏和 blob GC 通过 | 本地/假构建已有；真 BuildKit/registry `blocked_external` |

## 执行顺序

1. 先跑不依赖外部资源的静态和 kind gate，保存命令、commit、镜像标签和结果。
2. S-01/D-08/D-12/D-16 的人工评审完成后，再更新 proto/schema baseline 并运行生成代码检查。
3. 资源到位后按 D-02→D-04→D-05/D-11→D-09/D-16→D-13/D-14/D-15/D-17→D-10 的顺序执行；每个 gate 单独记录 `passed` 或 `blocked_external`。
4. D-06、D-23/D-24 与 coding-agent 的契约迁移独立验收，不把跨仓库改动混入本仓库的 kind gate。
