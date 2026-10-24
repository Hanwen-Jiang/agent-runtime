# 上下文压缩：Context Builder 与 S-11 评测

> 学习笔记，2026-10-21（第 4 节 2026-10-24 按 T-13 更新）。对应代码：`services/py-worker/src/agentruntime_worker/context/`（`builder.py`、`partitions.py`、`modes.py`、`summary.py`、`probe.py`、`tokens.py`）；评测：`services/py-learning/src/agentruntime_learning/evals/context.py`；设计依据：设计书 3.2.7、附录 G，[上下文与压缩参考](../reference/context-engineering.md)。

## 1. 要解决的问题

Agent 用 tool_loop 做长任务时，每一步是“调一个工具 → 拿回一段输出”，history 只增不减。几十步之后会出现三个问题：

1. **超长**：超过模型的有效长度后，模型看不全，或者质量明显下降（有效长度按模型实测，不按标称窗口）。
2. **贵且慢**：每次调用都要把整段上下文重新送一遍。
3. **丢关键信息**：粗暴截断会丢掉早期才出现、后来才显出重要性的细节，比如失败的发票号、用户的偏好、承诺的回电时间。

压缩要做到的是：**把占地方的旧内容收起来，同时保住后面还要用的关键信息，并且尽量不破坏提示缓存。**

一个 history 步骤长这样（取自评测样本）：

```json
{"tool": "ledger_classify", "args": {"invoice": "INV-0042"},
 "observation": {"item": "INV-0042", "status": "done", "result": "科目 6602 管理费用-办公费"}}
```

`tool` 是调用的工具，`args` 是调用参数（这里是“给发票 INV-0042 归类”），`observation` 是工具输出；工具失败时没有 `observation`，改为 `error`。

## 2. 上下文的五个分区

每次调用模型前，Context Builder 都从头拼一遍上下文（`ContextBuilder.build`），按“越稳定越靠前”分五个区（`partitions.Partitions.to_messages`）：

| 分区 | 内容 | 多久变一次 | 压缩方式 | 缓存断点 |
|---|---|---|---|---|
| system | 平台提示、固定工具集、租户策略 | 跨 Session | 不压缩 | 有（断点 1） |
| profile | 用户偏好、Workspace 约定（来自 Memory） | Session 内 | 只追加“偏好变更”，不改写旧条目 | 有（断点 2） |
| brief | Task 目标、验收条件、本 Goal、计划、检索到的经验 | Task 内，replan 时整体替换 | 不压缩 | 有（断点 3） |
| history | 每一步的动作与输出 | 每步追加 | **压缩的主要对象** | 无 |
| status | 进度、Task State 的精确状态、补回的事实、最近一个动作 | 每步都变 | 截到 500 token 以内 | 无，放最末尾 |

**Task State** 是由代码维护的结构化状态（目标、验收条件、约束、里程碑、已验证事实、待解问题、产物路径）。它每次都原样放进 status 区，**不经过模型摘要**。设计原则是“精确状态不进摘要”：文件清单、ID、数值靠 Task State 保住，摘要只负责叙述性的内容。

## 3. 压缩阶梯

从便宜到昂贵逐级触发。阈值都按有效长度 `effective_tokens` 算，Task Plan 的 `context.mask_at`、`context.summarize_at` 可以覆盖默认值。token 用 `tokens.estimate` 估算：ASCII 字符每 4 个算 1 个 token，其他字符（如中文）每个算 1 个。

| 级别 | 触发条件 | 做什么 | 调模型吗 |
|---|---|---|---|
| 1. offload | 单条输出超过 2000 token | 完整输出写进 ObjectStore，上下文里只留开头 400 字摘录、取回路径和保留字段 | 否 |
| 2. mask | 整个上下文超过有效长度的 40% | 最近 `keep_recent` 步以外的输出换成占位符 | 否 |
| 3. prune | — | 清理旧轮次的思考内容；history 里本来就没有思考，目前是空操作 | 否 |
| 4. summarize | mask 之后仍超过 70% | 把最早、还没摘要过的一段步骤交给便宜模型做增量摘要 | 是（`light@low`） |
| 5. reset | 同一动作连续出现 3 次，或摘要后仍超过 70% | 把剩下的步骤也摘要掉，只用 system、profile、brief、持久摘要和 status 开新上下文 | 是 |

### 3.1 mask（遮蔽）

占位符的样子：`第 5 步的输出已遮蔽，完整输出：<ObjectStore 路径>；保留字段：{...}`。

遮蔽时**不会**被遮住的东西：

1. **动作本身**：工具名和 `args` 一直保留，模型始终知道自己做过什么。
2. **工具报的错误**：`error` 不遮蔽，失败原因要一直看得见。
3. **最近 `keep_recent` 步**：原样保留，当前推理最需要它们。
4. **按工作模式必须保留的字段**：只保留不超过 100 token 的。

