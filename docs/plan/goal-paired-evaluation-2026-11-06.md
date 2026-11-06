# EV-TB、N16-R / EV-REAL 执行记录（2026-11-06）

负责人：并发会话的 `paired-eval` worktree，分支 `goal/paired-eval-20261106`。本次只改评测客户端、公共 benchmark runner、Terminal-Bench 与新增配对实验模块；不改核心 Task Loop / Stage Runner / Node Executor、契约或部署。没有新增模型费用，没有启动镜像拉取或真实任务。以下为可执行准备及离线验证，不是三个真实评测任务已经完成。

## 已核对的实物

证据：[goal-paired-baseline-2026-11-06.json](evidence/goal-paired-baseline-2026-11-06.json)，由 `benchmarks.experiment_audit` 只读原始 `/opt/evals/tb2` 产物生成。

- `tb40-a`：20 finished，费用 $22.439347；`tb40-b`：3 finished、4 executed_error、13 not_run，费用 $10.048638。不是“17 题都没跑”。
- 四个已执行但后续失败的 Task：largest-eigenval $1.939309、mailman $0.595482、mteb-leaderboard $2.057617、mteb-retrieve $0.218900，原 task_id 均在证据 JSON。保留其费用和记录，先恢复采集，不盲目重新提交。
- 40 题共 23 finished、4 executed_error、13 not_run；22 个 finished 用上官方环境。按实际 grading 和环境再分：6 个有效解决、14 个模型未解决、7 个环境/判分失败、13 未运行。旧 report 的 7 个“解决”包含一个未用官方环境的 modernize-scientific-stack。两个预算停止但隐藏测试通过的题仍保留解决结论，并独立标记 budget_exhausted。
- 三次单题冒烟加 40 题，原记录合计 **$36.432857**，30 个不同 task_id；已批准该项上限 $80，保守累计余额 **$43.567143**。账本最终结算仍应在运行权限恢复后核对 model_usage，旧记录不是新的账本证明。
- 原 run.json 仅有 `agent-runtime-rev29` 标签和部分配置，没有完整提交、实际模型、Registry 修订、Memory 和评分版本冻结。不能与当前版本补跑的 17 题拼成同配置 40 题。旧结果保留，当前版本须完整跑原来选中的 40 题。

补充交给其它负责人：`/opt/evals/w1/deepeval-b` 已经有 8 题真实非空、未截断工具轨迹，运行费 $0.995772，54 次 Opus 5.5 判分 $0.325152；不是只有无轨迹旧记录。旧 `/opt/evals/t18/deepeval-2` honesty .3875，新轨迹基线 .5875；当前版本仍须按用户要求新目录采集。MX 原 `results.jsonl` 5742 行合计 $120.085294。预算依据总表 §7.1：EV-REAL/N16-R 共用 $40、DeepEval $10、MX $200。

## 实现与行为

- `benchmarks/client.py`：`AR_TASK_API_TOKEN_FILE` 优先，HTTP 与每次 SSE 重连重新读入轮换后的 ops JWT。保留 `AR_TASK_API_TOKEN`。不生成 dev token，不把 Task API 凭据转给 Memory 或 Gateway。发行仍用既有 `/root/ar-release/ops-token.sh`，令牌不进配置、报告或 argv。
- `benchmarks/swebench.py`：任何已有 task_id 的失败记录都跟回原 Task；已完成任务只重取补丁。预算不足不会覆盖旧失败记录或抹掉费用；已提交但账单未知的异常停止后续新提交。影响结果的配置对称校验，不能靠删除 network/provenance 字段绕过续跑保护。
- `benchmarks/terminalbench.py`：冻结题目、环境、隐藏 verifier 和 prepared 文件的摘要（仅摘要进入 run.json，测试内容不发给模型）。支持 `run --provenance <观察所得JSON>`；改变评分文件、准备文件、预算条件或版本必须新目录。报告独立列 not_run、environment_failure/unknown、model_unsolved、budget_exhausted、有效 resolved 及预算停止标记。
- `benchmarks/provenance.py`：代码提交与 Registry 修订是不同字段；Registry 按真实 scope / revision / document_sha256 固定。模型按角色、Memory revision/corpus、Gateway/Worker/租户策略摘要、评分版本固定。历史费用按 task_id 去重，未知账单不当作零，矛盾账单拒绝合并。
- `benchmarks/paired.py`：复用 Task API 与已有持久 runner，每臂/每题独立工作区，公共计划与独占进程锁防重入。每题前后执行只读 `snapshot_command`，只接受两臂除 off/rules 外相同的实际配置。每次尝试读租户绑定的 model_usage，核对服务模型与不可变 Registry；不匹配或有 pending 停后续支出。Temporal 真实工作流树确认子 Agent 节点与运行时间，按节点边界汇总子 Agent 费用，避免 `child` 匹配到 `childish`。已有待采账 Task 阻止另一臂继续支出。
- 配对输出：质量事实清单、拆分准确率、Gateway 总费和子 Agent 费、Temporal 耗时、可见工具数/重复目标数、额外轮次、Jev 否决数及轮次。工具目标只存摘要，不写参数或结果。原始 Task 记录、账本行、Temporal 身份与指标分别保留，可复算。

## 冻结样本、口径与费用

`services/py-learning/evalsets/paired/cases.json` 是固定 10 题：五种三对象只读独立调查 + 对应五种单点查证，标签按 sub-agent.md §3 在运行前固定。数据为明确标注的虚构固定事实，不依赖变化的网页。事实清单版本 `paired-factual-checklist-v1`，检查给出的事实标识，不在看到输出后改标准。预设建议质量增益至少 0.10、可接受模型成本倍数至多 2.0，必须写入实际 plan.json 再开始。

