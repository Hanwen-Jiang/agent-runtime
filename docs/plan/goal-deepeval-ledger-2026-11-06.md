# DeepEval 被测调用的权威账本核验

针对实际旧记录中Task API/SSE费用与model_usage不一致或仍有pending的情况。只读审计，不结算、不退款、不覆盖旧记录、不发模型请求。基线7703ef10，独立worktree deepeval-ledger。

## 执行接口

新版本run完成后、付费score之前执行：

    PYTHONPATH=services/py-learning/src:services/py-worker/src:libs/py-core/src:proto/gen/python \
      python -m agentruntime_learning.evals.deepeval_ledger audit --out <新运行目录> --tenant evals

业务库连接从既有AR_RELATIONAL_DSN读取，不进入argv、证据或错误文本。采集复用benchmarks.paired.collect_ledger：PostgresRelationalStoreProvider绑定租户、read_only=True事务、task_id参数化条件。跨租户由现有RLS处理，不构造平台范围查询。

历史只读核查另给--evidence-dir <新的证据目录>，避免给原始实验添加文件。历史缺少冻结部署仍可核算已知金额，但不能升级成新验收。

## 证据和门槛

每个已选题必须对应唯一、终态Task；采集每个Task的全部model_usage尝试。保存reservation_id、task/node、实际费用和model profile的真实platform/vN/document SHA256。没有行或actual_usd_micros=NULL均是未知，不能视为免费。重复读取同一reservation只计一次；同ID矛盾、错Task、未选Task、未完成Task均拒绝有效结论。

实际模型必须在冻结subject_models中，Registry身份须与冻结model_profile文档一致。run.json与records摘要绑定证据；采集期间原记录变化就不发布证据。只写独立ledger-evidence目录：按内容摘要命名的不可变audit文件与原子更新的latest.json。重复审计同一事实不重复产生版本；pending后核账产生新事实时保留旧文件。

report每次从保存的尝试重新核验，不相信保存的valid=true。记录变化、摘要失配或证据缺失显示未就绪。新的frozen score在账本证据缺失、不完整或有pending时，在发起裁判模型之前失败。

## 费用字段与范围

- 旧cost_usd和costs.subject_usd为兼容保留，明确标记为Task API/SSE对照，不能作为权威结论。
- costs.authoritative_subject_usd来自完整、已结算的model_usage；不完整时为null，另报已知金额下界和pending reservation IDs。
- costs.total_usd仅在权威被测金额与裁判费用已知时计算，不用SSE金额填补。逐题保留差额，不改原记录。
- 范围是已选Task ID归属的Gateway尝试，裁判仍按独立裁判计量记录。没有Task ID的Memory嵌入、外部Jev及基础设施费未被查询覆盖，报告明确排除，不能宣称整租户或全部外部费用已核清。

## 验证

新增行为回归覆盖SSE低估不覆盖账本、幂等、pending阻止裁判支出、模型/修订/digest/scope不一致、完整Task集合、采集中记录变化、篡改summary不能假通过、历史产物不变及错误脱敏。

真实临时Postgres以NOSUPERUSER、NOBYPASSRLS角色执行：两租户种入同Task ID不同费用，evals只看到自己的0.08美元，不看到另一租户9美元；审计前后账本不变。完整Python CLI也在该库上运行。测试库及短命容器清理，没有接触生产账本写入。

日志和JUnit在/root/proj/agent-projs/.goal-evidence/deepeval-ledger-20261106。58条相关DeepEval/真实框架回归通过，0skip；真实Postgres及CLI单项另行通过（在非root短命容器中运行，主机那轮有1项deselected仅是将该项目分开执行，不是跳过）。新增模型费用0美元。真实新版本run/audit/score/report由主评测负责人在费用和部署冻结条件满足后执行。
