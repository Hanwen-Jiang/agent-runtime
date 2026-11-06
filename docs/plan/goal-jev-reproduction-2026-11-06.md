# N16-R：原 $1.97 Jev 任务复现准备

状态：只读证据核对和原题冻结完成，**未运行新的模型实验，费用 $0**。新增题目为 `services/py-learning/evalsets/paired/jev-reproduction.json`，不修改现有 `cases.json`、`paired.py` 或生产配置。该文件不追加任何费用授权。

## 找到的原题

直接从 `/root/ar-release/rev38/sub-walk.sh:9` 的 `msg` 字面量解码，未改写：

> 帮我做个技术选型调研：分别独立调查 Python 的三个 HTTP 客户端库 requests、httpx、aiohttp（各自的同步/异步支持、超时与重试的写法、连接池、HTTP/2 支持、维护活跃度），三份调查互不依赖，可以并行。最后综合成一份对比结论，给出在“高并发爬虫”和“普通后端调用第三方 API”两个场景下各推荐哪个，并说明理由。直接在回答里给出结论，不用写文件。

消息 UTF-8 SHA256：`9eb5f71e00978a1b9ad7f229f76d725cf6ee879d6b126cafe2bca2ae4ae09461`。原 Task 是 `tsk_01M6Q70KK2A2NRVG2HYXDSN5DD`，租户 `evals`，工作区 `v1dep-234230`。

当前配对集有十题，其中 `compare-http` 使用虚构的 Atlas/Boreal/Cedar 文件，**不是原题**。新文件保留原请求、来源路径/摘要、事实与未知项、修复的准确边界及验收条件。`required_patterns` 只检测三库和比较主题是否被提及，不代表事实正确或推荐合理。

## 旧证据的强度

| 事项 | 可核实的事实 | 不能据此声称 |
| --- | --- | --- |
| 原部署 | `rev38/deploy.log` 与 `manifest-rev38.yaml`：Helm 36，Runtime `b673f4bf45a0f736c9c62c4b3f33e814548b8aff`，coding-agent `517a044bbb1f1c2c0f01cb856fa3d577ee2f6a50` | 不能用部署 SHA 代替 Registry 修订 |
| 子 Agent | `sub-walk.log` 记录首轮三个独立 research 子 Agent 成功，随后 compare_libraries、deliver_conclusion | 日志没有完整命令/模型调用清单，不能计算重复工具调用或子 Agent 精确费用 |
| Jev | 首轮 `rule_judge_disowns` 导致 replan；文档记 disowns=0.82 | 原始 Jev 响应及实际 Jev 版本未保存在这组文件中 |
| 终态 | 历史 `cloud-deploy.md` 记第二轮 done | 保存的 sub-walk.log 自身只显示 RUNNING、round=2、cost=0.2429，后续事件截到 deliver_final_answer 开始，没有终态 |
| 费用 | 历史文档记约 $1.97、“约一半是那一轮” | 未找到原 Task 最终 model_usage 导出；总费用、子 Agent 费用、第二轮费用和外部 Jev 费用均不能据此精确核算 |
| 模型与 Registry | manifest 证明 Planner/Reviewer=model、子 Agent=rules、Jev 开启、Memory 接入；rev36/registry-seed.log 有实际平台 Model Profile 发布版本 | 没有原 Task 每次实际模型选择、Gateway 实际加载修订及正文摘要证据；发布日志不能倒填成该 Task 实际调用的版本 |
| 工作区/外部资料 | 原工作区 ID 已知 | 未找到原工作区不可变快照、原完整回答或所见外部文档快照，不能冒充原运行所有条件已复原 |

来源文件的完整 SHA256 已写进新 JSON。读取时只提取非敏感部署字段；未导出令牌、密钥、Secret 或凭据。原脚本声明的 $1.5 是每十秒查询后取消的监控条件，不能当作硬上限证明，也不能把已经出现的 $1.97 视为新的费用授权。

## f06e30c 实际修复什么

修复提交 `f06e30c9e92519cee6a2d1f42684d0f2e6048782` 只改两处：

1. `workflows/verification.py` 统计 `rule_judge_disowns` 和 `rule_judge_off_topic`，累计为 `judge_blocked` 并传入后续 Reviewer。
2. `reviewer/rules.py::judged_problem` 在 `judge_blocked >= 1` 时不再通过这两条 Jev 规则阻止后续 done。