此清单是可复算辅助指标，**不是 DeepEval 语义正确性分，也不足以单独判定完整 N16-R / EV-REAL 验收**。完整结论还需既有语义评分/人工准则复核、规划结构证据及原 requests/httpx/aiohttp 题复测；可以重用这里已采的回答/轨迹，避免重复 Task 费用。公开 SSE 的重复目标只能代表重复请求的近似统计，不能冒充完整工具参数相等。Gateway 账本不含外部 Jev 账单或基础设施费，须另行对账后才声明总费用。

旧 $1.97 的 Task `tsk_01M6Q70KK2A2NRVG2HYXDSN5DD` 只有一次 Jev 误拦；`f06e30c` 只把每 Task 否决限制为一次，不保证消除第一次误拦。因此当前不预断修复会降低这题费用，报告只据新证据判断是否重复否决及额外轮次。

## Terminal-Bench 的具体下一次执行配置

准备文件：`services/py-learning/experiment-plans/tb40-20261106/{tenant-policy-seed.json,execution.json,cases.ids}`。恰好原来的 40 题，保留顺序；隔离租户 `evaltb20261106`，串行一次一题，网络域名沿用已批准的五个，单题目标 $1，总预算 $40。

- **Gateway 硬总准入限制**：新隔离租户 `model_budget.daily_usd=40`、`monthly_usd=40`。
- **Task 软限制**：`budget.usd=1`，runner `--task-usd-limit 1`；Task 内并行工作或一次模型调用可能跨过剩余额，不能把这两项称为每 Task 的硬账本限制。当前 modelbudget.Limits 只有日/月限额，Worker 未填写 Gateway 的 max_cost_usd_micros；本任务不为评测另造 Task 账本功能。
- 新 40 题均用同一预算与冻结配置；旧 $3/题结果单独保存。总额耗尽时停止未开题，保留明确的未运行/预算耗尽状态，不能声称完成 40 题。
- $36.432857 + $40 = $76.432857，未扩大已有 $80 授权。历史每个已启动 Task 均费的 40 题外推约 $48.13 仅是风险提示；执行前须按当前实际加载 Registry 的价格、输入/输出与工具轮次重新估计。
- 这里的 policy 候选复制自实物 `swebench-policy-seed-v5.json`，**并未证明它等于当前线上策略**；execution.json 明确 `policy_source_observed=false`。部署负责人先导出当前批准的策略文档，用下面的准备器再生成，仅改隔离 tenant、Task $1 与日/月 $40，不沿用过期模型白名单、扩大配额或覆盖生产租户。`prepare_terminalbench` 不发布、不改集群。

准备器命令（源策略应是刚读回的线上 document 或单条 seed）：

```bash
python -m agentruntime_learning.benchmarks.experiment_audit \
  --run /opt/evals/tb2/tb40-a --run /opt/evals/tb2/tb40-b \
  --charged-run /opt/evals/tb2/one --charged-run /opt/evals/tb2/one-b \
  --charged-run /opt/evals/tb2/one-c --charged-run /opt/evals/tb2/tb40-a --charged-run /opt/evals/tb2/tb40-b \
  --source-policy current-approved-policy.json --execution-out tb40-frozen \
  --out tb40-frozen/historical-audit.json
```

部署负责人按既有 registry-seed 流程发布新租户策略，保存回退/发布结果，再核对 Gateway 与 task-service 加载了同一实际修订、40 美元限制生效。Task API 无单 Task 的模型 pin 或 sub_agent_policy 字段，off/rules 需要两套隔离且同配置的评测环境，或受控顺序切换并采集前后快照；不能只改报告标签。

## 配对 runner 的执行接口

`python -m agentruntime_learning.benchmarks.paired estimate --plan <plan.json>`，之后 `run --plan <plan.json> --out <新目录> --arm off`、同目录 `--arm rules`，最后 `report --out <目录>`。没有默认付费运行。

plan 必填：`run_id`、`seed`、`tenant_id`、`budget_usd`（不得超过 40）、`task_usd_limit`、`thresholds.minimum_quality_gain`、`thresholds.max_cost_ratio`、`cases`（直接读上述固定 JSON）、`arms.off/rules`。每个 arm 包含 Task API 地址、只读 argv 数组 `snapshot_command`、真实观察的 `provenance`。命令使用 subprocess argv、不开 shell；快照失败不打印可能含凭据的 stderr。provenance 必填字段及严格校验见 `provenance.validate`；不能拿 Git SHA 代替 Registry revision。`historical_runs` 可列本项之前付费记录目录，去重扣减 $40 额度。

运行账户还需 `AR_RELATIONAL_DSN`（非超级用户，代码经 TenantContext/RLS 绑定，只读账本）、`AR_TEMPORAL_ADDRESS`、`AR_TEMPORAL_NAMESPACE`；ops JWT 只用于 Task API。实际运行前后观察快照相同才能计为有效；一次调用用了其它角色的模型，即使该 Profile 也在合法 Registry 中，也按污染拒绝。

## 验证与未完成项

已运行当前 worktree 的 py-learning、py-worker、py-core 和生成代码路径，针对四个测试文件完成 93 条回归（命令与最终结果见提交说明）。原有 `test_benchmark_client.py` 另外实际尝试，8 条在 fixture 创建 AF_INET socket 时全部 `PermissionError: [Errno 1] Operation not permitted`，没有 skip、删除或修改有效断言。待恢复 socket 权限后原套件原样重跑。

真实运行、部署、当前价格估算、R2/Temporal/账本线上核对未执行。本阶段新增实际模型费用 **$0**。没有覆盖真实网络验收的离线测试不算线上成功；三个任务继续保持未完成。
