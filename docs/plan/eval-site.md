# 评测网站：主页、评测结果与数据集（2026-11-03，设计稿）

挂在哪：TMP 计划（`eval-observability-and-runtime-fixes.md`）第 11.12 节的 Eval Dashboard 与第 11.16 节的实时一栏的延续，work-dag 的 H 线。

## 1. 人的要求（2026-11-03）

- 把已有的数据集做成动态页面，罗列具体的题和答案，标注来源，细节可以展开；默认按评测类别分组，也能换别的分法。
- 页面的主要目的是透明；同时介绍测试框架：哪些是标准框架、哪些是自研、哪些是改编的，为什么改、好在哪，每个数据集用的是哪套。
- 与现有的评测页面合并，另做一个主页展示两个子页的重点，细节分别展开。
- **数据不要等发布时才更新**（人否了“发布时生成”）；其余由 Claude 定。

## 2. 页面

| 地址 | 内容 |
| --- | --- |
| `/` 主页 | 两张卡片：评测结果的重点（最近几次的关键分数与实时一栏的摘要），数据集与框架的重点（各类来源的题量、人审状态、框架一览）。点进去看子页 |
| `/results` | 现在的评测看板（原来的 `/`），原样搬过来；`/` 的旧链接跳到这里 |
| `/datasets` | 新页：上面是测试框架的介绍，下面是题目列表 |

`/datasets` 的列表：

- **分组**：默认按评测类别（分诊与规划、Reviewer 判定、上下文压缩、交付质量、记忆检索、写代码、终端、工具调用、多轮智能体、当裁判、长上下文、事实问答）；可切换为按来源类型（自编、改编、原题）、按框架、按人审状态、按数据集。另有搜索与筛选。
- **每题一行**：题号、题目摘要、答案摘要、来源标记（数据集、上游版本、题号或行号，带链接）、状态标记（已人审、草稿、原题）。
- **展开**：完整输入、完整答案或判分标准、我们补的标签与理由、保留的上游原字段、许可证与归属、它属于哪道原题（派生题的 `group_id`）。
- 中英双语，写给不了解本项目的人（同 11.12 的约定）。

框架介绍按三类写，每个数据集标出用哪一套：

- **自研**：Task Loop 评测框架（`agentruntime_learning.evals`：被测对象、规则判分、留出集只给汇总），能力矩阵调度（钉模型、bootstrap 与收缩），经 Model Gateway 的裁判（钉 Opus、逐条留记录）。原因：没有现成框架评我们的分诊、Planner 与 Reviewer 的决策。
- **官方原样**：SWE-bench 的判分工具、τ²-bench、Harbor（Terminal-Bench）、DeepEval、Ragas，LongBench、SimpleQA、FRAMES 的官方模板。
- **改编**：Terminal-Bench 的离线判分（Task API 不能在原容器里跑验收命令，只带回工作目录）、BFCL 的 AST 规则（网关的参数都是浮点数，按声明还原整数；只做单轮）、SimpleQA 的“不知道”给 0.25、所有裁判经网关。每条写明改了什么、为什么、代价是什么。

框架介绍与每个数据集的说明写在仓库里的一份双语数据文件（`services/py-learning/evalsets/site.json`），与题目一样动态更新，不写死在页面里。

## 3. 数据怎么“动态”

不在发布时生成。看板服务（learning 镜像里的 `dashboard_server`）自己拿数据：

1. **题目来自仓库 main**：服务每 5 分钟用只读令牌取一次 main 的 `services/py-learning/evalsets`（GitHub 的 tarball 接口，只解出这个目录，标准库 `urllib` + `tarfile`，不加新依赖），放进自己的缓存目录；取不到时用上一份并在页上标明数据的时间。evalsets 改动推到 main 后几分钟内页面就变，不用重新部署。
2. **原题数据集只列我们实际会跑的题**：每个公开数据集在 evalsets 里有一份选题清单（`public/<数据集>.json`：上游版本、题号与题目内容、许可证），与题目一起同步。SWE-bench 是那 50 题，BFCL 等按能力矩阵的计划与种子选出的前缀。整个数据集不搬。
3. **评测结果**：实时的一栏照旧读 VictoriaMetrics；报告也改为服务读取（与题目同一个同步机制取 `.eval-reports` 的公开汇总，或由评测脚本写进共用卷），不再靠发布时生成整页。具体二选一在验收测试时定。
4. 页面本身是静态的壳，内容全部来自 `/api/*.json`。

## 4. 守住的规则

- **留出集只给题量，不给内容**：在服务端过滤，不发到浏览器；派生题按 `group_id` 跟着原题走。
- **草稿与改编题照常展示，醒目标“未经人审”**；已人审的写确认人和日期。
- **每题必须有来源**：自编题写“自编”和所属工作项，改编与原题写上游仓库、版本、题号与许可证；缺来源的题不上页面，服务端日志记一条。
- **不展示的**：LongBench 的长篇上下文（只给问题、选项与链接）、租户数据（评测集本来就不含）。
- 服务端只答固定的路径：`/`、`/results`、`/datasets`、`/api/live.json`、`/api/site.json`、`/api/datasets/<名字>.json`（名字只认索引里有的），其余一律 404；请求的路径不拼进文件名（沿用现有约束）。
- 只读令牌是 K8s Secret `agent-runtime-eval-site` 挂进来的文件（可选，公开仓库不要），只读这个仓库的内容；日志、页面与状态里不出现它。看板现在不接 SecretStore，为一个令牌接上它太重，所以不放 SecretStore（写验收测试时改的）。
- 网络：看板的 NetworkPolicy 现在只出到 VictoriaMetrics 与 DNS，要加一条出到公网 443（排除内网段），只为取 GitHub。

