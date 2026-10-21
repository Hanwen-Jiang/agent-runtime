"""Context Builder（设计书 3.2.7、附录 G；计划“Context Builder 主体”、S-11）：Worker 内的库，为生成型模型调用组装上下文。

- partitions：五个分区按稳定性从前到后排（system、profile、brief、history、status），前三个分区末尾设提示缓存断点。
- modes：按工作模式（附录 G.3）区分必须精确保留的内容、遮蔽窗口与摘要重点；工作模式与阈值取自 Task Plan 的 context。
- builder：Compaction Ladder（offload → mask → prune → summarize → reset），阈值按模型的有效长度设；
  外置内容（完整工具输出）写进 ObjectStore，上下文里只留摘录与路径；精确状态由 Task State 注入 status 区。
- summary（S-11）：增量摘要：只把新截断的片段交给模型，模型给出固定分区（意图、决策、产物、未决、下一步）的增量，
  由代码合并进持久摘要，不整体重写。
- probe（S-11）：summarize 或 reset 之后的 Compaction Probe：从 Task State 与 Task Plan 抽 3 到 5 个关键事实提问并判分，
  答错就补回原文片段。
- tokens：token 估算。
"""
