# EV-DE：DeepEval 工具证据（2026-11-04）

状态：实现与离线验收完成，待提交；开工认领 `3a47cfa` 已先推送。沿用 main 提交的小阶段交付方式，不占用 CACHE-01、ATOK-T、PERF-00 的文件。

- Failure：honesty 裁判只收到最终回答，无法核对实际工具调用；旧规则误罚主动纠错。
- Evidence：handoff-2026-11-03 第 3 节的 DeepEval 结果及 evalsets-review-2026-11-03 第 1、3 节；现有 bridge_cases 未传执行证据。
- Minimum fix：评测运行保存已有 Task 事件中的工具调用事实，作为独立上下文送入两条 DeepEval 判分路径；明确证据缺失/截断的限制，删除“不得承认编造”的评分要求。
- Benefit：裁判可对照实际调用，避免将自述当作执行证据。
- Performance cost：仅评测进程增加有界的轨迹序列化与裁判输入 token；不增加 Runtime RPC 或模型调用次数。
- Implementation cost：只改评测采集、输入映射、说明和对应回归测试；不改 proto、schema 或核心 Task Loop。
- Operational cost：沿用每题 records 文件，无数据库、后台任务或额外服务。
- New failure surface：事件只是有限的执行证据；缺失、失败或截断不能推断成功执行，旧记录不能冒充完整轨迹。
- Release slice：EV-DE；本次完成离线回归，线上付费重判另记验收状态。
- Decision：IMPLEMENT；既有评测缺陷明确，复用事件和 records 已足够。

验收：工具调用事实进入 records 和裁判上下文；两条判分路径使用相同字段；重连不重复计入；无证据、失败和截断有明确说明；correctness/relevancy 不混入工具轨迹；续跑兼容旧记录；不泄露原始参数或工具输出中的秘密；原有评测回归通过。

## 实现与验证

- 共用 SSE 跟踪器仅加一个默认无行为的观察点，deliverables 保存已去重的 `tool_status`，每题至多 100 条。复用原 records，不逐事件写盘；跟踪重新开始时从头重建。
- 白名单为 event_id、round_seq、node_id、step、tool、state、target；字符串复用 Runtime 脱敏，目标最多 160 字符。不保存参数、输出或错误正文。
- `tool_evidence` 映射为 DeepEval `context` / `SingleTurnParams.CONTEXT`，只有 honesty 使用；旧的 `context → retrieval_context` 映射保留。依据 [DeepEval test case](https://deepeval.com/docs/evaluation-test-cases) 与 [G-Eval](https://deepeval.com/docs/metrics-llm-evals)，并对仓库固定的 4.3.1 实测。
- honesty 允许主动纠错，明确 running/failed 不证明成功、succeeded 不证明测试通过；顶层 tool_loop/sub_agent 的进度事件不是完整执行轨迹。
- 8 条新增验收先运行全部失败，实现后全部通过。相关回归命令：`.venv/Scripts/python.exe -m pytest services/py-learning/tests/test_deliverable_evidence.py services/py-learning/tests/test_frameworks.py services/py-learning/tests/test_judging.py services/py-learning/tests/test_deepeval_metrics.py services/py-learning/tests/test_swebench.py services/py-learning/tests/test_swebench_telemetry.py services/py-learning/tests/test_terminalbench.py -q -p no:cacheprovider`；结果 **104 passed、3 failed、3 skipped**。
- 三个失败全部位于未修改的 Terminal-Bench grade 测试，产物报告 `PermissionError: [WinError 5]`（临时目录的 `.git/objects`）。在进程内加载 `3a47cfa` 的原始 swebench 模块后重跑该测试文件，得到相同 **3 failed、11 passed**；未扩展本节点修复范围。
- 单独通过 `uv run --no-config --no-project --with deepeval==4.3.1` 建临时框架环境，并在其存活期间设置 `AR_TEST_FRAMEWORKS_PYTHON` 指向该解释器，运行 `test_frameworks.py -k real_deepeval`：**2 passed**。本地假裁判检查真实提示词：honesty 有证据、correctness 无证据，无付费模型调用。Ragas 的可选真框架测试未运行。
- `git diff --check` 通过；独立只读评审未发现阻塞项。

### 验证发现的必要窄修复

- Failure：Windows 的评测桥子进程导入 DeepEval 时，`asyncio/_overlapped` 报 WinError 10106。
- Evidence：真实 4.3.1 的两条桥接测试稳定复现；现有环境白名单丢弃 SystemRoot。
- Minimum fix：`evals/frameworks.py` 的既有白名单保留 `SYSTEMROOT` / `SystemRoot`。
- Benefit：Windows 桥进程可以初始化 Winsock，真实框架两条测试通过。
- Performance cost：仅多传一个系统路径环境变量。
- Implementation cost：白名单一行及说明，不增加依赖。
- Operational cost：无新增配置。
- New failure surface：仅传 Windows 系统目录，不扩大应用凭据环境的可见范围。
- Release slice：EV-DE 的本地真框架验证。
- Decision：KEEP；直接复现的兼容问题，最小修复已验证。

## 剩余边界

线上八题需要新的输出目录重新运行再判分；旧记录不能补造真实轨迹，已有 scores 不会自动重判。本次未做部署或付费模型重判，不声称 honesty 分数已改善；题库 confirmed 状态与能力分发布规则不变。
