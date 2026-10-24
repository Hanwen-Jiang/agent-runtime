# 风险与开放问题

> 来源：`主文档` 第十一、十二节，`精简版` 第 8 节；另汇总了设计书 1.5 中标为“待定”的指标，以及[强档实施计划](../plan/strong-tier-plan.md)中需要人确认的事项。
> 用途：开工前和每个阶段结束时过一遍，把已经决定的项写回设计书或计划。

## 1. 主要风险

最大的两个风险：

- 记忆被污染后“自信地犯错”。
- 路由与压缩出错时难以归因。

两者都靠完整的 trace 与离线评测兜底。

| 风险 | 表现 | 缓解 | 落在哪个工作项 |
| --- | --- | --- | --- |
| 公开榜与自家任务分布不一致 | 按榜单选的模型在线上表现平平 | 公开分只做先验，以内部任务族评测为准 | S-08、R-02 |
| 记忆投毒 | 外部内容诱导写入恶意经验，再被其他成员检索到 | 来源分级、外部来源不可晋升、指令式条目人审、版本回滚 | S-13 |
| 经验过时 | 旧经验在工具或模型升级后误导 agent | 有效期、用后反馈、升级时批量标记待复核 | Memory Service 主体 |
| 压缩丢关键信息 | 压缩后重读文件、重做已完成的步骤 | 精确状态独立跟踪、压缩后做 Probe 自检 | S-11、R-03 |
| 路由错误难归因 | 分不清是路由选错还是模型答错 | 记录每次路由决策与置信度；影子评测对照 | R-02、Learning Pipeline |
| 缓存与灵活性冲突 | 动态工具集、动态偏好让前缀频繁失效 | 工具集固定，改用屏蔽；偏好以追加形式变更 | R-03 |
| 后台成本失控 | 反思、整理、Probe 本身消耗大量 token | 后台任务单独记预算，用小模型，按价值抽样 | R-06 |
| 工作流不确定性 | 改代码后旧的事件历史无法重放，运行中的 Task 卡死 | 版本分支或 Worker Versioning；重放测试进入 CI | S-03、S-04 |
| 写操作重复生效 | Worker 崩溃或超时后重试，外部动作执行两次 | 幂等键、UNKNOWN 对账路径、故障注入测试 | S-05、S-07 |
| 跨租户泄露 | RLS 与连接池配合不当，或检索漏带租户过滤 | 服务端拼接过滤、越权测试进入 CI | S-12 |

后三条是实施阶段新增的风险，调研文档里没有列出。

## 2. 需要团队决定的开放问题

### 2.1 来自调研文档

- [ ] Workspace 级经验晋升是否必须人工确认，还是达到验证次数即自动生效。
- [ ] 是否允许去标识化后的通用经验跨租户共享，默认开还是关。
- [ ] 用户能否看到 Workspace 经验的来源成员。
- [ ] 各模型厂商的条款是否允许用其输出训练自有路由器。
- [ ] Triage 的成本上限按 Task 预计成本的比例设，还是按租户套餐设。

### 2.2 设计书 1.5 中待定的容量指标

推算方法见设计书附录 M.3。

- [ ] 并发 Task 数（由状态吞吐反推）
- [ ] Task 提交速率（由状态吞吐反推）
- [ ] 在线 SSE 连接数
- [ ] 模型调用峰值：每秒请求数与每分钟 token 数，受供应商配额约束
- [ ] 交互式 Task 的首 token 延迟 P95
- [ ] 恢复时间：从 Worker 故障到 Task 继续执行，取决于 activity 的超时设置

### 2.3 实施计划中需要人确认的事项

- [x] 共享库目录 `/libs/py-core`、`/libs/go-core`：设计书 4.15 没有列出（S-02）。已确认建立。
- [x] 租户上下文的传递方式：每个方法的第一个参数，还是绑定在按请求创建的客户端上（S-02）。已确认绑定在按请求创建的客户端上。
- [ ] 评测门槛：Triage 准确率、Verdict 准确率的起点值（S-09、S-10 给出的是建议值）。
- [ ] Claim-Check 的阈值：沿用技术选择资料的 64 KB，还是按实测调整（S-06）。S-06 先默认 64 KiB，两个服务都可经 `AR_CLAIM_CHECK_THRESHOLD_BYTES` 调整；实测后再定。

## 3. 初步完成之后仍未做的事

TMP 计划（初步完成整个项目，2026-10-21 至 2026-10-24）收尾时，从各工作项的“登记到后续”与 R-01 到 R-06 评审（含另一家复审，
`docs/plan/reviews/`）里汇总过来。按模块分组，每条注明出处；做的时候先读出处。

