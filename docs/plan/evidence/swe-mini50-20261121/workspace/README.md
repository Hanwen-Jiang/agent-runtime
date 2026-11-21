# SWE 工作区初始化修复

2026-11-21，使用 `cloudnium_cloud` 上的常规评测租户和 WorkspaceService，整个检查没有提交模型 Task。

- **基线。** 旧的 `swebench` 操作返回 `sandbox_unavailable / gvisor`。对应沙箱 Pod 已被调度，init 容器却无法拉取 Helm 指定的 `sandbox-agent:goal-f1388e13`：GHCR 对匿名拉取返回 403，k3s 的 k8s.io 镜像库缺少该版本的五个启动标签，预拉取 DaemonSet 未就绪。这时甚至还未调用 git。评测宿主的 Docker 镜像库仍保存同版本和完整摘要。
- **修复。** 仅将部署引用的 sandbox-agent、go、node、python、universal 镜像从该机 Docker 导入 k3s；逐一核对索引摘要，将五个运行镜像设为 CRI pinned，防止 kubelet 回收后再次尝试向私有 registry 匿名拉取。DaemonSet 已 1/1 就绪。[镜像摘要与 CRI 核对](image-cache.json)
- **建区复测。** 正常异步 CreateWorkspace 指定公开的 Sphinx Git URL 及 8056 的完整 base commit。首次在代理连接 GitHub 时遇到 CONNECT 502；第二次在同一租户、同一授权、同一基准下成功克隆并精确返回 `e188d56ed1248dead58f3f8018c0e9a3f99193f7`。出网审计记录前次 TCP 连接失败、后次允许并传输了约 45.6 MB。该情况与镜像缺失分开记录；现有 SWE runner 对这类错误有有限重试。[按操作 ID 的结果](probes.json)
- **挂载与存档。** gVisor 租户使用 PVC 挂载模式，成功的 CreateWorkspace 可以只有 HEAD，没有自动生成的 snapshot_id。另起的 ArchiveWorkspace 独立检查未通过：工作区约 1,459 个文件、64 MiB，网关观测到此归档操作的 362 次允许连接，随后收到 `unknown_env`；该 sandbox Pod 于 11:46:06 UTC 被终止。仅由这两个现象不能证明究竟是哪条回收策略生效。主动 S3 归档可靠性是独立限制，不影响已经成功的挂载盘初始化，未借此推断恢复率。
- **正式执行镜像。** 另将两份 Sphinx 转换环境的原有 Docker 镜像（8056、9367）导入 k3s 并补相同的不可变 digest 引用。定义使用去除预置 testbed 的 Runtime 专用环境。镜像缓存可用和租户的 EnvironmentService 已认定环境 ready 是两件事：同租户 9367 的真实构建 Job 使用 BuildKit，从 11:37:25 至 11:43:29 UTC 成功结束；11:44:12 UTC sandbox-service 独立报告 `16974e4ea1cd state=ready`。单凭缓存或 ready 不推断每次工具执行都使用它。本次没有直接改环境状态表、网关策略或租户授权。

复核时用 `k3s crictl images -o json` 确认五个标签及 pinned 状态、检查预拉取 DaemonSet，使用 WorkspaceService 查询本次操作，不依靠耗时判断是否成功。条件式回滚：后续发布改用并验证了新的启动标签后，解除旧五个镜像上的 `io.cri-containerd.pinned` 标签，可按正常镜像回收策略回收；在仍有使用者时不执行镜像清理。原 Docker 副本保留，不需要重启共享服务。
