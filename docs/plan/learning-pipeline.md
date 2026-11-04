# Learning Pipeline 最小闭环（中档工作项细化，TMP 计划 T-16）

> 实施计划第 7 节阶段 6 的中档工作项“Learning Pipeline”（中 · 中），强档评审 R-06。
> 依据：设计书 3.5、3.5.1、附录 J（J.1–J.5）、H.5、实施计划 S-12 的“跨租户读取”；`docs/plan/registry.md`（Publish、Promote 与评测证据）；
> `docs/plan/deploy-telemetry.md`（T-15：trace 的 span 与 Collector）。
> 状态：已实现（2026-10-23）。第 4 节的决定与第 5 节的验收测试经人确认；实现记录见第 6 节。
> 2026-10-28：集群里的 Job 直接消费 trace 主题（第 7 节；新依赖 confluent-kafka 待人确认）。

## 1. 目标

TMP 计划的完成标准：从 trace 里挑一个任务族，产出一个候选 Recipe；跑评测；过门槛才经 Registry 发布。后台任务单独记预算，运行中的 Task 不受影响。
演示一次“候选未过门槛，发布被拒”，一次“过门槛，发布成功”。

R-06 的评审重点：未过评测不能发布；后台任务单独记预算；运行中的 Task 不受影响；失败归因提示。

## 2. 第一期范围

一条命令跑一遍（`python -m agentruntime_learning.pipeline run --tenant <租户> --family <任务族> --traces <文件>…`），按顺序五步。
退出码：0 跑完（已发布、发布被拒、没有候选、dry-run 都算）；1 Registry 写失败（含 Promote 被拒）；2 参数或环境不对（例如不是 dry-run 却没有 `AR_REGISTRY_ADDRESS`）；
3 读主题或提交消费组的位置失败（第 7 节）。

1. **读 trace**（J.1）：输入是 T-15 定下的 span：Task 级 `invoke_workflow` 与它下面的 `runtime.round`。两种格式，解析成同一个模型：
   - `otlp_json`：Collector 的 kafka 导出器（与 `file` 导出器）写的 OTLP JSON，每行一个 `ExportTraceServiceRequest`；
   - `console`：控制台 Telemetry 写到标准错误的 JSON 行（本地开发）。
   从文件读（`--traces`，可以给多个），或在集群里直接消费 Collector 写的主题（`--topic`、`--group`，见第 7 节）。
   归并成 `TaskTrace`：租户、task_id、任务族、结局、用过的 Recipe、成本、各 Round（verdict、triage_level、成本、Stage 形状）。
   同一个 task_id 出现多次（至少一次投递）只算一次；缺关键属性的 Task 跳过并计数。
2. **按租户过滤并留审计**：trace 里混着所有租户（Collector 不分租户）。一次运行只处理 `--tenant` 指定的租户，按 span 上的 `tenant_id`
   （由 Telemetry 绑定时加上，调用方伪造不了）过滤；其他租户的 span 只计数、不进入任何后续步骤。每次读取在平台作用域的 Telemetry 记一条审计日志
   （`learning.read_traces`：租户、文件、读到的 span 数、跳过的其他租户 span 数），不复用在线服务的身份（S-12 的“跨租户读取”）。
3. **归纳候选 Recipe**（J.4“晋升：某任务族的做法连续验证成功后，固化为 Recipe”）：
   - 只看该任务族、没有用 Recipe（完整规划）的 Task。每个 Task 的“做法签名”是它经 Planner 规划的各 Round（有 triage_level 的 Round；原地重跑的不算）的
     Stage 形状，只取结构：Node 的 id、kind、template、after、tier、mode。
   - 取成功（done）次数最多的签名，成功次数不少于 `--min-support`（默认 5）才产出候选。并列时取签名的字典序最小的（确定）。
   - 候选 Recipe 的正文：stages 取签名对应的 Stage 形状（goal、exit_when、Node；Goal Node 带 goal、done_when、mode、output_schema），
     description 按模板生成，`typical_cost_usd` 取成功 Task 成本的中位数，`risk` 取各 atomic 节点模板 effect 的最大值（模板的 effect 从 Registry 按版本查），
     有 Goal Node 时按 write_irreversible 算（见第 4 节第 3 条）。名字 `learned_<任务族，点换下划线>`，租户作用域。
   - 发布前按 `recipe.schema.json` 校验；不合格就不产出候选（写进报告）。