按工作模式区分（`modes.py`，附录 G.3）：

| 工作模式 | keep_recent | mask 时保留的字段 | 摘要必须保留 |
|---|---|---|---|
| coding | 2 | path、files_changed、command、exit_code、status、failed、passed、error | 文件改动清单、错误原文、命令与退出码、测试状态、决策与原因 |
| research | 3 | url、title、source、published、quote、citation | 引用（URL、要点、出处）、已排除的假设、待查问题 |
| conversation | 4 | preference、commitment、request、ticket、status | 用户偏好、已作出的承诺、未解决的诉求 |
| writing | 2 | path、section、comment、status、style | 风格约定、修改意见及其状态、大纲与当前段落 |
| data_analysis | 2 | path、schema、columns、row_count、value、result、query、code | 数据的 schema、已算出的数字及生成代码、分析假设 |
| batch | 2 | item、id、status、error、result | 每项结果的状态表、失败项与原因 |
| long_horizon | 2 | feature、status、version、path、milestone | 功能清单、进度、版本历史 |

mask 是**可恢复**的：原文都在 ObjectStore 里，模型需要细节时可以按路径取回。

### 3.2 summarize（增量摘要）

- **摘到哪一步**（`_cut`）：从最早的未摘要步骤开始，一直摘到剩下的内容不超过 40%；最近 `keep_recent` 步不摘。
- **只摘新截断的片段**：摘要提示（`summary.summary_prompt`）只包含这次新截断的步骤，不包含之前的摘要。模型输出一个增量，再由代码合并进持久摘要（`summary.merge`：各分区追加、去重，已有条目原样保留）。这就是“锚定式增量摘要”，旧摘要不会被反复改写，也就不会越改越走样。
- **固定五个分区**：意图、决策、产物、未决、下一步。提示里还会按工作模式写明“必须保留”的内容。
- **结果替换掉原步骤**：被摘要的步骤从 history 里拿掉。history 开头换成持久摘要，后面附一张“已执行过的动作”清单（由代码从轨迹算出，去重，最多 400 token），明确写着“不要重做，需要细节时按路径取回”。这张清单专门用来防止压缩后重复操作。
- **失败就不摘**：摘要输出不合格时重试一次，仍不合格就保留遮蔽后的 history，不丢任何步骤。
- **持久化**：持久摘要存在 `context/<task_id>/r<round>/<node_id>/summary.json`，记录摘到第几步（`covered`）。下一次构建直接读回，不会每步重写。

### 3.3 Compaction Probe（压缩后抽查）

只要这次构建做了 summarize 或 reset，就做一次抽查（`_probe`）：

1. 从 Task State 和 Task Plan 里按工作模式的 `probe_priority` 抽 3 到 5 个关键事实，挖空成问题。
2. 用压缩后的上下文让模型作答，用 `probe.grade` 判分。
3. 答错的事实，从原始 history 里找到第一次提到答案的那一步，截取前后各 200 字的原文片段，补进 history 的“【补回的原文】”（总共最多 600 token）；找不到原文就把事实本身补进 status。

**判分规则**（`probe.grade`）：先做 NFKC 规范化、合并空白、转小写。纯数字答案必须作为一个完整的数出现（`31` 不能匹配 `310`，但能匹配 `09:31` 里的 `31`），其余答案按子串匹配。

### 3.4 一次构建的完整流程

```
读回持久摘要（如果有）
→ offload：单条超过 2000 token 的输出外置
→ 总量 > 40%？mask 最近 keep_recent 步以外的输出
→ 仍 > 70%？summarize 最早的一段，直到剩下的不超过 40%
→ 出现重复动作，或摘要后仍 > 70%？reset
→ 拼装五个分区
→ 做过 summarize 或 reset？Compaction Probe，答错的补回原文后重新拼装
```

每次构建都从原始 history 重新算，同样的输入一定得到同样的输出（确定性），这是保住缓存的前提。

## 4. 提示缓存（prefix cache）

提示缓存只能复用“从开头起完全相同的那一段前缀”。当前的设计：

1. **分区按稳定性排序**：system、profile、brief 放最前，status 放最后。每步都变的内容不会打断前面的前缀。
2. **三个显式断点**：`partitions.py` 在 system、profile、brief 三条消息上设 `cache_breakpoint=True`，history 里的消息一律清掉断点；`test_context_partitions.py` 断言断点只在这三处。Planner 和 Reviewer 的提示也在 system 区末尾设断点。
3. **history 只追加**：没压缩时，每一步只在末尾多两条消息。
4. **mask 是确定的**：已遮蔽的步骤以后一直保持遮蔽，占位符文字固定。每往前走一步，就有一个原本原样保留的步骤变成占位符，缓存从这个位置往后失效。
5. **摘要持久化**：只有摘要刚发生的那一次，history 开头会变，缓存从 history 开头失效；之后每次都读回同一份摘要，又稳定下来。
6. **profile 只追加**：偏好变化写成“偏好变更：新增/撤销 …”追加在后面，前面的条目不动。

