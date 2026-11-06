# B 的 Firecracker 真实验收（2026-11-07）

状态：真实快照正确性与四组测量完成；不代表生产部署或20项总Goal完成。A统一集成与部署。

固定Rust源码 `5e1037e509d161bf3746d52a49b811656a8fa0a8`，Firecracker 1.17.0，Python VM 1 vCPU / 256 MiB RAM / 256 MiB scratch。同一内核、rootfs、静态manager与agent，二进制和镜像SHA见各组manifest。宿主24逻辑CPU、约125GiB内存，既有平台服务继续运行，测量窗口无其他构建、TB拉取或压力测试。

| 模式 | 样本 | 首命令 p50 ms | 首命令 p95 ms | 实际启动来源 |
| --- | ---: | ---: | ---: | --- |
| cold | 20 | 933.00 | 1169.41 | cold |
| warm | 20 | 19.68 | 47.39 | warm |
| snapshot | 20 | 9829.78 | 10743.94 | memory-snapshot |
| density | 96 | 1050.31 | 1315.69 | cold |

预热池恢复正常；快照确实恢复内存，但本宿主上完整SHA校验使其仍慢于冷启动。只移除重复输入哈希，未降低完整性验证，不发布正向加速结论。默认快照配置仍为0，按工作负载收益决定启用。

密度是在96台同时存在时成功执行首命令，负载为idle VM，并非编译负载。96是本次配置/样本上限，只报告至少96台的观测下界，不能称物理极限。

每个样本保留cpu.stat、memory.current/peak/events、io.stat，以及宿主CPU/内存/磁盘IO/PSI前后值。第一轮因缺IO控制器且warm误回退cold被保留为诊断，未混进最终数据。snapshot缓存填充单独记录，不计入20个恢复样本。

正确性：真实memory/state创建、无内核重启的恢复、新machine-id/boot-id/hostname/随机数/时钟、租户磁盘隔离和权限、篡改快照冷回退通过（优化后29.23s）。Rust34单元、21宿主验收及clippy全目标通过。

已修：API socket路径上限遗漏；VM IO控制器未启用；同一输入重复哈希；硬链接ctime变化错误清空预热池。提交依次e4c662b、7749651、4404b1d、d983238。

证据目录 `/root/proj/agent-projs/.goal-tools/fc-final-measure-20261107`：manifest、请求journal、原始samples、summary、cleanup、manager日志/配置、aggregate和SHA256清单。脚本 `.goal-tools/goal_fc_final_run.py` 调用仓库 `scripts/fc_real_measure.py`，使用独立manager实例和全局资源锁。旧诊断目录 `fc-measure-20261107`、`fc-measure-shortpath-20261107` 保留。

所有本次VM已销毁并sweep，持久VM记录0，临时token/secrets删除。模型费用0美元。证据生成后清理本次缓存工作目录gm8；生产manager和VM未修改。