4. **评测与门槛**（J.3 的非劣效检验，见第 4 节第 1 条的出入）：
   - 候选组：签名等于候选签名的全部 Task（成功与失败都算），加上签名是候选签名的严格前缀、且没有成功的 Task（走这条路中途失败的）。
     只算成功的会有选择偏差（成功率恒为 100%）。
   - 现行组：该任务族当前的默认 Recipe（`ListRecipes` 按任务族）有就取用它的 Task；没有就取该任务族全部完整规划的 Task。
   - 成功率用 Wilson 95% 置信区间；门槛：候选的下界 ≥ 现行的成功率 − δ。每次成功成本：候选的 95% 上界（均值 + 1.96 × 标准误）≤ 现行的均值 × (1 + ε)。
     默认 δ = 0.02、ε = 0.05、每组至少 100 条（J.3），都可以用参数改。样本不够直接不通过。
   - 结果写成 `registry_evaluation.schema.json` 的评测证据（eval_set 名为 `traces.<任务族>`，版本是参与评测的 task_id 集合的哈希），完整报告写进 `--report-dir`。
5. **发布**（J.5）：只有门槛通过才调 Registry：`Publish`（租户作用域、影子）后带证据 `Promote`。门槛不通过时**不调用 Registry 的任何写方法**，
   报告写明“发布被拒”与原因。`Promote` 被 Registry 拒绝（证据不合格等）时报告失败，影子版本留在 Registry 里，默认版本不变。
   `--dry-run` 只出报告、不写 Registry。运行中的 Task 不受影响由 Registry 保证（固定版本，T-08 已测），Learning 只经这两个方法写。

另外两件：

- **失败归因**（J.2，R-06 的“失败归因提示”）：对候选组与现行组里没做成的 Task，按 MAST 三类归因：`specification`（规格问题）、`coordination`（协作失配）、
  `verification`（验证缺失）。用小模型（`small@low`）经 Model Gateway 判断，输入只有结局、各 Round 的 verdict 与 Stage 形状（trace 里本来就没有用户数据）；
  输出按 schema 校验，不合格记 `unattributed`。没配 Gateway 或预算用完时一律 `unattributed`。第一期归因只进报告，不反哺路由与模板（见“不做”）。
- **单独记预算**（3.5.1 关键规则）：一次运行一个预算（`--budget-usd`，默认 0.5 美元），只算 Learning 自己的模型调用（按 Gateway 返回的 `cost_usd_micros`）；
  每次调用的 `max_cost_usd_micros` 不超过剩余预算；用完就停止调用模型。花费记在平台作用域的指标 `learning.cost_usd` 与报告里，
  模型调用的 `CallContext` 不带 task_id，不计入任何 Task 的成本。

**不做（登记到后续）**

- EventStream 的 Kafka 适配器（按租户绑定的 append 与消费）：Learning 读的是 Collector 写的、不带作用域头的主题，直接用 Kafka 客户端（第 7 节）。
- trace 原文存进 ObjectStore（3.5）：现在历史只到主题的保留期（第 7 节）。
- offline 队列上的 Temporal 工作流（3.5“作为 offline 队列上的工作流运行”）：第一期是一次性命令，失败重跑整条即可（读 trace、算门槛都是确定的，发布按门槛结果幂等）。
- 影子流量的分流与对比（J.3“影子评测”）：第一期是观测式评测（见第 4 节第 1 条）。
- 灰度放量与指标变差自动回滚（J.3 末句）。
- 归因结果反哺路由规则、Model Profile 阈值、Node Template（J.2 后半句）；Recipe 统计（J.4 第 1 条，Planner 的 recent_failure 仍是 false）；
  Recipe 的离线搜索与 Reasoning Template（J.4 第 3、4 条）；Model Profile 内部分数（H.5）。
- 平台作用域的 Recipe（跨租户归纳）：要先定跨租户聚合的隐私规则。

## 3. 难点与防错

