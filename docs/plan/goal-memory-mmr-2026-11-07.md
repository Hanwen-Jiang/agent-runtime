# LongMemEval Search 超时修复与有限重验

A按B确认的原Goal18故障修复执行；不扩大题集，不修改指标、seed、权限、相似度阈值或超时。

## 旧实验已终止并清理

原/opt/evals/longmemeval-isolated-20261106固定50题；实际完成1题、3题报TimeoutError，第4题停止请求后沿清理路径退出。此前“600秒外层”说明错误：实际manifest外层1800秒、pipeline1200秒、RPC30秒。后两题在pipeline_complete和删除哨兵后发生TimeoutError，指向最终Search；原journal不改写。

只发一次SIGINT，未强杀。最终process不存在、1042个已提交ID均查无剩余、memory_queue=0、pending model_usage=0；原清理遗漏30项经原tenant/workspace/已记录ID逐项Delete后再核验。旧实验实际0.060847美元，加最早试跑0.015291美元，累计0.076138美元。证据为原输出目录stop-cleanup-recovery.json；未删除原样本或把预占当实付。

## 等价优化

旧MMR在每个选择轮次，对每个候选重新计算它与所有已选项的余弦，重复已知候选对。改为缓存各候选的当前最大相似度；每轮只计算与新选项的相似度，O(N·k²·d)降为O(N·k·d)。

保留原_cosine逐元素求和、浮点运算、输入排序、精确匹配优先、阈值、同分先到先得、负相似度、负零、NaN max顺序和token预算。没有用近似向量、删候选或更换后端。

原算法对照：32固定随机seed×4种top_k，以及负值/零/NaN/异长向量、精确命中、0.9边界、同分、空集合；结合已有真实MemoryService检索回归57项通过。操作计数用250候选/top50断言余弦调用减少超过20倍，避免用机器负载决定测试绿红。

本机描述性性能：80×768/top20原2.566s、新0.270s，顺序摘要相同；250×3072/top50新9.418s、11025次余弦。这是纯选择CPU测量，不替代线上Search或50题验收；证据.goal-evidence/memory-mmr-performance-20261107.json。

## 下一次完整验收

B部署新Memory版本后A只开一个新输出目录，仍固定原50题/seed0/top50/k5、10、30，1800/1200/30秒和原验收门槛不变。旧结果不拼入新版本。复用evalmem20261106及原硬月/日19.984709上限，已有0.060847继续占用额度；不重新设置预算。launcher新增--prior-run，要求此前清理、停止、结算证据且账本精确匹配，阻止把未知/新增无归属费用洗成零。新run费用与累计tenant费用分别报告。三个launcher回归通过；新线上运行尚未开始。
