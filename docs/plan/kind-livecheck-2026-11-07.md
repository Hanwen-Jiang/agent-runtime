# K8s Sandbox / gVisor 真实回归（2026-11-07）

基线 Runtime `3c9ffcb5`，独立分支 `goal/kind-regression-20261107`。未修改生产集群、现有工作树或业务实现。运行 `go test -count=1 -v ./libs/go-core/adapters/k8ssandbox`，**14 个顶层测试、35 个子用例通过，0 fail、0 skip，157.476 秒**。包含真实 gVisor 内核、租户隔离、CPU 计量、只读根、无 ServiceAccount、出网阻断、令牌保护、存档与文件恢复。

验收过程中发现的 v1 陈旧断言：`TestCommandsCannotReadTheAgentTokenFromProc` 要求命令 PPID=1；但 `f02ee969`（SBX-ZOMBIE）已改为 PID 1 回收孤儿、子 agent 执行命令，实测 PPID=2。现在验证父进程确为 sandbox-agent，若非 PID 1 则其父必须为 PID 1；同时要求 reaper 和实际 agent 的 environ、mem 都不可读，并保留扫描整个 /proc 查令牌的断言。兼容旧 Go agent 直接作为 PID 1 的结构，未删除有效安全断言。

环境问题与处理：

- 节点 systemd 启动失败的原因为宿主 `fs.inotify.max_user_instances=128`，报 `Failed to create control group inotify object: Too many open files`。测试期间临时设为 1024，未修改持久配置；完成后恢复 128。
- CI 镜像缺 Cargo，而 `BuildSandboxAgent` 不消费 `AR_SANDBOX_AGENT_BIN`。从同提交已构建的优化 musl 二进制制作独立 scratch image，通过已有 `AR_SANDBOX_AGENT_IMAGE` 选择。二进制 SHA-256：`bc624d792e88040fe68f766718baa30d96bc3f50708f402ba5872df5aa444f3b`。
- Docker 29 的多平台索引经 CI 旧 CLI save 导出后，kind `--all-platforms` 导入缺层。任务专用 wrapper 使用宿主 CLI，给 save 指定 `--platform linux/amd64`；build 使用现有 legacy builder 的同一平台。未修改测试源码来规避环境失败。
- kind `v0.33.0`，缓存节点 `kindest/node@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5`（Kubernetes `v1.37.0`），gVisor `release-20261005.0`。gVisor 从宿主已安装的三个组件打包，校验 SHA-512 后导入，仅是本机派生产物，不冒充上游下载包。

证据保存在宿主 `/root/proj/agent-projs/.goal-tools/kind-diagnose-20261107/`：`run.sh`、`bin/docker`、`full-suite-reaper.log`（最终通过）、`full-suite-final.log`（旧断言失败）、`node-failed.log`、`create-fixed.log`、`cleanup.log`、`production-nodes-after.txt`。较早失败日志保留，不计为通过。

清理：仅删除本次诊断与测试节点、关联匿名卷、专属 agent image/测试 Python tag、派生 tar 与工作区临时数据；既有镜像、用户数据与其他匿名卷保留。生产 K3s 节点清理后仍为 Ready。无新模型费用。该记录证明该冻结提交与回归补丁的行为，不替代之后业务变更的必要复验。