- **未过门槛不能发布**：门槛的判定与发布在同一个函数里按顺序做，没有“先发布再说”的路径；测试用记录调用的 Registry 断言：不通过时写方法一次都没调用。
  Registry 自己也拒绝 `passed=false` 的证据（T-08），这是第二道。
- **选择偏差**：候选组必须含失败的 Task（见第 2 节第 4 步）；测试构造“成功的都走这条路、失败的也大多走这条路”的数据，断言不会因为只看成功而通过。
- **样本不足不能通过**：少于最小样本数一律不通过，原因写明；不因为两组都很小而“差异不显著”就放行。
- **租户隔离**：别的租户的 span 不影响候选、评测与归因（测试：把另一个租户的大量 trace 混进去，结果逐字段不变）；发布一律在 `--tenant` 的作用域；
  审计日志每次运行都记。
- **trace 不带用户数据**：Stage 形状本来就不含 inputs 与 rationale（T-15）；归因提示只用形状与 verdict，测试断言发给模型的消息里没有 trace 之外的内容。
- **预算**：预算用完后不再调用模型（测试：预算只够一次调用，第二个失败 Task 不再发请求）；`max_cost_usd_micros` 不超过剩余预算；花费不进 Task 的指标。
- **确定性**：同一批 trace 跑两次，候选正文、证据（除 evaluated_at 与 report）完全相同；读 trace 的顺序不影响结果。
- **至少一次投递**：重复的 span（同一 task_id 的 `invoke_workflow` 出现多次）只算一次。

## 4. 待人确认的决定

1. **观测式评测代替影子评测**：J.3 说新 Recipe 先在影子流量上运行再对比；影子分流还没有（T-08 登记到后续）。第一期用历史 trace 比较
   “按这种做法走的 Task”与“现行做法的 Task”。两组有重叠（候选组是完整规划的子集），且不是随机分配，结论比影子评测弱。证据的 eval_set 名写明 `traces.` 前缀，
   以后影子评测的证据用别的名字，能区分。
2. **门槛只用 J.3 的两条**：成功率下界与每次成功成本上界。不加 Round 数、升级率（J.4 的统计项）作为门槛。
3. **Goal Node 的 risk 按 write_irreversible 算**：tool_loop 一类的 Goal Node 做什么事要到运行时才知道，Recipe 的 risk 用来和 Session 已授权的风险比较（C.1），
   宁可高估。代价是含 Goal Node 的学得 Recipe 在风险授权低的 Session 里命中不了。
4. **Recipe 的文字取自 Planner 的规划**：Stage 的 goal、exit_when 与 Goal Node 的 goal、done_when 是 Planner 写的，可能带具体业务名词（例如客户名）。
   Recipe 是租户作用域，只有同一租户看得到；不做脱敏。要更严，可以在发布前过 Memory Service 的 PII 脱敏（另排期）。
5. **Learning 的身份**：调 Registry 与 Gateway 时用 `--tenant` 的租户上下文，user_id 固定为 `learning-pipeline`；发布令牌与 Worker 无关，从 SecretStore 的平台作用域取
   （同 T-08 的 `registry/publish-token`）。生产上应有自己的令牌与角色（S-12），第一期共用发布令牌。

## 5. 验收（测试已写，待人确认）

