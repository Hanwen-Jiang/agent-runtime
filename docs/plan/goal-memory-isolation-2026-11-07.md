# 固定题库的 Memory 测试隔离修复

本项仍属Goal18真实验收。旧API Delete按产品“遗忘”语义写入租户级content_hash墓碑；因此换工作区或新memory_id不会让同内容复活。第一版隔离launcher复用租户并只清items，导致新固定版本首题278候选中274不存在、recall0。A立即停止，未把31秒当作MMR收益或模型失败结论。

第二轮/opt/evals/longmemeval-mmr-fixed-20261107原始产物保留；停止后，当前及前次可归属1879个ID均无残留，queue/items/lineage/reviews/pending均0。该轮新增费用0.000325美元，原隔离tenant累计0.061172美元，加最早试跑0.015291美元，总已知0.076463美元。

B已授权仅在唯一评测tenant evalmem20261106清理有完整journal归属的测试墓碑。launcher现在：

- 从本题及明确prior-run链收集submitted_ids和Delete返回的全部级联ID；其他tenant、非w-eval工作区、非法ID或循环链拒绝。
- 在服务正常API删除之后，以非超级用户、无BYPASSRLS、固定tenant、Serializable事务核实queue/items/lineage/reviews/pending皆空。
- 墓碑中存在任何未归属ID时整次拒绝，只有memory_id在上述集合中的测试墓碑才删除；不读取记忆正文、不改服务端forget规则。
- 每题前检查所有活跃状态与墓碑归零，之后再写本题；完整结束及可安全取消退出时再次清理并记录。保留audit/revision元数据作为证据，它们不进入检索；向量由原Memory Delete路径清理，真实MemoryService测试核实向量与谱系同时消失。

1041个已归属历史测试墓碑已按此路径实际清理，test-residual-check.json确认所有活跃状态/墓碑0，账本不动。旧journal仍原样，另有清理回执；不清除其它租户或未知数据。

真实MemoryService+SQLite/向量链路覆盖同内容跨题拒绝、精确清理后恢复、跨tenant同ID保护、去重存活ID和级联清理、未排空拒绝；结合原gRPC回归与launcher测试22项通过、0skip。下一轮仍原50题、seed、1800/1200/30秒和原门槛，新目录、固定版本；只在修复预检完成后执行，不拼接两个无效试跑结果，不扩大20美元授权。
