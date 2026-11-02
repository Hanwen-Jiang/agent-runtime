# memory-rag：记忆检索的评测集（Ragas，N-13）

`corpus.jsonl` 是记忆条目（kind 用 Memory Service 的分类：recipe、fact、success、preference），`questions.jsonl` 是问题、参考答案与
应当检索到的条目（`reference_ids`）。被测的是检索器：按问题取回 top-k 条，和参考的条目比——

- 自己算的（不要模型）：hit@k、recall@k、MRR；
- Ragas 的桥：离线时 non_llm_context_precision、non_llm_context_recall（字符串相似度）；给了判分模型时另算 context_precision、context_recall。

检索器：`lexical`（离线基线，只检查链路），或 `<模块>:<工厂函数>`（例如 Memory Service 的代码，见 agentruntime_learning.evals.rag）。
`confirmed: false` 的标注是起草的，要经人确认。