## 5. 验收测试（先写，写完停下给人看）

| 文件 | 看什么 |
| --- | --- |
| `services/py-learning/tests/test_eval_site_catalog.py` | 从 evalsets 目录生成索引与各数据集：每题都有来源与状态；留出集只有题量；草稿标未审；派生题带 group_id；缺来源的题被剔除并记录；按类别、来源类型、框架、状态、数据集都能分组 |
| `services/py-learning/tests/test_eval_site_sync.py` | 对着假的 GitHub：只解出 evalsets 目录、路径穿越的条目被拒、取不到时保留上一份并标时间、令牌不进日志 |
| `services/py-learning/tests/test_dashboard_server.py`（扩充） | 新路径的白名单、未知数据集名 404、`/` 旧链接到 `/results`、安全头 |
| `tests/deploy/test_eval_dashboard.py`（扩充） | 令牌的 Secret 引用、NetworkPolicy 出到公网 443 且排除内网段 |
| 页面冒烟 | headless 浏览器里三页都能打开、切换分组、展开一题、中英切换 |

## 5.1 要先补的数据

- 各公开数据集的选题清单（`public/*.json`），从现有适配器按计划与种子导出。
- `site.json`：框架与各数据集的双语说明。
- 自编集的“评测类别”映射（task-loop 现有的 category 是业务分组，与页面的评测类别不同）。

## 6. 实现记录（2026-11-03）

- `agentruntime_learning.evals.site_catalog`：按 `evalsets/site.json` 读各数据集（task 类目录、jsonl），留出集只计数，缺来源的剔除并记进 excluded（不上页面）。
  仓库现有 10 个数据集：公开 98 道，留出集 24 道只计数，没有缺来源的。
- `agentruntime_learning.evals.site_sync`：tarball 接口 + 标准库 tarfile，只解 evalsets，跳过不安全的条目，失败留上一份；`SiteSync.local` 给本机。
- `dashboard_server`：`/` 主页、`/results`（原看板，`/index.html` 301 过去）、`/datasets`、`/api/site.json`、`/api/datasets/<名字>.json`；
  主页与 /datasets 的页面在 `evals/site_pages/`（随 learning 镜像）。`--site-dir` 本机直接读一个目录。
- Chart：`evalDashboard.site.{repo,ref,tokenSecret,cacheSize}`；给了 repo 才有环境变量、令牌卷、缓存卷与公网 443 的出站。
  内存上限 96Mi 改成 192Mi（目录在内存里建）。
- 验证：py-learning 新加与改过的测试 77 条全过；整套 697 过、3 失败（`test_terminalbench.py` 的判分三条，没动到的代码）；
  部署测试在服务器上跑（本机没有 helm）：`test_eval_dashboard.py` 全过，`helm lint --strict` 过。本机起服务在浏览器里看过三页、分组切换、展开与中英。

与本稿不同或没做的：

- 令牌改为挂载的 K8s Secret 文件（第 4 节已改）。
- **原题（2026-11-03 补上）**：`evalsets/public/*.jsonl`，来源写在 site.json 的数据集上，状态是 original（官方题，不需人审）。
  SWE-bench Verified 是 t18-rev19 与 sol-rev18 跑的那 50 题，Terminal-Bench 2.0 是全部 89 题（只有题目说明与测试，**不放官方参考解**，免得题目流进训练数据）；BFCL、JudgeBench、LongBench v2（只有问题与选项，不收长篇原文）、SimpleQA、FRAMES 是能力矩阵第一轮的前缀（`scripts/export_public_items.py`，n = 40、seed = 0）。**τ²-bench 与 RewardBench 2 还没上**：前者要装官方的 tau2 包才选得出题，后者经 Hugging Face 的 rows 接口取回的 JSON 被截断（适配器要改用 parquet）。页面现在公开 437 道，留出集 24 道只计数；所有数据集合起来约 4 MB，页面一次取完，以后可以改成按需加载。题目里形似访问令牌的字符串（Terminal-Bench 的 sanitize-git-repo 故意放了 4 个假令牌）换成了占位符，题目的 extra 里注明：GitHub 的推送保护会拦，也不该进仓库。
- 评测结果仍是发布步骤写的整页（第 3 节第 3 条的二选一没做）；主页的结果卡片读实时的一栏。
- 页面冒烟测试（headless 浏览器）没写，靠手动看过。
- 上线要：人建只读令牌（fine-grained，只给本仓库 Contents 读），建 Secret `<release>-agent-runtime-eval-site`（键 `token`），
  `helm upgrade` 时设 `evalDashboard.site.repo=Hanwen-Jiang/agent-runtime`，换新的 learning 镜像。