| 验收 | 测试 |
| --- | --- |
| 读 trace：OTLP JSON 与控制台两种格式解析成同样的 TaskTrace；Round 按 round_seq 排序并挂到所属的 Task；重复投递只算一次；缺属性的跳过并计数；格式错误的行报错带行号；真的 WrapUp 写出的控制台记录能读回（T-15 的 span 与这里的解析是同一个契约） | `services/py-learning/tests/test_pipeline_traces.py` |
| 租户：只取 `--tenant` 的 span，其他租户只计数；混入别的租户的 trace 结果不变 | 同上、`test_pipeline_run.py` |
| 候选：签名只看结构；只看该任务族、没用 Recipe 的 Task；成功次数最多的签名、不足 min_support 不产出、并列时确定；原地重跑的 Round 不进签名；正文合 `recipe.schema.json`；risk 按模板 effect 与 Goal Node 规则；typical_cost 是中位数；名字与租户作用域 | `test_pipeline_candidates.py` |
| 评测：Wilson 区间的数值；候选组含失败与中途失败的 Task（选择偏差）；现行组按有没有默认 Recipe 取；两条门槛各自能判不通过；样本不足不通过；证据合 `registry_evaluation.schema.json`；结果确定 | `test_pipeline_evaluation.py` |
| 归因与预算：三类加 unattributed；模型输出不合 schema 记 unattributed；调用用 small@low、带租户、不带 task_id；消息里只有结局、verdict 与形状；`max_cost_usd_micros` 不超过剩余预算；预算用完不再调用；没有 Gateway 时全是 unattributed；花费记在平台作用域的 `learning.cost_usd`，不出现 Task 的指标 | `test_pipeline_attribution.py` |
| 闭环：未过门槛时报告“发布被拒”、Registry 写方法零调用；过门槛时 Publish（租户作用域、RECIPE、影子）后带证据 Promote，证据与报告一致；Promote 被拒时报告失败；没有候选时不写；dry-run 不写；审计日志；同一批 trace 跑两次结果相同；命令行参数与退出码 | `test_pipeline_run.py` |
| 演示（端到端）：真的 task-service Registry；一个任务族样本不够或成功率差，发布被拒、Registry 里没有它的 Recipe；另一个任务族过门槛，Recipe 成为该租户的默认版本；另一个租户看不到它（命令行最后一行输出运行摘要的 JSON：status、recipe、reasons、报告路径） | `tests/e2e/test_learning_pipeline.py` |

## 6. 实现记录（2026-10-23）

代码在 `services/py-learning/src/agentruntime_learning/pipeline`：`traces`（读 trace 与按租户归并）、`candidates`（签名与候选 Recipe）、
`evaluation`（两组、Wilson 区间、非劣效门槛、评测证据）、`budget`、`attribution`、`run`（一次运行）、`__main__`（命令行）。
trace 来自 T-15：Worker 收尾写回打的 `invoke_workflow` 与 `runtime.round`（`agentruntime_worker.reviewer.wrap_up.trace_task`），
Round 的 Stage 形状是 `agentruntime_worker.workflows.verdicts.stage_shape`；Task 工作流把任务族（Task Plan 的 `triage.task_family`）
交给收尾写回（只改 activity 的输入，不改命令序列）。

**细则**

- 同一个 task_id 的 `invoke_workflow` 有多份时取 span_id 最小的那份（收尾写回被重试会再打一份，span_id 不同）；它下面的 Round 按 round_seq 去重。
- 归因只看 aborted 与 failed 的 Task（取消的不是这次做法的失败）；报告里列每个 Task 的类别与依据，另有按类别的计数。
- `--dry-run` 且没有 `AR_REGISTRY_ADDRESS` 时，模板一律按 write_irreversible 估 risk（只影响报告里的候选正文）；有地址时只读 Registry。
- 报告的文件名是 `<任务族>-<时间>.json`，同时写进评测证据的 report；同一批 trace、同一时刻跑两次，候选正文与证据逐字相同。
- 审计日志 `learning.read_traces` 与指标 `learning.cost_usd` 在平台作用域（`bind_platform("learning")`），不带租户属性；读的是哪个租户写在 `learning.tenant` 里。

**端到端演示**（`tests/e2e/test_learning_pipeline.py`，2026-10-23 通过）：真的 task-service Registry 上先跑一个成功率不如现行做法的任务族
（发布被拒，Registry 里没有它的 Recipe），再跑一个过门槛的（Publish、Promote 之后成为 t1 的默认 Recipe，t2 看不到）。演示只用 Registry 的
fixture 已经种下的模板：端到端环境整个会话共用，另种模板会进入别的用例里新 Task 的固定版本。

**没验证的**：真模型的归因没跑过（供应商额度，见 T-03）；R-06 评审没做（要另一家的强模型或人，写审分离）。

## 7. 云上的输入（2026-10-28）

第一次上线时 ScaledJob 按 Redpanda 的积压起 Job，Job 却读 `tracesClaim` 卷里的文件；卷没配，回退成 emptyDir，每半分钟起一个失败的 Job，
线上先关了（`cloud-deploy.md` 的“第一次上线”）。两条路：(a) Job 直接消费 `telemetry.spans`，用的就是 KEDA 盯的消费组，跑完才提交；
(b) Collector 另用 file 导出器写到共用的卷，Job 挂这个卷。**选 (a)**：