**断点怎么到达供应商（2026-10-24 更新）**：`ChatMessage.cache_breakpoint`（`model_gateway.proto`）是 Worker 交给 Model Gateway 的标记。Gateway 的 OpenAI 兼容适配器（`libs/go-core/adapters/openaicompat`，T-05）把带断点、文本非空的消息写成内容片段数组，文本片段带 `cache_control {"type":"ephemeral"}`；计量里的 `cache_read_tokens` 取自响应的 `prompt_tokens_details.cached_tokens`。

**T-13 真模型端到端看到的命中率**：CI 修复 Task 的 13 次调用，cache_read ÷ 输入合计 7%，全部来自 Reviewer（gpt-5.5，OpenAI 的自动前缀缓存，第二次 review 命中 5552 / 7920）；Anthropic 模型（Planner、tool_loop、Node）一律 0。单独探测过：同一段 18k token 的前缀带 `cache_control` 连发两次，供应商（OpenAI 兼容的转发服务）返回的 `cache_creation_input_tokens` 与 `cache_read_input_tokens` 都是 0，即它在 OpenAI 兼容格式下丢掉了 `cache_control`。所以 Worker 与适配器这一侧已经做完，Anthropic 的缓存要换成原生 Messages 接口的适配器（或支持透传的供应商）才生效，已登记在风险文档 3.2 节。

**可以改进的点**：history 是最长、也是每步都在增长的部分，但现在没有断点。Anthropic 允许每次请求最多 4 个断点，在 history 最后一条消息上再设一个“滚动断点”，下一步就能复用到上一步为止的整段 history。等 Anthropic 的缓存真正生效、能看到命中率之后再验证收益。

## 5. S-11 压缩评测

### 5.1 考什么

验收标准（实施计划 S-11）：在长任务样本上，**完整压缩（full）的效果不劣于只做 mask 的基线（mask_only）**。两个指标：

- **Probe 通过率**：压缩后用每条样本的 Probe 题问模型，答对的比例。
- **重复操作率**：压缩后让模型给出下一步动作，它与样本标注的“已完成动作”之一完全相同（工具名 + 参数）的比例。

门槛是 `full.probe_pass_rate >= mask_only.probe_pass_rate` 且 `full.repeat_rate <= mask_only.repeat_rate`。报告里另有一个“答案留在上下文里的比例”（留存率），不依赖模型作答，只看压缩后的上下文里还找不找得到答案。

### 5.2 样本（`evalsets/task-loop/context_samples/*.json`）

每种工作模式一条，共 7 条。每条包括：

- 一条 30 步以上的 tool_loop 轨迹（`history`），关键事实埋在前段，后面是大量无关输出，确保会触发 mask 和摘要。
- `task_state`、`task_plan`、`node`：Context Builder 构建时需要的输入。
- **Probe 题**（`probes`）：3 到 8 道问答，答案必须能在轨迹或 Task State 里找到。
- **已完成的动作**（`completed`）：轨迹里已经做完、结果仍然有效的动作；压缩后再做一遍就算重复操作。
- `review`：人工确认状态。只有 `confirmed` 的样本算数，`test_every_context_label_is_confirmed` 要求全部确认。

**开发集与留出集**：提示（比如摘要提示）只能在开发集上调；留出集平时不看，最后用来判断是否过门槛，防止提示被调得只在见过的样本上好看。7 条里 5 条开发集、2 条留出集（batch、long_horizon）。

### 5.3 好的 Probe 题是什么样

一道题能不能“测出压缩的差别”，取决于答案在只做 mask 时是否还看得见：

| 答案在哪 | 只做 mask 时 | 这道题能测出什么 |
|---|---|---|
| Task State 里 | 每次都注入，看得见 | 测不出差别 |
| 动作参数、错误信息、保留字段、最近几步里 | 看得见 | 只能测出“摘要把它弄丢了”（full 变差） |
| 被遮蔽的输出正文里 | 看不见 | 能测出“摘要保住了它”（full 变好） |

**首轮审阅（2026-10-21）发现并修掉的问题**：

