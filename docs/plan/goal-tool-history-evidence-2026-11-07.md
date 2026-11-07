# N16-R 已有轨迹的精确工具重复与重试证据

仅补原评测描述指标，不新增Task、模型调用、部署或评分门槛。模块为agentruntime_learning.benchmarks.tool_history_evidence，可在既有隔离Worker内使用其现成Temporal/Claim-Check凭据执行只读采集。

读取每个真实call_tool scheduled输入的ToolCallInput.tool/args，按工作流memo绑定租户后解密Claim-Check，再检查输入租户和Task身份。输出仅包括工具名、完整参数规范JSON的SHA256、workflow/run/event身份及Temporal启动/终态/重试元数据；不输出参数、工具输出、错误消息、凭据或对象签名URL。JSON规范为键排序、紧凑UTF-8、数组和数值类型不变。

- repeated_full_requests数相同完整请求的再次调度；repeated_full_started_requests只计有已启动证据的独立请求。
- 同一个activity的Temporal重试留在同一scheduled记录内，另记reported_started_attempt与重试计数下界，不冒充再次模型决策。
- Temporal可能省略中间重试事件；保存最终started attempt和可见事件，不伪造逐次成功或外部副作用次数。
- 开放历史、未解码引用、缺启动事件、继续运行的新run链均显式不完整。只对schedule-to-start超时或无重试的启动前取消认定not_started。
- 子工作流按实际started run ID读取，并逐个核租户、namespace及parent workflow/run关系；不靠名字前缀假设所有子任务安全。

命令（输出路径应新建，不能覆盖原结果）：

    python -m agentruntime_learning.benchmarks.tool_history_evidence \
      --task-id <真实TaskID> --tenant <实验租户> --out <新证据文件.json>

环境沿Worker的AR_TEMPORAL_ADDRESS、AR_TEMPORAL_NAMESPACE及ObjectStore/SecretStore配置；--out -便于在Worker子进程只返回脱敏JSON。退出0为完整证据，4为不完整或读取失败。不会提交Task、重跑工具或修改Temporal。

零模型费用真实核对：旧模型配置无效样本tsk_01M6TSWW3DPYE33KA2R6SPTJ80在goal-paired-20261107中有2个完整工具请求（repo_list/repo_read）、2个reported started attempts、0重试、0相同完整参数重复，证据完整。这不改变该样本的模型配置无效结论。结果保存在父目录.goal-evidence/tool-history-20261107/original-invalid-task.json。

回归使用真实Temporal History protobuf、真实DataConverter、磁盘ObjectStore及加密Claim-Check，覆盖对象键顺序、不同工具、数组顺序、重试与独立调度区分、参数和失败信息不泄漏、未启动/缺证据、跨租户和父子run错配。没有修改核心工作流或原评测产物。
