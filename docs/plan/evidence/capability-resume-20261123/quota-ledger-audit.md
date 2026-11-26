# 已拒绝的付费尝试如何归因

三份运行冻结时，共有 1,122 条带额度拒绝、没有成功调用记录的收据。独立对账在同一 PostgreSQL 只读可重复读事务中提取了评测租户 `evals` 的 1,182 笔 reservation。2026-11-23 04:08:05 UTC 的快照中，424 条收据能够各自匹配一笔不同的零结算记录。core 有 55 条分诊和 80 条 BFCL，transform 有 289 条 LongBench；judge-v2 没有通过一对一规则的记录。其他 698 条继续保留原状态。[无题号公开摘要](quota-ledger-summary.json) · [重算脚本](prove_zero_quota.py)

每条入选收据须属于冻结清单，携带原生额度拒绝类型，调用计数表为空，记录费用和本地预算的已花金额均为零，且原预算还处于 pending。候选、任务标识与节点必须匹配；直接评测还核对其原生单次调用记录。分诊与压缩的任务标识按原 fixture 取值。辅助调用或一次以上的被钉调用无法完全还原时，保留原收据。单凭账本金额零也不能推出“模型什么也没做”，因此同时要求匹配的结算行输入、输出、缓存 token 都明确为零。

按每条预算的永久锁文件 mtime 向下取整到毫秒，作为开始边界。原预算 JSON 的最终 mtime 向上取整，作为原始结束边界。账本行必须出现在这个原始边界内。保守复核窗口在开始前再留 1 秒，结束取原预算最后写入和收据 UTC 秒末两者较晚值再加 1 秒。在这个扩大窗口中检查该租户的全部账本行，只要有一行金额正数或待结算，就排除这个槽位。窗口内匹配角色、模型、任务和节点的 reservation 必须只有一笔，还要排除该笔 reservation 落在其他可竞争任务窗口的情况。候选模型相同、同一道题重复并发执行的评审任务，经常无法区分三笔收据的所有者。最后再核对所有入选 reservation ID 在三个运行之间全局唯一。

原机私有凭证目录是 `/opt/evals/deployments/capability-resume-20261123/quota-ledger-proof`。其中 `ledger-query.sql` 为查询原文，`ledger.jsonl` 为按 reservation 时间及 ID 排序的原始投影，`proof.json` 包含逐槽绑定，`public-summary.json` 仅包含计数和摘要。查询与底稿使用 SHA-256 绑定，公开摘要固定完整 proof 的 SHA-256。正式原件和各预算文件仍保存在对应只读停机快照中。

重算时在评测机使用 `python3 prove_zero_quota.py /opt/evals/deployments/capability-resume-20261123/quota-ledger-proof-rerun`，传入新目录。稍后的未知账单可能已完成结算，快照时间和事务编号也会变化。复验旧结论时，传入新输出目录，并追加 `--replay-ledger /opt/evals/deployments/capability-resume-20261123/quota-ledger-proof --expected-proof-sha256 870573ad8f98216258ffdea86a2d09736ae2f1a9e88f9f0c51280c091dc9d6b2`。实际复验得到完全相同的 proof 字节。全部这些步骤只生成对账证据。
