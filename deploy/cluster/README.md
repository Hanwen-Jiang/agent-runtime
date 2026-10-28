# 云上单节点部署

依据：`docs/plan/cloud-deploy.md`（第 2 节的布局；D-02 集群基线、D-03 数据层、D-04 Runtime 上线、D-05 沙箱）。
一台带 KVM 的独服（64 GB 内存、NVMe、Ubuntu 24.04），k3s 单节点；Cloudflare 免费版做 DNS、TLS 与代理；R2 放备份。

## 目录

| 位置 | 内容 |
| --- | --- |
| `deploy/cluster/versions.conf` | 所有组件的版本（装之前核对） |
| `deploy/cluster/install.sh` | 机器 → k3s 集群：gVisor、k3s、Kata、基础清单、cert-manager、KEDA、CloudNativePG |
| `deploy/cluster/verify.sh` | 核对 D-02 验收 1–3（RuntimeClass、gVisor 与 Kata 的内核、沙箱断网、没有令牌） |
| `deploy/cluster/k3s/` | k3s 配置、Pod Security 默认值、containerd 模板（runsc）、Traefik 配置 |
| `deploy/cluster/*.yaml` | 命名空间、沙箱命名空间的约束、RuntimeClass、工作区与业务对象的卷 |
| `deploy/cluster/secrets/` | SOPS 加密的 Secret（见那里的 README） |
| `deploy/data/` | Postgres（CloudNativePG，备份到 R2）、Temporal、Qdrant、Redpanda、Langfuse、Tempo、Grafana |
| `deploy/helm/agent-runtime/values-cloud.yaml` | Runtime 的 Chart 在这台机器上的 values |
| `deploy/cloudflare/sandbox-bridge/` | 可选的第二个 microVM 后端（Cloudflare Containers，D-07） |

## 步骤

1. **买机器与域名**（你来做）：独服要有 `/dev/kvm`（Kata 要）；域名的 DNS 托管到 Cloudflare。
2. **防火墙**：443 只放 Cloudflare 的地址段（<https://www.cloudflare.com/ips/>，与 `k3s/traefik-config.yaml` 的 `trustedIPs` 一致）；
   6443（API Server）、NodePort 不对外。`firewall.sh` 与 `ar-firewall.service`（安装命令在 service 文件的开头）：servicelb 把 443
   DNAT 给 Traefik，ufw 那种只管 INPUT 的规则拦不住，所以在 raw 表里查。22 默认对所有地址开着；有固定地址时再收紧，
   并把 sshd 改成只认密钥。
3. **集群**：把仓库拷到机器上，`sudo deploy/cluster/install.sh`，然后 `deploy/cluster/verify.sh`。
4. **Cloudflare**：DNS 加一条代理（橙色云）的记录指向机器；SSL/TLS 模式 Full (strict)；Origin Server 里生成源站证书，
   放进 Secret `cloudflare-origin-tls`。SSE 不用另外设置（Cloudflare 不缓冲 `text/event-stream`）。
5. **R2**：建桶 `agentruntime-backups`（Postgres 备份）、`agentruntime-langfuse`（Langfuse 的事件）；各建一个只能读写本桶的 API 令牌。
   把 `deploy/data/postgres.yaml` 与 `deploy/data/values/langfuse.yaml` 里的 `ACCOUNT_ID` 换成账号 ID。
6. **密钥**：按 `deploy/cluster/secrets/README.md` 填好、加密、`sops -d … | kubectl apply -f -`。
7. **数据层**：`deploy/data/install.sh`；末尾会打印三个角色的 `rolsuper`、`rolbypassrls`（都应是 `f`）。
   之后跑一次 `deploy/data/restore-drill.sh`（从 R2 恢复到新集群，比对行数与校验和）。
8. **Langfuse 的 API Key**：不用登录界面建。Secret `langfuse` 里的 `init-public-key`、`init-secret-key` 是 Langfuse 第一次启动时
   建的项目 Key（`values/langfuse.yaml` 的 `LANGFUSE_INIT_*`），`agent-runtime-langfuse` 写 `base64(init-public-key:init-secret-key)`。
   管理员是 `init-user-email` 与 `init-user-password`，界面经端口转发看（`demo.md`）。
