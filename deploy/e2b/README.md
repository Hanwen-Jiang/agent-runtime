# 自托管 E2B（e2b 分支）

把 E2B 的开源运行时（[e2b-dev/runtime](https://github.com/e2b-dev/runtime)，Apache-2.0）跑在一台专用的 KVM 节点上，作为 Runtime 的又一个 microVM 沙箱后端。设计与取舍见 [docs/plan/e2b-backend.md](../../docs/plan/e2b-backend.md)；main 分支不接 E2B。

| 文件 | 作用 |
| --- | --- |
| `kustomization.yaml` | E2B Embed 的单节点 StatefulSet（上游固定在提交 67b3182），加四个补丁 |
| `patches/` | 容忍专用节点的污点；team 的 API key 取自我们的 Secret；去掉网页控制台；`ready` 容器不把 key 打进日志 |
| `firewall.sh` | 节点的 nftables：3000、3002 只给集群；另外十一个端口只在本机 |
| `secrets.example.yaml` | Secret `e2b/e2b-api` 的样子（只有占位符） |
| `values-e2b.yaml` | 叠在 `values-cloud.yaml` 后面：Worker 的沙箱后端登记多一个 `e2b` |
| `../../scripts/e2b_template.py` | 从 Runtime 的沙箱镜像（按 digest）构建 E2B 模板 |

## 机器（你来做）

- 一台**单独的**机器，带 `/dev/kvm`（独立服务器，或开了嵌套虚拟化的云主机），Ubuntu 24.04（内核 6.8 以上、cgroup v2、glibc 2.34 以上）；至少 12 GiB 内存、`/` 上 20 GiB 空闲。不要和 k3s 的主节点合用：E2B 管 VM 的节点进程以 root、宿主网络与 pid 命名空间运行，还要改内核参数（上游明确要求专用节点）。
- 与主节点在同一个内网（或经 WireGuard）；E2B 的端口不暴露到公网。

## 步骤

1. 节点准备（在 E2B 节点上）：

   ```bash
   sudo apt-get update && sudo apt-get install -y iptables rsync e2fsprogs iproute2 nftables
   echo 'vm.nr_hugepages=2048' | sudo tee /etc/sysctl.d/90-e2b.conf && sudo sysctl -p /etc/sysctl.d/90-e2b.conf
   ```

   大页要在 kubelet 启动前预留，kubelet 只按启动时看到的上报。
2. 作为 k3s 的 agent 加入集群（版本与 `deploy/cluster/versions.conf` 的 `K3S_VERSION` 相同），带标签与污点：

   ```bash
   curl -sfL https://get.k3s.io -o k3s-install.sh
   sudo INSTALL_K3S_VERSION=<K3S_VERSION> K3S_URL=https://<主节点>:6443 K3S_TOKEN=<节点令牌> \
     INSTALL_K3S_EXEC="--node-label e2b.dev/single-node=true --node-taint agentruntime.io/dedicated=e2b:NoSchedule" sh k3s-install.sh
   ```

   核对：`kubectl get node <E2B 节点> -o jsonpath='{.status.allocatable.hugepages-2Mi}'` 是 `4Gi`。
3. 防火墙（在 E2B 节点上，开机后跑一次或装成 systemd 单元）：

   ```bash
   sudo ALLOW_CIDRS=<主节点地址>/32,10.42.0.0/16 EXTERNAL_IFACES=eth0,cni0,flannel.1 bash deploy/e2b/firewall.sh
   ```
4. Secret：照 `secrets.example.yaml` 写好，SOPS 加密进仓库，部署机上解密后 `kubectl apply`。同一个 `TEAM_API_KEY` 放进 Runtime 的 SecretStore 种子（平台作用域的 `sandbox/e2b-api-key`）。
5. 装 E2B：`kubectl apply -k deploy/e2b`，再 `kubectl -n e2b rollout status statefulset/e2b --timeout=15m`（参考机器约 3 分钟）。
6. 模板：用 D-01 的流水线推好沙箱镜像，取 digest，构建模板：

   ```bash
   AR_E2B_API_KEY=<TEAM_API_KEY> uv run --no-sync python scripts/e2b_template.py --api-url http://<E2B 节点>:3000 \
     --name agentruntime-python --image ghcr.io/<owner>/sandbox-python@sha256:<digest>
   ```

   私有镜像仓库另设 `AR_E2B_REGISTRY_USERNAME`、`AR_E2B_REGISTRY_PASSWORD`。
7. Runtime：`helm upgrade --install ar deploy/helm/agent-runtime -f deploy/helm/agent-runtime/values-cloud.yaml -f deploy/e2b/values-e2b.yaml`（先把 `values-e2b.yaml` 里的地址换成 E2B 节点的内网地址）。要用它的租户在租户策略里写 `{"sandbox": {"backend": "e2b"}}`。
8. 验收（在部署机上，花的是这台机器的资源）：

   ```bash
   AR_LIVE_E2B=1 AR_E2B_API_URL=http://<E2B 节点>:3000 AR_E2B_SANDBOX_URL=http://<E2B 节点>:3002 AR_E2B_API_KEY=<TEAM_API_KEY> \
     uv run --package agentruntime-core --all-extras pytest libs/py-core/tests/adapters/test_e2b_sandbox.py
   ```

## 要留意的

- 5008 是 E2B 节点进程没有认证的控制口，`firewall.sh` 一定要先跑；`ss -ltnp` 能看到 13 个端口监听在 `*`。
- Embed 走明文 HTTP、没有高可用；只放内网。超过一台 E2B 节点要自己组装（E2B 的代码支持多节点放置，官方的打包只有 BYOC），见设计稿第 7 节。
- 删除：`kubectl -n e2b delete statefulset e2b`，等 Pod 没了再在节点上 `sudo rm -rf /var/lib/e2b/data`（数据库与 team key 一起删）。节点上 host-setup 写的东西（`/etc` 下的文件、大页、iptables 规则）要重装系统才干净。
- 轮换 team key：改 Secret 的 `TEAM_API_KEY` 与 SecretStore 种子，删掉节点上的 `/var/lib/e2b/data/seed-state/team-api-key`，再删 Pod；旧 key 最多再用 5 分钟（api 在 Redis 里缓存）。
