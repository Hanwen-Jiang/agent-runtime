# LongMemEval 实物接续核验（2026-11-06）

本记录基于宿主权限恢复后的只读核验。本线未启动新的模型调用，未修改集群或原始评测产物。已有真实运行不能按“未运行”重启。

## 数据、固定抽样与 lexical

已有数据和结果位于 `/opt/evals/longmemeval-livecheck-20261107`，不重复下载或运行。官方文件 `longmemeval_s_cleaned.json`：277383467 字节，SHA-256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`，已与 Hugging Face 数据集 API 的 LFS oid 和大小核对一致。CDN ETag 为 Xet hash，不能将它当作内容 SHA-256。

源地址仍为既有 runner 中的 `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json`。500题中按既有分层抽样选50个非拒答题，seed 0、top_k 50、k 5/10/30。题型为 knowledge-update/multi-session 各9题，其余四型各8题，共12611轮记忆。数据/题ID清单和全量摘要在父目录 `.goal-evidence/memory-live-audit-20261106`。

官方数据存在重复 session ID 的不同日期记录，`69eb50aa` 已修为同一 session 内累计轮ID，保留全部内容和证据，未去掉难题。有效 lexical 是 `lexical-fixed`，50/50完成、无超时，主指标 session recall_any@10 = 0.96；初始 `lexical` 保留但不作为修复后基线。

运行前冻结的主门槛仍是 gain≥0.10 且单侧精确配对检验 p≤0.05。lexical=0.96 使可能的最大增益只有0.04；本轮不能达此预设增益门槛。不能看结果改成更宽松门槛；仍需完成固定50题live，以交付真实分组/NDCG、故障与费用结论，未将数学不可达当作无需执行。

## 已有付费尝试及清理对账

原尝试只提交第一题 cf22b7bf，随后因串行写入无法适配原300秒流水线期限而被 SIGINT 中断。已核实无存活的对应进程，`live` 没有完成的题目/summary；`live-long` 尚未开始。

只读数据库核验使用线上 Memory Pod 的正常业务账号，READ ONLY 事务，显式绑定 evals，账号非超级用户且无 BYPASSRLS。自原起始时间1793992614297毫秒起，339条记录均为 text-embedding-3-large 的 Embed 尝试，已结算费用 **$0.015291**，pending=0。该统计是共享evals租户的时间窗口，保守全部归入本项目，不能宣称每条都有任务ID的精确归属；Memory目前的EmbedRequest没有call.task_id。

原journal最后一条的 nested lifecycle显示 pipeline_complete=true、cleanup_complete=true：278个提交ID，274个删除成功、4个NOT_FOUND。新独立只读对账确认memory_queue为0、这278个ID在memory_items中全部不存在。原外层记录仍interrupted/cleanup_complete=false，因为CancelledError没有进入原异常分支，finally之后的最终append没有执行。原数据未修改；新代码补为清理后先持久化终态，再重新抛取消，不将取消当成功、不自动重跑。

费用上限仍为$20，扣此历史费用后保守余额 **$19.984709**。本次只读核验、回归和准备新增模型费用 **$0**。

## 实际线上配置与下一次执行前提

观察时 Memory/Gateway 镜像均为 `809cb41`。Memory为单Deployment单Ready Pod，generation24，Pod UID `bc75a4ea-d13c-4536-9e07-26a7339d297f`，与原快照一致。实际后端Milvus，embedding为 `a6api/text-embedding-3-large@2026-10`；Memory仍用内部HMAC身份，不使用Task API ops JWT。

Registry现有不可变 `platform/model_profile/a6api/text-embedding-3-large@2026-10@v1` 正文SHA-256 `ac61544d2cec07c76bdbeee2a68cb9e1a4e3f171076242374725a93a87028180`；输入单价$0.13/百万token，price_at=2026-10-31。Gateway当前ConfigMap的embedding正文相同，但旧文件没有registry_revisions元数据；新运行应等R-06版本部署/导出后验证实际来源，不用评测脚本Git SHA冒充运行时或Registry版本。

按既有字符/4估算，50题写入和一次查询约 **$0.8170695975**，最大单题约$0.01705324；此估计未包含等待哨兵的重复检索、流水线额外搜索或失败重试。旧第一题实际$0.015291提供吞吐/费用参考，不能当整个实验实际费用。

当前evals租户Gateway日限$100000/月限$1000000，**不是本项$20硬上限**。已有launch-live-long.py的每题前检查只是额外防护，不替代账本准入。推荐由部署负责人建立专用实验租户（例如evalmem20261106），仅允许已批准embedding模型，Gateway `model_budget.daily_usd=19.984709`、`monthly_usd=19.984709`；其他策略从当前已批准策略导出保留，不扩大权限。新factory支持 `AR_MEMORY_EVAL_TENANT`，所有Memory请求及HMAC身份都使用该租户，租户写入manifest防止续跑切换。

部署负责人发布并核验硬限额后，冻结Memory拓扑、实际镜像、模型Profile/Registry版本、查询配置与本轮评分源码。新的运行目录必须与旧 interrupted 输出分开，并保留旧费用。建议预先固定pipeline timeout=1200秒、每题timeout=1800秒（仅故障期限变更，指标和门槛不变）。执行时环境包含：

- `AR_MEMORY_ADDRESS`：当前Service地址（观察时10.43.4.45:50061，启动前重新取证）。
- `AR_MEMORY_TOPOLOGY_EVIDENCE`：刚取的Deployment+Pod JSON，单写者验证通过。
- `AR_MEMORY_EVAL_TENANT=evalmem20261106`、`AR_MEMORY_EVAL_WAIT_S=1200`。
- `AR_DEV_SIGNING_KEY`：从既有Secret读到进程环境，禁止打印或保存到报告。
- runner固定数据、`--limit 50 --seed 0 --top-k 50 --budget-usd 19.984709 --timeout-s 1800`及真实部署/Registry/Memory版本。

本地旧launcher因SIGINT遗留 `.memory-signing-key`，仅记录其存在，未读取内容输出；由原运行负责人确认后清理。实验结束按专用租户账本核算全部Embed尝试，队列排空且每个journal的ID清理核验后才可报告cleanup成功；未知账本或cleanup失败停止后续题。

## 代码与测试

独立分支 `goal/memory-live-20261106` 基于 `c3660e33`，复用已有69eb50aa与87e20acf，新增取消终态持久化和显式实验租户配置。未改公共proto/schema/工作流。

实际宿主执行 `test_longmemeval.py` 和 `test_live_retriever.py`：**39 passed、0 skipped**，含原先被socket权限阻挡的真实gRPC本地监听、隔离租户认证写入及清理、取消后费用不重放。日志 `.goal-evidence/memory-live-audit-20261106/regression.log`。

该39条回归及lexical结果不等于线上50题live已完成。当前尚需统一部署冻结、硬预算策略、live实验、最终账本及对照报告。

## 隔离实验已启动（22:43，UTC+3）

主集成负责人确认并发布专用租户后，本线作为唯一付费启动者执行。Registry tenant_policy/v1与Gateway文件白名单都只有已批准的embedding模型，日/月均为19.984709美元；Memory仍809cb41，Gateway为goal-9ef23021并开启requireRegistry，实际挂载不可变Profile ConfigMap。`scripts/run_longmemeval_live.py preflight`已核验上述状态、单Ready副本/实际imageID、真实Registry正文、专用租户零历史账单与空队列。

源策略和增量发布材料位于 `services/py-learning/experiment-plans/longmemeval-20261106`。Registry source-policy.json 是实际evals/v1，新增tenant仅更改模型白名单和额度，其余配额不变。gateway-configmap-patch.json先test旧值后replace，只增加专用tenant，避免覆盖并发修改。launcher自身不执行seed、ConfigMap修改或部署。

已启动进程 PID **1335794**（启动时刻1793994191.302 UTC epoch），独立session、stdin=/dev/null、日志直接持久写入 `/opt/evals/longmemeval-isolated-20261106/run.log`。完整argv、源码commit `b91db2eec2440b15dee8831b2f9801548ed9785d` 与预算估计见该目录process.json。工作目录为 `.worktrees/memory-live/agent-runtime`。单独的输出锁阻止重复启动；中断后必须核对原PID/账本/journal，不能因观察超时盲目重跑。

启动命令（已执行，不能重复新开一份）：

    PYTHONPATH=services/py-learning/src:services/py-memory/src:services/py-worker/src:libs/py-core/src:proto/gen/python \
      /opt/evals/venv-2d94680/bin/python scripts/run_longmemeval_live.py run \
      --out /opt/evals/longmemeval-isolated-20261106 \
      --memory-commit 809cb41f9059e67d33fbb4da8e77ef4c2ec22218 \
      --gateway-commit 9ef23021f618a5dfade58ce604fe3808c7b1eacf

Memory使用既有内部HMAC密钥在进程内签专用tenant的短期请求身份，**无需Task API ops JWT**；密钥不写仓库、结果或argv。实验已冻结50题seed0，复用lexical-fixed；每题1200秒pipeline、1800秒总期限，门槛不变。每题前重新读实际部署/CM/Registry、专用租户全量账本与空队列；存在pending、非embedding/其他Task流量、版本漂移或保守预留累计达到16.984709美元即拒绝下一题。Gateway的19.984709月度账本限制是硬准入底线，脚本另外按完整历史防止新周期重置预算。

首批实际R06证据已落 `first-batch-ledger.json`：11次Embed均为platform/v1和正文摘要ac61544d…28180，已结算$0.000237；当时2条为正在执行的预留，不能称最终unknown或失败，首题队列仍在处理。这证明本次真实调用写入了Registry身份，但不等于整个实验完成。

脚本结束时会写 ledger-after.json、cleanup.json（journal全部ID、数据库排空与不存在核验）、costs.json、comparison.json和report.md。完整live实际费用只取专用租户model_usage，再加历史0.015291；未知结算不填0。预设gate即使未通过也如实交付全部指标，退出4不能解释为模型请求未执行。

新增launcher离线边界回归2过：真实来源/策略失配、未知费用、跨任务流量与预算不足均阻止新写入。进程仍在运行，最终50题结果、成本和清理尚待完成。其他服务可部署，但Gateway/Memory及其配置快照在此实验期间须保持冻结。