9. **镜像**：打 `v*` 标签让 CI 推到 GHCR（`.github/workflows/images.yml`），或者本地 `python scripts/build_images.py --push`。
10. **Runtime**：

    ```bash
    helm upgrade --install ar deploy/helm/agent-runtime -n agent-runtime \
      -f deploy/helm/agent-runtime/values-cloud.yaml \
      --set image.registry=ghcr.io/<仓库所有者> --set image.tag=<版本> --set ingress.host=<域名> \
      --set modelGateway.providerBaseUrl=<模型供应商的地址>
    ```

    `values-cloud.yaml` 里沙箱后端的镜像（`worker.shell.backends.*.image`、`agent_image`）也要换成同一个仓库与版本。
11. **验收**（D-04）：Task API 经 HTTPS 可达、Registry 端口从外面连不上；mock 模型下一个 Task 跑到 done；
    任务跑到一半删掉 Worker Pod，任务照样跑完；积压时 KEDA 把 Worker 扩到不止一个副本；
    `python scripts/sse_probe.py --base-url https://<域名> --task-id <正在跑的 Task>`（D-02 验收 4）。

## 内存预算（requests，64 GB 的机器）

| 部分 | 约 | 出处 |
| --- | --- | --- |
| 系统与 k3s（system-reserved、kube-reserved、Traefik、CoreDNS 等） | 3.5 GiB | `k3s/config.yaml` |
| 数据层：Postgres 2、Temporal 1.25、Qdrant 1、Redpanda 2、Langfuse（含 ClickHouse、Redis）3.1、Tempo 0.5、Grafana 0.1 | 10 GiB | `deploy/data/` |
| Runtime：最少副本时约 2.4，Worker 都扩到上限时约 6.7 | 2.4–6.7 GiB | `values-cloud.yaml` |
| 沙箱（命名空间配额的 requests） | 32 GiB | `sandboxes.yaml` |
| 合计（最坏） | 约 52 GiB | `tests/deploy/test_cluster.py` 核对不超过 60 GiB |

## Kata 与 gVisor 共存

`install.sh` 先放 containerd 模板（`config-v3.toml.tmpl`，加 runsc），再装 kata-deploy。kata-deploy 4.2 在 k3s 上把自己的 handler
写进 `config-v3.toml.d/kata-deploy.toml`（containerd 的 imports），不动模板（更早的版本改同一个模板）；`install.sh` 装完核对 runsc
还在、Kata 的 handler 在这几个文件之一里有 `privileged_without_host_devices = true`。runsc 不在时：把 `k3s/config-v3.toml.tmpl`
里的 runsc 一段追加回模板，`systemctl restart k3s`。Kata 用 Cloud Hypervisor，跑在 Kata 4 默认的 Rust 运行时上（`kata-clh-runtime-rs`；
4.0 起 Go 运行时的 `kata-clh` 进入弃用期）。不用 Firecracker：它不支持 virtio-fs，用它要另配 devmapper 快照器，kata-deploy 4.2
里也只有 Go 运行时的 `fc`。

## 项目环境的构建

按项目定制的运行环境（D-17，`docs/plan/sandbox-platform.md` 第 5 节）在 `sandbox-builds` 里构建用户的 Dockerfile。BuildKit 要以特权
跑，特权只能停在 Kata 的 VM 里：kata-deploy 给 handler 配的 `privileged_without_host_devices = true` 不能少（install.sh 核对，
没有时退出）；没有 Kata 时构建会被 `builds.yaml` 的准入策略拒绝，环境退回 universal。

构建出的镜像放 Chart 建的环境镜像仓库（`worker.shell.environments.registry.deploy`），节点经 NodePort 30500 拉：

1. 定一个用户与口令：SecretStore 的 `sandbox/env-registry` 写 `{"username": …, "password": …}`；Secret `agent-runtime-env-registry`
   的 `htpasswd` 写 `htpasswd -nbB <用户> <口令>` 的输出（`secrets/secrets.example.yaml`）。
2. 跑 install.sh 时带上 `ENV_REGISTRY_USER`、`ENV_REGISTRY_PASSWORD`：写 `/etc/rancher/k3s/registries.yaml`（`k3s/registries.yaml.tmpl`，
   0600），节点拉镜像时用它认证、走 http。之后改口令要重跑一次（或改这个文件再 `systemctl restart k3s`）。

## 单节点的前提，加节点之前要换掉的

- 业务对象（`agent-runtime-objects`）与 SecretStore 的可写文件（`agent-runtime-secretstore`）是 local-path 的卷，只在一台机器上共用：
  换成 R2（Chart 的 `objectStore.s3`，D-09）与 OpenBao（设计书 4.12）。
- 工作区（`workspaces.yaml`）是宿主机目录：换成“工作区跟着沙箱走”（D-06，设计书更新之后）。
