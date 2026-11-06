# LongMemEval 与页面验收冻结记录（2026-11-06）

本规则在读取或运行本轮真实 LongMemEval 结果之前冻结。主 Goal 的授权覆盖补测试与真实验收，旧 v1 暂停规则不适用。

## 固定实验

- 数据：既有 runner 的 LongMemEval_S cleaned 官方下载地址；不重新挑题。首次取回文件后记录完整文件 SHA-256，lexical/live 共用该文件。当前尚未取得数据，不伪造 digest。
- 按既有分层算法抽 50 个非拒答题，seed 0，top_k 50，k=5/10/30；原始题型全部按既有分层算法覆盖，拒答题另计。
- 线上检索器 `agentruntime_memory.eval_retriever:live_retriever`，Memory 使用已批准的 Milvus / `a6api/text-embedding-3-large@2026-10`。运行前登记实际 Runtime 提交、不可变 Registry 修订、Memory 镜像/配置版本，不以部署 SHA 冒充 Registry 修订。
- 输出目录固定配置、原始数据 digest、抽样题 ID、runner digest、评分版本；变更任一项必须新建目录。旧无 manifest 结果不自动合并；已经记录的付费失败/中断不自动重复收费。
- 本项既有批准上限为 $20（总 DAG 第 7.1 节）。先记录 embedding 估算，再检查剩余额度；实际费用从模型账本核验，不能把估算当成实付。可选 Ragas 不属于本次主检索指标，不重复花费。

## 预先固定的判断

主指标为会话级 recall_any@10。将原“明显高于 lexical”量化为：同一 50 题，live 相对 lexical 至少增加 0.10（5 个净命中），且对不一致对使用单侧精确 McNemar/binomial 检验，H0 为 live 无改善，p <= 0.05。

这是单个预先指定主指标，不在 k 或题型中选择最好的结果。报告会话/轮级 recall_any、recall_all、NDCG@5/10/30 与全部题型，其他指标仅描述，不用来替代主门槛。未显著改善就判未通过，不能通过剔除难题或改 seed 达标。

完整性门槛：50 个配对题全部完成，无 RPC/流水线超时，所有已尝试写入的候选有清理证据；缺任意一项为验收无效。模型无改进与环境失败分别报告。无证据的 cleanup 不算成功。

## 生命周期与恢复

运行前必须保存 `kubectl get deployment,pods -n agent-runtime -l app.kubernetes.io/component=memory -o json` 的完整 JSON 并通过 `AR_MEMORY_TOPOLOGY_EVIDENCE` 引用。验证期望/实际/更新/就绪/可用副本均为1，实际只有1个Running且Ready的Pod，无terminating Pod；`--memory-version` 必须是该快照里的实际镜像。证据digest写入manifest，实验期间协调方必须冻结部署、禁止rollout。服务锁仅进程内，缺少该单写者证据时拒绝写入，不能用哨兵推断跨Pod全局FIFO。

每题独立 evals 工作区。批量候选后追加无任务内容的唯一中性 FIFO 哨兵，确认流水线到达哨兵后删除它，再检索计分，避免最后一道原始内容被筛掉导致误判。候选被过滤或合并允许形成真实分数下降。

每次RPC接收写入之前，将工作区、已尝试候选ID和清理进度fsync到结果journal；进程在批量提交中断也留有恢复范围。输出目录使用操作系统文件锁，防止并发续跑重复付费，进程崩溃时锁自动释放。

RPC 均有截止时间。删除仅在已确认队列处理完毕时把 NOT_FOUND 视为已清理；其他错误有限重试并保留错误类型、工作区和候选 ID。不能确认队列完成时标记 cleanup incomplete 并停止后续题；保留恢复清单，不把“当前查不到”当作“之后不会写入”。人工/独立恢复后需要重新核实清理证据再开始新实验。

## 页面冒烟

复用 `test_eval_site_pages.py` 的真 dashboard_server 与 headless 浏览器。`AR_REQUIRE_BROWSER=1` 时缺浏览器直接失败，禁止 skip。`AR_EVAL_SITE_REPORTS` 可指向已冻结真实报告根，校验结果表确实来自报告，另验语言切换、表格展开、三页手机宽度；默认测试用明确标识的合成报告用于回归，不将其当真实评测产物。

本机已定位 Python `/opt/evals/venv-2d94680/bin/python` 与 uv `/opt/swebench/bin/uv`。当前找不到 Chrome/Chromium；kubectl 只读请求被执行沙箱拒绝 `socket: operation not permitted`。这些是实际验收阻塞，不是 Memory 内部认证错误；Task API 的 ops JWT 脚本位于 `/root/ar-release/ops-token.sh`，Memory 保持现有内部签名机制。

## 本轮验证与剩余限制

工作分支 `goal/memory-eval-20261106`，基线 Runtime `771efed7`；变更只涉及 LongMemEval、Memory 评测适配器及其测试、页面冒烟测试。

测试环境：`PYTHONDONTWRITEBYTECODE=1`，`PYTHONPATH=services/py-learning/src:services/py-memory/src:services/py-worker/src:libs/py-core/src:proto/gen/python`，Python 为 `/opt/evals/venv-2d94680/bin/python`，避免导入旧安装。跨模块执行 pytest 时显式 `-o asyncio_mode=auto`，因为根目录没有子模块 pytest 配置。

- `python -m pytest -o asyncio_mode=auto services/py-learning/tests/test_longmemeval.py services/py-memory/tests/test_live_retriever.py -k 'not writes_wait and not each_retriever and not signing_key' -q`：31 passed、3 deselected。31条包括完整LongMemEval回归、故障/拓扑边界，以及真实MemoryServicer+认证+SQLite/向量存储的进程内写入、隔离、排空与清理；进程内执行没有冒充线上TCP连接。
- 完整 `test_live_retriever.py` 的3条既有TCP gRPC用例已尝试执行，均在监听 `127.0.0.1:0` 时被运行沙箱以 `socket: Operation not permitted` 拒绝；保留原断言和用例，未改成skip。
- `AR_REQUIRE_BROWSER=1 AR_REQUIRE_REAL_REPORTS=1 AR_EVAL_SITE_REPORTS=/opt/evals/reports/20261102-1231 python -m pytest services/py-learning/tests/test_eval_site_pages.py -q`：6 errors，均为缺少真实Chrome/Chromium；0 skip。这个记录是验收未通过的证据，不能报告页面通过。
- `git diff --check` 通过。日志在 `/tmp/goal-memory-validation/`（offline.log、memory-all.log、browser-required.log）；原有真实评测产物未改动。

本轮未启动真实模型或embedding调用，实际费用 **$0**。LongMemEval真实数据下载、线上50题对照、实际费用账本核验及真实浏览器验收仍需具备网络/监听能力和浏览器的执行环境。当前实现报告费用未知时明确用null，绝不把估算填入实际费用。
