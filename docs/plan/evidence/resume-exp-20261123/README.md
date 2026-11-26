# RAG 与工作区交付的岗位实测

| 场景 | 原结果 → 候选结果 | 实验条件与证据 |
|---|---|---|
| 代码引用能否直接打开正确的定义行 | 对 48 个文件只做重排，首条命中 1→15；再用 33 个不同文件比较显式符号召回，6→25，Top‑8 为 6→27 | 283 个真实源码文件，4,303 个语法块。查询按测试调用构造，三个题集的目标文件隔离。没有生成回答。[逐题实验](developer-rag/README.md) · [线上第一轮](https://evals.cgent.link/results?type=ablation#experiment-engineering-20261123-rag-citation) · [线上第二轮](https://evals.cgent.link/results?type=ablation#experiment-engineering-20261123-rag-lookup) |
| 多进程重试会不会同时轮询同一工作区操作 | 7 种场景各重复 4 次，验收 16/28→28/28；4 进程同键的并发查询峰值 4→1 | 真实 Go 进程、HTTP 和共享本机卷，模拟下游 Runtime。两版均创建 32 个预期项目。[逐组实验](../../../../../coding-agent/docs/plan/evidence/resume-exp-20261123/workspace-delivery/README.md) · [线上结果](https://evals.cgent.link/results?type=ablation#experiment-engineering-20261123-workspace) |

[求职简历表述](../../../../../8-legged/岗位与简历/README.md) · [代码知识库面试复盘](../../../../../8-legged/实验复盘/开发者RAG-代码定义与可验证引用-20261123.md) · [工作区幂等面试复盘](../../../../../8-legged/实验复盘/CodingAgent多副本工作区幂等-20261123.md)