- 设计书 3.5 写的就是“消费者从 Redpanda 读取 trace 事件……由 KEDA 按 Redpanda 消费延迟与队列积压扩缩”，3.5.1 是“trace 经 EventStream 到达”，
  4.13 的第三路导出写明“供 Learning Pipeline 消费”。(b) 要给 Collector 加设计里没有的第四路导出。
- (b) 与 KEDA 的触发器合不上：没人提交消费组的位置，延迟只增不减，Job 一轮接一轮地起，每轮重算全部；要么改成定时触发，离开 3.5。
- (b) 的卷要 Collector 与 Job 共用：只有单节点的 local-path 行，多节点要 RWX；Collector 多副本时几个进程写同一个文件；文件只增不减，轮转又一样丢历史。
- (a) 的代价：新依赖 Kafka 客户端（confluent-kafka，librdkafka 的官方 Python 客户端，wheel 自带 librdkafka；待人确认）；py-learning 的测试要 Docker 里的 Redpanda。

**做法**（`agentruntime_learning.pipeline.stream`；命令行 `--topic <主题> --group <消费组>`，broker 取 `AR_EVENT_STREAM_BROKERS`）：

1. 快照：各分区当前最早保留的位置与末尾，从最早保留的读到末尾，按（分区，偏移）排好，一条记录一行交给第 2 节第 1 步。
   **每次都从头读**：J.3 要每组至少 100 条样本，只读上次之后的新记录凑不够；重读的 Task 由第 2 节的去重处理（与至少一次投递同一条路）。
2. 跑完（退出码 0：已发布、发布被拒、没有候选）才把快照的末尾提交为消费组的位置。Registry 写失败（1）、坏记录（2）、读不到主题或提交失败（3）
   一律不提交：KEDA 看到的延迟不变，下一轮整条重跑（第 2 节“不做”里的“失败重跑整条即可”）。发布被拒也提交：同一批 trace 再跑结论相同。
   dry-run 不提交。快照之后才到的记录不提交，留作下一轮的延迟。
3. 消费组的位置因此只表示“评测到了哪里”，KEDA 的延迟就是上次评测成功之后新到的记录。Job 不加入消费组（assign，不 subscribe），提交时用组名。
4. 主题里混着所有租户，记录上没有 EventStream 的作用域头，所以不经按租户绑定的 EventStream 接口读（4.6 的接口注释：跨租户读取由 S-12 另行提供）。
   按租户过滤与审计不变；审计 `learning.read_traces` 的 `learning.files` 写“主题#分区:起-止”。坏记录的报错写出分区与偏移。

**Chart**（`templates/learning.yaml`）：

- Job 的参数是 `--topic`、`--group`，与触发器的主题、消费组取自同一个值；`AR_EVENT_STREAM_BROKERS` 与触发器的 bootstrapServers 都是 `external.redpanda.brokers`。
  不再挂 trace 的卷；`learning.tracesClaim`、`learning.tracesFile` 设了就渲染失败，不被悄悄忽略。
- 触发器 `offsetResetPolicy: earliest`：没提交过时延迟按全部记录算。默认的 latest 只算 1，`activationLagThreshold` 永远达不到，Job 起不来，也就永远不提交。
- `activationLagThreshold`（默认 100 条记录；Collector 每批 span 写一条）：攒够新记录再起 Job，不是来一条就重算全部历史。
- `pollingInterval`（默认 600 秒）：也是失败后重跑的间隔，不再是 KEDA 默认的 30 秒。

**验收**：`services/py-learning/tests/test_pipeline_stream.py`。对着 Docker 里的 Redpanda（`redpanda_support`，与集群同版 v26.2.4）：读全部历史、提交末尾、延迟归零；
下一轮仍看到早先的 trace；失败与坏记录不提交、修好后下一轮跑通；快照之后到的记录留作延迟；dry-run 不动消费组；主题还不存在时当作空。
命令行的提交规则另用假的读者测（各退出码、读与提交出错、审计）。部署：`tests/deploy/test_helm_chart.py`（三套 values：Job 读的就是触发器盯的主题与消费组、
earliest、激活阈值、轮询间隔、旧设置被拒）、`test_cloud.py`（云上的 Job 消费的就是 Collector 的 kafka 导出器写的主题与 broker）。

