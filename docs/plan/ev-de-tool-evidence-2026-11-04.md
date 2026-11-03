# EV-DE：DeepEval 工具证据（2026-11-04）

状态：正在实现；开工前先提交并推送认领。沿用 main 提交的小阶段交付方式，不占用 CACHE-01、ATOK-T、PERF-00 的文件。

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