### 3.1 决策质量（S-09、S-10 的门槛没达到）

- [ ] Triage 准确率 85.7%（门槛 90%）：区分“改标题一类的续改”与“有 Recipe 的新任务”；fast 规则在命中 Recipe 时让位给 light（TMP T-03 差距清单第 1 条）。
- [ ] 规划成本占 Task 预计成本最大 30.9%（门槛 5%）：Planner 提示瘦身、提示缓存、strong@medium 换模型，或请人重定这条对小 Task 的口径（T-03 第 2 条）。
- [ ] Reviewer 样本 Verdict 准确率 75%（留出集）/ 50%（开发集），门槛 85%；该升级却原地重试中位数 1 次（门槛 0）：附录 B.7 的症状写进规则（T-03 第 3、4 条）。
- [ ] 2.3 的“评测门槛起点值”仍待人定。

### 3.2 Model Gateway 完整版（R-02 评审第 4 节、另一家复审）

- [ ] 按每次成功的期望成本选档（H.2）；多供应商切换；按供应商的并发池与 Valkey 限流。
- [ ] 执行 `max_cost_usd_micros` 与租户预算上限；数据驻留与 SLA 档位；BYOK 作为选档约束。
- [ ] 按 Task 固定 Model Profile 版本与主模型（要 Task State 配合，核心链路）；升降级（H.4）；网关只剩同一家时的同源评判（只记录）。
- [ ] 计量与模型调用 span 进 Langfuse；上游错误说明脱敏；结构化输出在网关侧按 schema 校验或写明由调用方校验的契约；activity 重试时的请求级去重。
- [ ] 服务间认证（部署层 mTLS）：现在网关信任调用方给的租户。
- [ ] Anthropic 的提示缓存没有生效：T-13 里 Anthropic 模型的 cache_read 一律为 0，单独探测确认现用的 OpenAI 兼容转发服务丢掉了 `cache_control`
      （OpenAI 模型靠自动前缀缓存有命中）。要加原生 Messages 接口的适配器，或换支持透传的供应商；之后再定 history 的滚动断点（R-03 评审第 4 节，`docs/plan/live-e2e.md` 第 6 节）。
- [ ] 转发服务把请求校验错误当成 HTTP 200 的“System Notice”正文返回，网关现在记为 bad_response 并切换；应识别出来记为 invalid、不切换（T-13）。
      转发服务的响应里带它自己的 `usage.cost`，可以用来与按标价算的成本对账。

### 3.3 Registry 与 Policy Engine（T-08、T-09、R-01 评审与另一家复审）

- [ ] 全局 Registry 修订号（设计书 3.1.2，要改 proto）；在那之前，真 Planner 每一轮列 Recipe 取的是当前默认版本，运行中的 Task 重规划时看得到新晋升的 Recipe（R-06 评审 1.3）。
- [ ] `expand_template`；Model Gateway 改读 Registry；租户策略的收紧与放宽语义；发布租户策略、Recipe 时校验 Hook 名。
- [ ] before_compact 挂载点；提示注入检测（附录 F.3“外部内容进入上下文”）；Task 级挂载点的 modify、inject；reroute 换 Recipe 后重新加载它的 Hook。
- [ ] tool_loop 的单次工具调用经过 before_node、after_node（R-01 评审第 4 节第 5 条，人定登记）。
- [ ] 持久审计存储（现在审计记录只进 Telemetry）；扩展 Hook 的 reason 限制格式。
- [ ] 并行 Node 的预算预留（现在可以一起通过 budget_check 再一起超支）；sub_task 的花费带回父 Task（TaskWorkflowResult 加成本字段，要改 proto）。
- [ ] 跨 Session 写同一份共享产物的分布式写者锁；`single_writer_lock` 现在只复述 Stage Runner 的判断。
- [ ] `checkpoint` 记回滚方式的描述；Node Executor 验证 GateRecord 的来源（签名或持久的审批记录）。
- [ ] Recipe 展开与工具检查时 Catalog 取不到，现在整个 Task 工作流失败，应像 Stage 开始时那样记为 denied；惰性 Catalog 查询的竞态（目前走不到）。

### 3.4 工具、SecretStore、Ingress（T-10、T-11、T-11b，R-04、R-05 评审）