**与设计书不一致、没做的**

- 历史只到主题的保留期（主题是自动建的，用 Redpanda 的默认保留期，7 天）。设计书 3.5 是“评测集与 trace 原文存于 ObjectStore”：要更长的历史，
  Job 把本租户的 Task 与 Round span 存进 ObjectStore、只读上次之后的新记录。登记到后续。
- 每次读全部保留的记录，Job 的内存随主题的大小涨（限 1 GiB）；量大了同样要上面的存档。
- 主题里一条坏记录会让之后的每一轮都失败，直到它过了保留期。Collector 写的是合法的 OTLP JSON，出现坏记录就是配置错了，宁可停下报错。
- 一个 ScaledJob 只学一个租户的一个任务族；多个任务族要多个 ScaledJob、各用各的消费组，Chart 还不支持列表。
- 真集群里没跑过（KEDA 读这个消费组的延迟、Job 连 `redpanda.data.svc:9093`），线上重新打开时看。

## 8. 成本读 Gateway 的账本（R-06-D，2026-11-04）

[代审记录](delegated-review-2026-11-04.md)第 7 节第 1 条定了权威存储是业务库的 `model_usage`，Gateway 结算、Learning 读同一事实源；第 5 条定了含
pending、unknown 成本的比较不自动晋级（门槛在 R-06-B 已改）。原来 Task 的成本取 trace 上的 `runtime.cost_usd`：Worker 收尾时记的估计，
Gateway 结算的实际金额与它不一定一致，用量没回来的调用也看不出来。

**做法**（`agentruntime_learning.pipeline.costs`）：

- 按读到的 Task 的 task_id 查 `model_usage`（每次 provider attempt 一行，Go 一侧 `libs/go-core/modelbudget` 的迁移 301、302；Python 只读，不拥有迁移），
  每个 Task 的成本是全部尝试的实际金额之和，失败的尝试也算。
- 有一次尝试还没结算（实际金额为空：用量未知，网关留着预留），或账本里没有这个 Task 的记录，这个 Task 算成本未知：按 0 填，`cost_known` 为假，
  门槛据此交人工判断。没有 task_id 的调用（Learning 自己的失败归因）不归任何 Task。
- 租户由 RelationalStore 的绑定决定：开发时是租户自己的 SQLite 库文件（`<AR_DATA_DIR>/relational/<租户>.sqlite3`，与 Gateway 同一个），集群里是
  Postgres 的行级安全；SQL 里没有租户条件。task_id 每 500 个一批查。
- 读不了账本（表不存在、连不上库）时整次运行失败，退出码 4，不提交消费组的位置，下一轮重跑；不退回 trace 上的成本。错误里不带连接串与库的位置。
- 命令行 `--cost-source`：`auto`（默认）在配了 `AR_RELATIONAL_DSN` 或 `AR_DATA_DIR` 时读账本，否则读 trace；`model_usage` 要求配了业务库；`trace` 照旧。
  报告记 `cost_source` 与成本未知的 Task 数 `cost_unknown`，审计 `learning.read_traces` 也带这两项。
- Chart：Learning 的 Job 拿到与 Model Gateway 同一个 `secrets.relationalDsn`（`templates/learning.yaml`），云上因此读账本。

**验收**：`services/py-learning/tests/test_pipeline_costs.py`（读账本的四条在 SQLite 与临时的 Postgres 上各跑一遍，Postgres 用非超级用户、不带
BYPASSRLS 的角色：合计、未结算与没有记录算未知、没有 task_id 的不算、只读绑定的租户（第三个租户什么都看不到）、分批、表不存在报错；
门槛用账本的成本、一次未结算交人工；命令行的 auto、trace、model_usage 与退出码 4）；`tests/deploy/test_cloud.py` 的
`test_cloud_learning_reads_costs_from_the_gateways_ledger`。

**没做的**

- 线上 Job 的连接串没在集群里试过（随下一版部署）。
- Task State 里只放引用或摘要（第 1 条的后半句）没有改：Task State 现在不带成本。
- Registry 修订号进账本（第 2 条）仍没做：Gateway 手上没有修订号。
