# Q78 多因素模型路由实施记录

本切片落实人工验收 Q78：能力按工作种类与节点阶段区分，发布时间对老模型明显降权，结合价格、性价比、可用性及既有租户硬约束。它是已批准设计的实现，不改写冻结设计书，也不改变显式 Tier、角色默认 Tier 或跨档升级规则。

| 决策项 | 本次结论 |
| --- | --- |
| Failure | 现有 Router 在同档候选里只按输入加输出单价排序，不能表达近期模型或某工作种类/节点阶段的优势；新增评测若引用不存在的 Tier，Registry 发布后 Gateway 会无法加载。 |
| Evidence | 原排序直接比较 `Cost.UnitPrice()`；先写的年龄/阶段路由测试及发布日期同步测试在原实现失败。Registry 测试证实原实现接受不存在的评分 Tier。Q78 明确要求这些因素。 |
| Minimum fix | 扩展现有 Profile JSON，使用一次纯函数评分；Gateway 转交已有的 `CallContext.task_family`；在现有 Registry/Profile 配置校验处检查新字段与维度。 |
| Benefit | 相同工作、阶段和时间下可解释地排序；老而便宜的模型不能仅靠低价获胜；不把取价时间或缺失评测伪装成模型新鲜度/质量。 |
| Performance cost | 每次选档额外扫描候选的评分条目，总计 O(评分条目数)，排序仍 O(候选数 log 候选数)。每次读取时钟一次，无新增网络请求、磁盘读取或后台任务。 |
| Implementation cost | Profile/schema、Router、Gateway 一行传参、同步脚本及两处已有配置校验。无新依赖、表、服务、重试层或持久状态。 |
| Operational cost | 现有 Profile/Registry 文档增加可选字段；有真实评测后按原发布流程填写。无需运行新的探测器或调参服务。 |
| New failure surface | 不可比的评测或不合适的权重可能影响排序；分数必须在 [0,1] 且保留来源与日期。权重是首版工程选择，不声称来自线上优化实验。 |
| Release slice | 本次交付完整配置读取、同步、过滤、评分与调用链传参及本地回归；真实基准数据填充和 k3s/供应商验收按既有发布流程进行。 |
| Decision | KEEP 这个无状态实现；不增加在线自动调权或动态健康状态。今后有实测校准时只需调整评分函数/数据；替换算法不需要状态迁移。 |

## 字段与兼容

- `released_at`：实际发布日期，`YYYY-MM-DD`，缺失不推断；与 `freshness.price_at` 独立。
- `capability_scores`：条目包含 `task_family`、`role`、`tier`、`score`、`source`、`at`。三个维度精确匹配才使用；同维度不重复，Tier 必须属于该 Profile。角色沿用已有枚举，任务族沿用已有格式。
- `availability`：`false` 从候选中剔除，pin 不能绕过；省略保持旧 Profile 可用。这是配置事实，不是实时健康探测。
- `reliability`：`score`、`source`、`at`，记录 [0,1] 的可用/成功请求比例观测。

以上字段均为可选，旧 Profile 不需迁移。能力/可靠性缺测量时采用 0.5 先验；未知发布日期不给新鲜度奖励。`Candidate.Score` 的 `CapabilityMeasured`、`ReleaseKnown`、`ReliabilityMeasured` 显示哪些是已知数据。配置仍拒绝未知字段，评分漏填也拒绝，不把它当成零分。

## 首版评分

保留原有档位、能力、租户白名单及 `avoid_vendor` 过滤，然后计算：

```text
Q = 精确匹配 task_family × role × tier 的能力分数，缺失为 0.5
R = reliability.score，缺失为 0.5
A = 2 ^ (-max(0, 距发布日期的天数) / 180)，未知发布日期为 0
V = Q / (1 + cost.in_per_m + cost.out_per_m)
总分 = 0.45 Q + 0.35 A + 0.15 V + 0.05 R
```

发布日期权重 35%，明显高于价格性价比 15%；180 天新鲜度减半。价格沿用美元/百万 token 的输入加输出基础价作为首版代理，不声称它是一次任务的实测成本。`V` 在零价时也有限。相同能力/发布时间/可靠性下更便宜的候选优先；明显更强或更新的模型可以胜过便宜模型。

按总分降序，同分按基础价、Profile id 升序。合法且未被过滤的 pin 仍排第一。同次选择只取一次 UTC 时钟，`Choice.AsOf` 及 `NewWithClock` 支持固定时间重放/测试；不新增协议字段。

## 数据来源与实际补齐

2026-11-03 读取 [models.dev API](https://models.dev/api.json)，按原脚本第一方供应商 + 模型名匹配，将其 `release_date` 补入开发 Profile 与 Registry seed。此次只回填发布日期，保留原价格及取价日期。后续 `scripts/sync_model_profiles.py` 同步合法完整日期；来源缺失或日期无效时保留已有值，未知模型不改。

| 模型 | API 返回的发布日期 |
| --- | --- |
| gpt-4o-mini | 2024-07-18 |
| gemini-3.5-flash-lite | 2026-07-21 |
| deepseek-v4-flash | 2026-10-17 |
| gemini-3.5-flash | 2026-05-19 |
| claude-sonnet-5-5 | 2026-09-28 |
| gpt-5.5 | 2026-04-23 |
| claude-opus-5-5 | 2026-09-22 |
| claude-haiku-5-5 | 2026-10-07 |
| text-embedding-3-large / text-embedding-3-small | 2024-01-25 |
| gpt-6.1-sol | 2026-09-29 |

models.dev 不提供这里定义的任务族/节点阶段/推理档实测矩阵，也不提供本服务的运行可靠性。因此不生成这些分数；测试中的数值明确是合成夹具。上线 Profile 要获得能力差异的实测收益，仍需由真实内部评测或可比较外部评测填入带来源的条目；没有数据时结果会明确显示先验。

## 验证与边界

先验证新行为在旧实现失败，再完成实现。完成后执行：

- `go test ./services/go-model-gateway/... ./services/go-task-service/internal/registry`：通过；`AR_ALLOW_MISSING_TOOLS=1`，本机无 PostgreSQL 工具，Registry 的 SQLite 路径通过，PostgreSQL 路径跳过。
- `python -m pytest tests/deploy/test_model_profiles_sync.py tests/contract/test_model_profile_routing.py tests/contract/test_schema_examples.py -q`：359 项通过。
- `git diff --check`：通过。

覆盖便宜老模型降权、任务族和角色分别影响结果、不同推理档评分隔离、可靠性、availability/白名单/pin/avoid 语义、固定时间与解释分解、旧 Profile/零价/缺数据兼容、RPC task_family 传入、Registry 交叉字段检查、严格 schema 和同步日期不造假。

后续收尾已在 WSL Debian、Go 1.26.5、真实 PostgreSQL 17.10 下通过 Registry、Task Store、Task API、task-service 命令包与 Gateway 测试；上面的 PostgreSQL 跳过仅记录首轮环境。真实供应商能力评测及线上 k3s 验收仍未完成。
