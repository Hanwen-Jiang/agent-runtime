# 模型评测、价格与路由

> 来源：`主文档` 第四、五节。已按设计书附录 A 改用新术语：模型画像 → Model Profile，档位 → Tier，路由器 → Model Gateway 的选档。
> 设计以[设计书](../design/agent-runtime-design.md) 3.3 与附录 H 为准。本文保留完整的平台调研和结论依据，供实现 Model Profile 的数据导入与选档逻辑时查阅。
> 平台的指数版本、价格、数据更新时间都会变；本文记录的是 2026 年 10 月调研时的状态，使用前请到原页面核对。

## 1. 结论

没有一个平台能直接回答“这个任务该用哪个模型、开多大推理强度”。可信的做法是组合三类公开数据，再用自己的任务集做最终裁决：

1. 独立综合评测：能力 × 价格 × 速度。
2. Agent 专项评测：包含脚手架与成本。
3. 实时价格与可用性。

## 2. 评测与价格平台

| 平台 | 测什么 | 对选型的用处 | 局限 |
| --- | --- | --- | --- |
| [Artificial Analysis](https://artificialanalysis.ai/models/releases/gpt-6-astra) | 自己跑评测，合成“智能指数”（v4.2 含 10 项，如 AA-Briefcase、GDPval-AA v2、τ³-Banking、Terminal-Bench v2.1、HLE、AA-LCR）；同时测价格、输出速度、每任务成本；另有 Agentic 指数，以及金融、法律、医疗等分行业指数 | 能力、价格、速度三方面初筛；“每任务成本”已含思考 token，比单价更真实 | 综合分掩盖单项差异；指数版本常更新，跨版本分数不可比 |
| [Arena（原 LMArena）](https://botnation.ai/en/chatbot-arena/) | 人类匿名两两盲评形成 Elo；2026-01-28 更名 Arena；Style Control 剔除长度与格式偏好 | 对话、写作等主观体验的人类偏好参考 | 偏好不等于正确；存在被针对性优化的争议，看分要看置信区间 |
| [HAL（Princeton）](https://iclr.cc/virtual/2026/poster/10006806) | 第三方、默认计入成本的 Agent 评测；模型 × 脚手架 × 基准三维；21,730 次 rollout、9 个模型、9 个基准，花费约 4 万美元 | 直接给出成本-准确率的帕累托前沿，回答“换模型或调高推理强度值不值” | 覆盖的模型少，更新慢于新模型发布 |
| [Vals AI](https://www.vals.ai/benchmarks/vals_index_v2) | 私有数据集上的行业真实任务（金融、编码、法律、医疗），报告准确率、每测试成本、耗时、误差条；Vals Index v2 按行业经济权重加权 | 垂直领域选型；私有集降低训练集污染 | 私有集不可复现；行业覆盖有限 |
| [Epoch AI Benchmarking Hub](https://techjacksolutions.com/ai-brief/epoch-ai-cost-deflation-enterprise-ai-strategy-benchmarks/) | 自跑与外部提交分开标注；Epoch Capabilities Index；提供 CSV、ZIP 与 Python 客户端下载（调研时数据更新至 2026-10-08） | 结构化数据可直接导入 Model Profile | 偏能力趋势，不测延迟与可用性 |
| [METR Time Horizons](https://capitalandcompute.net/ai-benchmarks/metr-time-horizon/) | 用人类耗时衡量模型能以 50% 成功率完成多长的任务；2026 年发布 1.1 版，扩充了长任务 | 估计长时程任务能否托管、检查点要多密 | 以软件与研究任务为主；1.0 与 1.1 不可比 |
| [OpenRouter](https://openrouter.ai/rankings?view=daily) | 按真实 token 使用量排名；各提供商的价格、TTFT、吞吐、近 3 天可用性，可经 API 获取 | 实时价格、延迟、可用性与故障切换 | 它自己声明：排名衡量采用度，不衡量质量 |

## 3. 按节点类型看专项基准

| 节点类型 | 基准 |
| --- | --- |
| 代码补丁 | SWE-bench Verified / Pro |
| 终端与运维 | [Terminal-Bench 2.x](https://proceedings.iclr.cc/paper_files/paper/2026/hash/444a3737adaee10d86ad2ef5f74468e6-Abstract-Conference.html) |
| 工具与用户交互、策略遵循 | τ-bench 系列 |
| 函数调用 | BFCL v4 |
| 深度搜索 | BrowseComp |
| 电脑操作 | OSWorld-Verified |
| 通用助手 | GAIA |
| 长上下文 | AA-LCR、MRCR 一类 |

## 4. 四条影响设计的结论

| 结论 | 证据 | 对实现的要求 |
| --- | --- | --- |
| 脚手架和配置常比模型差异更大 | Anthropic 报告，仅配置差异就在 Terminal-Bench 2.0 上造成 6 个点的差距（[来源](https://www.buildfastwithai.com/blogs/deepswe-vs-terminal-bench-vs-osworld)） | 公开榜分数只当先验；Model Profile 中内部评测的权重最高 |
| 更高推理强度不一定更好 | HAL 发现，提高 reasoning effort 在 36 组实验中有 21 组反而降低准确率（[来源](https://papers.lunadong.com/paper/7506)） | 推理强度必须按任务族实测；同一模型的不同推理强度视为不同 Tier |
| 价格要看每任务成本，不看单价 | 推理模型的思考 token 可能让“便宜”的模型更贵；AA 与 Vals 都直接报告每任务或每测试成本 | 选档按“每次成功的期望成本”，不按单价 |
| 路由有成熟的收益证据 | RouteLLM 在 MT Bench 上只把 14% 的请求送强模型，就保住约 95% 的强模型质量，成本降低约 85%（[来源](https://lmsys.org/blog/2024-07-01-routellm/)） | Triage 与 Model Gateway 选档值得做；每次决策都记录下来，用于训练 |

## 5. Model Profile 的数据

Model Profile 的结构见设计书附录 H.1。

### 5.1 数据可信度权重

从高到低：

1. 内部评测。
2. 垂直第三方：Vals、HAL。
3. 独立综合评测：Artificial Analysis、Epoch。
4. 厂商自报。

### 5.2 刷新频率

| 数据 | 频率 | 来源 |
| --- | --- | --- |
| 价格、延迟、可用性 | 每天 | OpenRouter 或厂商 API |
| 公开分数 | 每周 | 第 2 节各平台 |
| 内部评测 | 新模型上线时，以及每月一次 | 生产 trace 沉淀的评测集 |

### 5.3 每项能力分数都要带来源与时间

例如 `code_patch: {score, src: swe-bench-pro, at: 2026-10-01}`。上下文长度记录**实测有效长度**，不记标称窗口，依据见[上下文与压缩参考](context-engineering.md)第 1 节。

## 6. 选档

### 6.1 目标

选档的目标不是单次调用最便宜，而是在成功率、延迟和租户约束下，让一次成功交付的期望成本最低。对“生成 → 验证 → 重试”的节点，可以近似为：

```text
E[cost] ≈ (c_attempt + c_verify) ÷ p_success
```

单价便宜但成功率低的 Tier，可能比强 Tier 更贵。这也是 HAL 用成本-准确率前沿，而不用单一分数排名的原因。

### 6.2 运行时升降级

- **先调推理强度，再换模型**：Task 中途更换主模型会丢失提示缓存。主模型尽量固定，换模型留给子任务和 sub_agent。
- **升级触发**：验证连续失败 2 次、ensemble 分歧大、停滞计数到阈值、置信度低。这些信号与 Reviewer 的升级顺序共用。
- **降级探索**：某任务族在强 Tier 上连续成功后，用小比例流量或影子运行试更便宜的 Tier（多臂老虎机式探索），达标才切换。

### 6.3 内部评测闭环

1. 从生产 trace 按任务族沉淀评测集，带成功、失败标签与成本。
2. 新模型或新价格出现时，先影子运行，再灰度放量。
3. 在线跟踪：成功率、每次成功成本、P95 延迟、升级率、用户纠正率。
4. 升级率持续偏高，说明便宜的 Tier 被过度使用，应调高该任务族的默认 Tier。

### 6.4 租户约束先过滤

选档前先过租户策略：模型白名单、数据驻留区域、预算上限、BYOK、SLA 档位。不满足约束的 Tier 直接剔除，不参与优化。

## 7. 开放问题

- 各模型厂商的条款是否允许用其输出训练自有路由器。这一条也列在[风险与开放问题](risks-and-open-questions.md)。