- **答案太容易撞上**：batch 的 `31` 会被日志时间戳 `09:31:13` 匹配；research 的 `Borealis` 在轨迹里出现 52 处。这两道题几乎不可能答错。
- **答案本来就在 Task State 里**：data 的 `refunded_at`、writing 的 `v7`、long_horizon 的“按时区导出”。压缩前后都会注入，这些题只是凑数。
- **每条至少要有一道只有摘要才能保住的题**：conversation 原来四道题的答案都在保留字段里。
- **已完成动作不能误伤合理操作**：coding 原来把“改完 `pricing.py` 后重读”算成重复，但这是在核对改动；改成“在 3.12 上重跑同一个测试”，那个结果不会变。

修订后每条样本至少有一道答案只出现在被遮蔽正文里、全轨迹只出现一次的题。

## 6. 面试速查

**Q：长任务 Agent 为什么需要上下文压缩？只靠更大的窗口行不行？**
A：模型在标称窗口内的有效长度远小于窗口，越长质量越差；每步都重送整段上下文，成本和延迟随步数增长；工具输出大多是一次性的。所以要按有效长度管理，把旧输出收起来，同时保住关键事实。

**Q：mask 和 summarize 有什么区别？为什么先 mask？**
A：mask 不调模型，把旧输出换成带路径的占位符，原文可取回，保留错误、动作和关键字段，便宜、确定、可恢复。summarize 要调模型改写，有成本，也有丢信息的风险。所以先做便宜且无损的，不够再做昂贵且有损的。

**Q：怎么防止摘要丢掉关键信息？**
A：四层保护。一是精确状态不进摘要，由 Task State 每次注入。二是摘要分区固定，并按工作模式写明必须保留什么。三是“已执行过的动作”清单由代码从轨迹算出，不靠模型记。四是摘要后做 Compaction Probe，答错就补回原文片段。

**Q：为什么用增量摘要，而不是每次把整段重新摘要？**
A：每次重摘会让早期信息被反复改写，越摘越走样（摘要漂移），还要重复花钱。增量摘要只摘新截断的片段，合并时旧条目原样保留，摘要稳定，也更利于缓存。

**Q：压缩后 Agent 重复做已经做过的事，怎么办？**
A：摘要后附一张由代码算出的“已执行过的动作”清单，并明确写“不要重做，需要细节按路径取回”；mask 时动作本身始终保留；同一动作连续 3 次触发 reset。评测里用重复操作率来衡量。

**Q：怎么设计才能保住 prefix cache？**
A：分区按稳定性排序，status 放最末；在稳定分区末尾设缓存断点；history 只追加；mask 结果确定，已遮蔽的不再变；摘要持久化，读回同一份；profile 只追加变更。

**Q：你们的缓存断点现在生效了吗？**
A：链路上做完了：Worker 在 system、profile、brief 上设 `cache_breakpoint`，Gateway 的适配器翻译成 `cache_control`，计量记下 cache_read。但真模型端到端里 Anthropic 模型的命中率是 0：现在用的 OpenAI 兼容转发服务丢掉了 `cache_control`（单独探测确认过）；OpenAI 模型靠自动前缀缓存有命中。要换原生 Messages 接口的适配器才能让 Anthropic 的断点生效。这也说明命中率必须从计量里实测，不能假设标记写出去就生效。

**Q：mask 的阈值为什么是 40%，摘要为什么是 70%？**
A：这是参考值，按模型实测的有效长度设，不按标称窗口。mask 早触发是因为它几乎没有代价；摘要留出余量，给后续步骤和摘要本身的增长留空间。不同模型、不同 Task 可以通过 Task Plan 覆盖。

**Q：不同类型的任务压缩策略为什么不一样？**
A：要保住的东西不同：编码要错误原文、文件和退出码；研究要 URL 和引用；对话要偏好和承诺；批量处理要每项的状态。所以按工作模式区分保留字段、最近保留几步、摘要重点和抽查优先级。

**Q：怎么评估压缩策略好不好？**
A：用长任务样本跑 full 和 mask_only 两种策略，比较 Probe 通过率和重复操作率，要求 full 不劣于 mask_only。样本分开发集和留出集，只在开发集上调提示。还要检查 Probe 题本身：答案在 mask 后仍看得见的题测不出摘要的价值，答案在别处碰巧出现的题会被蒙对。

**Q：Compaction Probe（运行时抽查）和评测里的 Probe 题有什么区别？**
A：运行时抽查是在线的自我检查：从 Task State 和 Task Plan 自动抽事实，答错就补回原文。评测的 Probe 题是人工标注的离线考题，用来比较压缩策略。两者用同一个判分函数。

**Q：数字答案的判分要注意什么？**
A：要按完整的数匹配，避免 `31` 匹配到 `310`；但时间戳 `09:31` 里的 `31` 仍会被算对。所以出题时要避开容易撞车的短数字，或者把答案写得更具体。