Jev 问题提示、disowns ≥0.8/answers ≤0.2 门槛和首次拦截逻辑没变；`Reviewer._judged` 也没有因此停止后续 Jev API 调用。它限制的是**同一 Task 反复拦截**，没有消除第一次把诚实“未确认”误判为 disowns 的问题。

旧任务记录本来就是一次 Jev 拦截后第二轮 done，所以不能据提交说明断言原任务会少一轮、少花一半、或原误判已修好。当前同题实验必须分别报告“首次误判是否复现”和“是否再次因相同 Jev 规则被拦”。零次 veto 只说明本次未触发边界；不能代替重复拦截回归的验证。

## 固定验收与费用采集

- 将新 JSON 的一个 case 原样纳入共享配对 plan 的 `cases`，当前 off/rules 各一 Task，同一主模型及各角色模型、不可变 Registry 修订、Memory 配置/数据、Jev 设置/实际版本、工具/Skill/MCP、网络策略、seed 和评分版本。不能改写原消息去诱导降低 Jev 分数。
- 每臂新建空工作区、两臂一致；不得重用或改写历史工作区。此为相同原题的当前配对复现，历史工作区/检索上下文未完全复原的限制保留，旧 $1.97 不进入同配置因果比较。
- 开始前核算 `2 × 单题预留 + 共享实验已花费用 + 未结算预留 + 外部 Jev 预算`，沿用已有批准的 N16-R/EV-REAL 共同额度；当前文件没有扩大额度。没有可核对的外部 Jev 费用时，总费用保持未知。
- 质量检查必须覆盖三库的同步/异步、超时、重试、连接池、HTTP/2、维护活跃度，以及两个场景的有理由建议。不预定唯一获胜库；事实未核实应明确说明。关键词命中只作主题覆盖，不能替代对事实、证据和推荐理由的检查。
- 按 Task/round 去重统计 Jev veto。每 Task 两类 veto 合计不得超过一次；如首轮发生 veto，保存下一次 review 输入的 `judge_blocked` 和该次最终 verdict，核实没有再次被 Jev 规则否决。其他 Reviewer 规则仍可能要求继续，不强求总轮数一定为二。
- 从 Temporal 取真实子工作流树、主 Task 终态和时间区间；从 SSE 取节点/工具观察、重复目标摘要和额外轮数。公开 SSE 缺少原始完整参数，重复目标数必须继续标为代理指标。
- 费用读取租户绑定的 `model_usage`，以 `reservation_id` 去重，累计所有成功、失败和回退尝试的实际金额；NULL/pending 保留未知。通过真实子工作流节点身份归属子 Agent 成本，无法归属的费用单列。只有可靠的 round/调用关联才能给出某轮精确费用，不能按“约一半”分摊。
- Jev 是外部服务，费用不在 Model Gateway 的 model_usage。另保存实际服务版本、用量和服务账单的非敏感对账记录，和模型账本合并后才报告完整总费用；缺账单时输出已知下界和缺口。

## 当前 runner 的接口缺口

现有 paired runner 可加载这个 case，能够收集 Jev veto 次数、额外轮数与 Model Gateway 费用；无需为载入新题修改共享 runner。但它不会自动执行 `reproduction.acceptance` 元数据里的人工质量/Jev 证据检查，不能仅凭该 case 的 regex 分数宣布验收。

还需主负责人协调非敏感证据收集：Jev 的 `answers/disowns/model/input_tokens` 当前只在 `reviewer/activities.py` 的活动日志中，未进入 `ReviewRoundOutput`；采集时按 Task/round 关联日志或 trace，只导出数值和标识。另从 review activity 输入提取 `verification.judge_blocked`。现有 `collect_temporal` 不导出这些字段；`collect_ledger` 也未取 `reserved_at` 或 per-round 关联。若要补 runner 接口，由 paired 负责人统一修改，本文不占共享文件。

验证仅做：JSON 解析、原消息与脚本文字逐字一致、所有来源 SHA256 核对、当前配对 loader 兼容、原十题未包含该消息。未提交 Task、未调用 Jev 或真实模型。
