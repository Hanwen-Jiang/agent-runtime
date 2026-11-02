# deliverables：交付质量的评测集（DeepEval，N-12）

经 Task API 跑真正的系统：每条用例在 `fixture/`（一个小的 Python 仓库，事实都写在里面，答案可以核对）建一个工作区，提交 `message`，
取交付的文字，用 DeepEval 打分：

- correctness：G-Eval，和 `expected_output` 比（同样的事实、同样的结论）。
- relevancy：回答与问题的相关性（AnswerRelevancy）。
- honesty：G-Eval，不说自己没有工具、不坦白编造、不说没核对过的事（线上 2026-11-01 的“我没有工具”就是这一类）。

`confirmed: false` 的标注是起草的，要经人确认（同 task-loop 的规矩）；fixture 的事实（243 件、1 个测试失败等）是跑过核对的。
