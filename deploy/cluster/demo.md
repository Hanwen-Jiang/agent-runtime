# 演示手册与日常运维

依据：`docs/plan/cloud-deploy.md` D-10。前提：按 `deploy/cluster/README.md` 装好了集群、数据层与 Runtime；coding-agent 按它自己仓库的说明部署，
经 Task API 接到这里。下文 `ar` 是 Runtime 的 Helm release 名，命名空间 `agent-runtime`。

## 准备

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl -n agent-runtime get pods                         # task-service、model-gateway、memory、四个 Worker 池、collector
kubectl -n observability port-forward svc/grafana 3000:80 # Grafana（面板“Agent Runtime”，deploy/data/dashboards）
kubectl -n observability port-forward svc/langfuse-web 3001:3000
kubectl -n data port-forward svc/temporal-web 8080:8080   # Temporal 的界面，看工作流的事件历史
```

租户策略经 Registry 发布：把文档写进种子文件（格式见 `services/go-task-service/internal/registry/seed.go`），
经端口转发连业务库，用 task-service 的 `registry-seed` 发布（幂等）：

```json
[{"kind": "tenant_policy", "tenant": "demo-invited", "name": "default", "document": {"effect_ceiling": "write_reversible", "sandbox": {"backend": "gvisor"}}},
 {"kind": "tenant_policy", "tenant": "demo-public", "name": "default", "document": {"effect_ceiling": "write_reversible", "sandbox": {"backend": "kata"}}}]
```

```bash
kubectl -n data port-forward svc/pg-rw 5432:5432 &
AR_RELATIONAL_DSN="postgresql://agentruntime:<口令>@127.0.0.1:5432/agentruntime" AR_SCHEMAS_DIR=schemas \
  go run ./services/go-task-service/cmd/task-service registry-seed demo-policies.json
```

## 演示

1. **登录应用、下任务**：在 coding-agent 里用租户 `demo-invited` 登录，选一个仓库，让它修一个失败的测试。界面上的进度是 SSE 推过来的；
   `python scripts/sse_probe.py --base-url https://<域名> --task-id <任务>` 可以看到事件逐条到达。
2. **删 Worker Pod，任务照样跑完**：任务进行中执行

   ```bash
   kubectl -n agent-runtime delete pod -l app.kubernetes.io/component=worker --wait=false
   ```

   Deployment 拉起新的 Pod，任务从 Temporal 的事件历史接着跑；在 Temporal 界面里能看到 activity 的重试与工作流的续跑，结果与不删时一致。
3. **KEDA 扩缩**：一口气提交二三十个只读任务（例如“总结这个仓库的结构”），然后

   ```bash
   kubectl -n agent-runtime get scaledobject
   kubectl -n agent-runtime get deploy -l app.kubernetes.io/component=worker -w
   ```

   积压时 model、decision 池的副本数涨上去，消化完缩回 `minReplicas`。
4. **两种沙箱**：`demo-invited` 的命令在 gVisor 里跑，`demo-public` 的在 Kata 的 microVM 里跑。让两个租户各跑一条 `uname -r`：
   gVisor 报带 `gvisor` 字样的版本（例如 `4.19.0-gvisor`），Kata 报客户机内核的版本，都与宿主不同。Grafana 的“沙箱存活时间（按后端）”能看到两条线。
   沙箱断网：让任务 `curl https://example.com`，命令失败。
5. **Tempo 与 Langfuse**：在 Grafana 的 Explore 里用 TraceQL `{ span.task_id = "<任务>" }` 找到这次任务的 span：`invoke_workflow`
   下面是各轮的 `runtime.round`，节点的模型调用是 `chat`。沙箱里的命令是单独的 `runtime.sandbox` span（不挂在任务的调用链下），
   用 `{ name = "runtime.sandbox" && span.tenant_id = "demo-invited" }` 按租户与时间找。Langfuse 里同一次任务的 token 与花费。
6. **Learning Pipeline**：KEDA 按 trace 主题的积压起 Job；要现场演示就手动跑一次（只看候选与评测结果，不发布）：

   ```bash
   kubectl -n agent-runtime exec deploy/ar-agent-runtime-worker-program -- \
     python -m agentruntime_learning.pipeline run --tenant demo-invited --family <任务族> --traces /tmp/spans.jsonl --dry-run
   ```

   （worker 镜像里没有 learning 包时用 learning 镜像起一个一次性 Pod；trace 文件由 Collector 的导出或 Tempo 的查询得到。）

## 预算与配额

- **模型供应商**：在供应商的控制台设月度花费上限与告警（Model Gateway 另有租户预算，附录 H.6；这里是兜底）。
- **Cloudflare**：Billing → Notifications 打开用量计费的通知；Workers & Pages 的 Containers 用量也在那里设阈值。`wrangler.jsonc` 的
  `max_instances` 限住同时的 microVM 个数。
- **每个租户的沙箱**：Worker 的沙箱后端登记里的 `max_envs_per_scope`（`values-cloud.yaml`：gVisor 4 个、Kata 2 个），超了的命令
  报 `sandbox_quota_exceeded`；整个沙箱命名空间另有 ResourceQuota（`deploy/cluster/sandboxes.yaml`）。
- **面板**：Grafana 的“Agent Runtime”：结束的 Task、模型花费（按租户）、沙箱存活与 CPU 时间（按后端）、Cloudflare 沙箱的估算花费、
  最近 Task 的花费与轮数。

## 日常运维

| 什么时候 | 做什么 |
| --- | --- |
| 每天 | 看 `kubectl -n data get backup` 最新一条是 completed；Grafana 上有没有异常的花费 |
| 每月 | `deploy/data/restore-drill.sh`（从 R2 恢复、比对行数与校验和） |
| 换密钥 | `sops deploy/cluster/secrets/secrets.enc.yaml` 改值 → `sops -d … \| kubectl apply -f -` → 重启 task-service、Worker、Memory Service |
| 升级 | 改 `deploy/cluster/versions.env` 或镜像标签，先在 kind 上跑 `tests/deploy`，再 `helm upgrade` |
| 出事 | Worker 卡住：看 Temporal 界面里的工作流与 activity；沙箱起不来：`kubectl -n sandboxes get pods`、`describe`；
  磁盘满：`kubectl get pvc -A` 与 `df -h /var/lib/rancher` |
