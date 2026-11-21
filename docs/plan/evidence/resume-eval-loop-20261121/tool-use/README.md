# 工具调用试跑与默认参数提示消融

2026-11-21 在 cloudnium 的 `evals` 租户上调用真实模型网关。保留原策略。新增系统指令确实减少了一个多余参数，但开发集没有多解决题目，独立于本次调参的 6 题还少通过了 1 题。所有实验只验证模型给出的函数和参数，未执行测试题中的工具。

| 题组 | 请求的配置 | 入选 | 有效通过 | 网关记录费用 |
| --- | --- | ---: | ---: | ---: |
| 开发 | GPT‑5.5，默认 | 8 | 不可评分 | $0.012008 |
| 开发 | Kimi K3.1 Pro，明确固定，默认 | 8 | 7/8 | $0.066010 |
| 开发 | GPT‑6.1‑sol，默认 | 8 | 7/8 | $0.082172 |
| 开发 | GPT‑6.1‑sol，追加默认参数指令 | 8 | 7/8 | $0.011026 |
| 验证 | GPT‑6.1‑sol，默认 | 6 | 5/6 | $0.005774 |
| 验证 | GPT‑6.1‑sol，追加默认参数指令 | 6 | 4/6 | $0.006002 |

共 44 次函数调用评测，网关记录 $0.182992，含首次未命中指定模型的花费。未计基础设施成本。GPT‑5.5 的 8 条请求实际均由网关报告为 Kimi K3.1 Pro，适配器标记污染并排除，不给 GPT‑5.5 算分。Kimi 对照另行固定并重跑；其余 36 条请求实际 Profile 均与指定一致。每题各运行一次；缓存与后端计费状态未冻结，费用差异不能归因于这条提示。

**实验协议。** [BFCL v4 数据源](https://github.com/ShishirPatil/gorilla/tree/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard/bfcl_eval/data)。使用项目 `3500363` 的 Python 单轮适配器和 AST 判分（对 protobuf 数值按参数类型还原），四类为 simple_python、multiple、parallel、parallel_multiple。每类按种子与题号哈希排序，再按类别轮转取前缀。开发 seed 37 取 8 题，验证 seed 38 取 6 题，两份清单交集为 0。输出上限 1,024 token，`act/medium`，单并发。验证集用于这一轮结束时做一次配对检查。仅有开发题的失败被用来提出改动，未根据验证集继续修改。无法据此评定模型总体强弱或线上修复率。

**唯一改动。** [候选适配器](bfcl_contract.py) 在每次请求前插入同一条系统消息，要求省略等于已声明默认值的可选参数。采样、工具定义、模型、用户问题、输出上限和判分逻辑均保持原样。适配器不读答案、不调用工具、不修改网关默认配置。

| 案例 | 对照结果与复核 | 后续方向 |
| --- | --- | --- |
| 开发 `parallel_multiple_69` | 两个 GPT 配置均未通过。新提示成功去掉折旧调用中的可选默认值 `monthly=false`，但折旧率仍给出 2，参考参数为 0.02；通胀率和利率也采用不同表示。工具对折旧率的说明写作“in percentage”，另外两项未明确传输单位。 | 将单位约定明确化作为下一项独立的工具协议实验；对业务工具用明确的单位字段与范围校验，并为含歧义的题单列案例分析。本次不改 BFCL 标准答案。 |
| 验证 `parallel_111` | 原版通过，候选失败。用户指定单位，工具的 `required` 列表包含 `to_unit`，虽然说明文字也提到默认值，候选却漏了两次调用的这个必填参数。 | 先在真实工具适配层做参数 schema 校验，对缺少必填字段返回可纠错反馈；下一次用全新的用例比较修复率、额外轮次和费用。 |
| 验证 `parallel_54` | 两臂均未通过。两次订单地点和尺寸正确，但把尺寸同时写进商品名，未匹配接受的条目字符串。 | 区分“错误调用参数”和“正确但与判分要求的字符串不同”；保留官方口径，可另建业务评分。 |

**复核入口。** [逐题结果、完整入选 ID、实际模型、费用与计划](results.json) 为脱敏的归档投影。五份原始归档位于 `/opt/evals/mx/`，名称见 JSON 的 `runs[].run_id`。每份只读发布的摘要是 `scores.json` 与 `plan.json`，原始请求和回答保留在运行机。

**复跑步骤。** 在项目既有评测租户环境中选定一个新的输出目录，将本目录加入 `PYTHONPATH`。从 `results.json` 取出所需的 `runs[].plan` 单独保存为 `plan.json`，使用其 `command_budget_usd` 作为总预算，保持单并发。`bfcl` 是原版入口，`bfcl_contract:make` 是候选入口。

```sh
python -m agentruntime_learning.capability estimate --plan plan.json
python -m agentruntime_learning.capability run --plan plan.json --out "$NEW_RUN_DIR" --budget-usd "$RUN_BUDGET_USD" --concurrency 1
```

比较前按 `candidate + item` 连接两份结果，并核对 `calls`、`missed_calls`、`contaminated`。模型路由不一致的调用保留成本记录，不并入该指定模型的有效分母。
