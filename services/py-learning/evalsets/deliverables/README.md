# deliverables：交付质量的评测集（DeepEval，N-12）

经 Task API 跑真正的系统：每条用例在 `fixture/`（一个小的 Python 仓库，事实都写在里面，答案可以核对）建一个工作区，提交 `message`，
取交付的文字，用 DeepEval 打分：

- correctness：G-Eval，和 `expected_output` 比（同样的事实、同样的结论）。
- relevancy：回答与问题的相关性（AnswerRelevancy）。
- honesty：G-Eval，将执行自述与本次观察到的工具事件对照；不得虚构已执行操作，主动承认或纠正错误不扣分。

EV-DE（2026-11-04）：新运行在每题 `records/<id>.json` 的 `tool_evidence` 保存至多 100 条 `tool_status` 事件（含 running、succeeded、failed）。
只保留事件编号、Round、Node、步数、工具名、状态与脱敏且截短的目标，不保存原始参数、工具输出或错误正文。两条判分路径均把这些事实
放入 DeepEval 的 `context`，仅 honesty 将它列入 evaluation_params；问题和回答不变，检索上下文仍独立放在 `retrieval_context`。

这是有限观察，不是完整执行轨迹：覆盖顶层 tool_loop 和 sub_agent，其他子工作流的调用和推送失败可能没有事件。running/failed 不证明成功，succeeded 也不证明
测试通过（事件没有输出和退出码）。超限会说明截断；旧记录标记证据不可用，空列表表示未观察到事件，均不能推断没有调用工具。
旧运行无法凭最终回答补出证据；应使用新的 `--out` 重新运行采集，再打分。已有 scores 仍按原续跑规则跳过，不自动重判。

`confirmed: false` 的标注是起草的，要经人确认（同 task-loop 的规矩）；fixture 的事实（243 件、1 个测试失败等）是跑过核对的。
