# FC-REAL 实测工具与冻结口径（2026-11-06）

本记录属于20项总Goal的FC-REAL，不替代D-26内存快照实现或真实验收。

## 实现与归属

`scripts/fc_real_measure.py`（标准库）与 `tests/deploy/test_fc_real_measure.py` 独立认领；不修改D-26核心、transfer、已有 `real_vm.rs`。基线 `1b79b883`，分支 `goal/fc-real-20261106`。

只调用真实fc-manager现有接口：创建 `POST /v1/envs`，首条客户机命令 `POST /v1/envs/<id>/agent/v1/exec`，对账 `GET /v1/envs`，销毁 `DELETE /v1/envs/<id>`。没有启动或伪造Firecracker进程、没有调用测试替身、没有绕开manager直接操作快照。

D-26的新实现经同一创建接口自动使用基础内存快照。判断恢复必须看到该env_id的 `bootstrap path: memory-snapshot` 日志或明确 `boot_source` 返回值。无此证据就保留unknown/失败；不能把预热池或更短耗时当成内存恢复。当前部署配置没有打开内存快照，工具不会声称已实现或验收。

## 固定测量口径

- 每个模式默认20个样本，可显式增大。记录VM runtime、vCPU毫核、内存MiB、scratch MiB，以及Runtime提交、manager/Firecracker二进制SHA-256、内核/rootfs SHA-256。比较三模式必须使用相同规格和镜像。
- 主要时间为客户端创建请求开始至第一条真实shell命令完成；另记创建API时延。p50/p95固定用nearest-rank，报告样本数，不剔除慢样本、失败或冷启动回退。
- 冷启动要求有效配置关闭预热和内存缓存；预热要求规格匹配池（包括默认scratch大小），且返回ID在调用前确实属于 `_pool`。快照要求禁用池、开启快照缓存，先进行一次真实创建作为缓存填充，单独记账且不计入恢复时延样本。
- 读取 `/proc/stat`、`/proc/meminfo`、`/proc/diskstats` 和CPU/内存/IO PSI；每台VM读取cgroup的 `cpu.stat`、`memory.current`、`memory.peak`、`memory.events`、`io.stat`。保留原始数值、单位、前后增量；缺关键cgroup证据不能宣布测量完整。
- 密度测量保持此前创建的VM同时运行，每次新增VM确认首命令，默认稳定等待1秒。负载明确为 **idle VM + 一条shell命令**，不推断实际编译/模型任务的容量。
- rootfs哈希会预读宿主页面缓存。这里“冷”指新启动VM，不声称宿主磁盘缓存冷。整个比较保持这一口径，不能单独给某组清缓存。

## 容量与资源协调

同时记录 `MAX_VMS`、`MAX_VCPUS`、`MAX_MEMORY_MIB`、`MAX_SCRATCH_MIB`。按真实durable records计算可用量，包括 `_pool`；例如配置64台却只有32vCPU，1vCPU VM会先触及vCPU配置边界，绝不能写成“物理极限32台”。`host_capacity` 明确归入配置边界；其他boot/transport错误单列。

所有密度结果只给观测下界（`physical_capacity_conclusion=lower_bound_only_no_physical_limit_established`）。碰到配置上限或保守安全余量不是物理容量测量。已有 `real_vm.rs` 的64台/默认32vCPU问题已通知其D-26负责人，不在本分支重叠修改。

运行必须传主Agent分配的 `--resource-window-id`，并独占 `/tmp/agentruntime-fc-real.lock`。不得与TB镜像拉取、镜像构建、其他密度压力交叉。默认至少保留8GiB可用内存和20GiB磁盘空间，逐样本核对；不改生产manager配置、不扩大既有上限、不删除其他资源。检查的manager PID有效环境与配置文件、目标HTTP监听地址必须一致。

## 产物、失败与清理

每个新输出目录包含 `manifest.json`、`requests.jsonl`、`samples.jsonl`、`cleanup.json`、`summary.json`。每次创建前fsync请求journal，响应后追加env_id。每个VM带随机独立owner和 `fc-real/run` 标签；只删除owner和标签同时匹配本次run的VM。对账同时检查API和本地durable records（后者包含启动中的VM）。请求超时不直接重发创建；无法对账的创建保持unknown，清理不能标成功。

`cleanup` 可在中断后按原manifest和请求journal重试；不会删除池、用户工作区或其他运行的VM。删除最多重试3次，失败保留ID和安全错误码，不输出token/HTTP异常中的机密信息。

## 可复现命令

先在主Agent协调好的专用manager实例上安排不同模式，保持规格相同；以下只展示调用，不代表本轮已获资源窗口或执行实测。

```bash
export PYTHONDONTWRITEBYTECODE=1
python scripts/fc_real_measure.py preflight --config-file <该实例env文件> --manager-pid <PID> --mode cold --out <预检目录>
python scripts/fc_real_measure.py run --config-file <该实例env文件> --manager-pid <PID> --mode cold --samples 20 --runtime universal --cpu-millis 2000 --memory-mb 2048 --disk-mb 4096 --runtime-revision <固定提交> --resource-window-id <协调记录ID> --token-file <仅含平台token的私有文件> --out <新的cold目录>
# warm：实例池规格匹配；snapshot：实例关闭池、开启快照缓存，并附 --manager-log <连续manager日志文件>。
# density：实例关闭池和快照，--samples 为本次已分配的最大并发数量；不会自动提高manager配额。
python scripts/fc_real_measure.py report --out <运行目录>
python scripts/fc_real_measure.py cleanup --config-file <原实例env文件> --token-file <私有文件> --out <运行目录>
```

`--token-file` 或环境 `FC_REAL_TOKEN` 只用于请求头，不进入产物。无需模型调用，模型费用为0；宿主资源仍需协调。

## 本轮实际验证

- `/opt/evals/venv-2d94680/bin/python -m pytest tests/deploy/test_fc_real_measure.py -q`，配置worktree的 `PYTHONPATH=services/py-worker/src:libs/py-core/src:proto/gen/python`：**9 passed**。覆盖容量边界、池占用、来源证据、快照预填充、冷回退保留、资源单位、仅本次资源清理、启动中记录、未知创建不伪称清理、真实API序列的离线回归。替身只用于离线控制流程测试，不产生性能数据。
- 只读preflight结果 `/tmp/goal-fc-real-preflight/preflight.json`：`ready=false`；当前沙箱缺 `/dev/kvm`，现有配置 `MAX_VMS=16`、`MAX_VCPUS=32`、内存65536MiB、pool `universal:2000:2048:2`、snapshot cache 0。未启动VM、未改变manager、未跑密度压力。
- `git diff --check` 通过。真实cold/warm/snapshot时延和密度尚未采集；不把工具完成当作FC-REAL验收完成。实际模型费用 **$0**，无本次VM资源需清理。

## 后续静态审查（2026-11-07）

修复测量证据的两个缺口：快照回退现在只写一条带失败原因的原始样本，避免同一 index 重复；创建/首命令失败也收集宿主资源前后值与可取得的 VM cgroup 数据。新增回归固定两类失败：`snapshot_restore_not_observed` 与 `configuration_limit`，仍明确 `complete=false`，但确认清理不误报未知创建。测量工具现 **11 passed**；这些离线用例不产生真实性能结论。D-26 实现、真实环境阻塞及部署条件见 `d26-memory-snapshot-acceptance-2026-11-07.md`。