- [ ] MCP Gateway 单独部署、HTTP 传输、MCP 工具的权限；MCP Server 的凭据改由 SecretStore 引用。
- [ ] 工具版本固定到 Task；`shell_run` 进 Sandbox（资源限制、网络隔离）；记忆检索等平台内工具。
- [ ] 生产 SecretStore（云 KMS + ESO 管平台密钥，OpenBao 管租户动态凭据）；按租户或 Task 前缀批量吊销；开发实现读密钥前检查文件是否变了。
- [ ] Valkey 适配器与 SSE 跨节点广播；流的条数上限与保留时间；同时在线连接数的限制；重连按 event_id 定位读。
- [ ] 人工回复改用带校验的 Temporal Update，对不上的回复当场拒绝（核心链路）；浏览器客户端的会话换令牌（部署层）。
- [ ] 进度事件：node_started、tool_status、思考与 token 增量（附录 K.1，要 activity 一侧的推流路径）；每个事件一个 activity 的历史开销。

### 3.5 Context Builder（R-03 评审）

- [ ] model 节点接 Context Builder（现在只有 tool_loop 经它）。
- [ ] effective_ctx 进 Model Profile（改 schema，走 S-01）并按 Task 固定主模型；token 估算按实际 input_tokens 校准。
- [ ] reset 的质量触发与 G.4 按目标切换摘要模型；运行时指标；`summary.json` 改条件写入。

### 3.6 Learning Pipeline（T-16、R-06 评审与另一家复审）

- [ ] 从 Redpanda 消费（EventStream 的 Kafka 适配器）；offline 队列上的 Temporal 工作流；Chart 里 ScaledJob 按 Kafka 积压触发、Job 却读固定文件，要等适配器统一。
- [ ] 影子流量的分流与对比；灰度放量与指标变差自动回滚；归因反哺路由、Model Profile 阈值与 Node Template（反哺之前先处理归因提示的注入）。
- [ ] Recipe 统计（Planner 的 recent_failure 仍是 false）、离线搜索、Reasoning Template、Model Profile 内部分数；平台作用域的 Recipe（要先定跨租户聚合的隐私规则）。
- [ ] Learning 自己的发布令牌与角色；跨运行的预算总额与按价值抽样；Learning 调用的计量记在平台还是租户名下（R-06 第 4 节第 6 条）。
- [ ] Registry 的 Promote 带“基于哪个现行版本”的比较（CAS）；重跑通过门槛时不再重复 Publish 影子版本；trace 存储的访问控制。

### 3.7 部署与运维（T-15）

- [ ] 真集群安装与镜像构建都没验证过。
- [ ] 2.2 的容量指标仍待定。

### 3.8 执行保障与恢复（T-13 真模型端到端）

- [ ] `plan_round`、`review_round`、`tool_loop_step` 与 `call_tool` 不发心跳：Worker 崩溃后要等 start-to-close（模型决策 5 分钟）才重试。T-13 杀 Worker 的用例里，
      中断的 `tool_loop_step` 过了约 5 分钟才由第 2 次尝试完成。要么给这几个 activity 加后台心跳，要么按 Node 的预算给出心跳超时（核心链路，改调度参数要加版本分支）。
- [ ] Planner、Reviewer 调模型失败时回退到保守规划或只按规则判定，Task 照常进行，外面看不出来：T-13 第一次运行里规划超时就是这样被掩盖的。
      回退要记进 trace 与 Task 的结局摘要，评测与 live 用例据此判定。
- [ ] 真模型下一次完整规划（经转发服务、带思考）超过 90 秒；网关单次尝试的超时与模型决策 activity 的 5 分钟要按 Model Profile 的实测延迟一起定。

## 4. 原调研文档的落地路线

调研阶段建议分四个阶段落地，每个阶段有明确的验收门槛，前一阶段的 trace 是后一阶段的数据基础。阶段 1 是一切的前提：没有全量 trace 和“每次成功成本”，后面的路由、记忆和 Recipe 都无从评估。

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| 1 | Control Plane 与 Worker 骨架：Task Service、Session 与 Task 工作流、Planner 与 Reviewer、三类 Node 执行、trace | 杀掉 Worker 后，Task 能自动恢复 |
| 2 | Model Gateway、Context Builder 与 Triage | 每个任务族都能算出每次成功成本 |
| 3 | Memory Service：偏好与经验写回、混合检索 | — |
| 4 | Learning Pipeline：评测、路由训练、Recipe 统计、离线搜索 | — |

[强档实施计划](../plan/strong-tier-plan.md)在这四个阶段的基础上做了两处调整：

- 前面加了阶段 0（契约与基座）。
- 执行保障（阶段 2）与评测集（阶段 3）提前，排在 Memory 之前。

原因是确定性、幂等与评测集是后续一切工作的门槛。
