# EV-DE 执行记录（2026-11-06）

状态：运行器补强与离线回归已实现；本轮新版本的线上 run、score、report 尚未执行，不能据此宣布 EV-DE 完成。独立 worktree 为 `.worktrees/deepeval/agent-runtime`，分支 `goal/deepeval-20261106`，起点 `1b79b883`。不改 Worker/Memory 认证或 Task Loop。

## 旧记录核对

只读核对 `/opt/evals/w1/deepeval-b`：八题全部 finished，均有真实、非空、未截断的 `tool_evidence`（分别 2–20 条，共 86 条）；不是缺少工具证据的最早一版。54 次裁判调用：correctness 14、honesty 16、relevancy 24；八条 honesty 最终评分提示包含工具证据，correctness/relevancy 没有混入工具证据。不能把旧目录的真实证据误报成缺失，也不能把其重新打分冒充本轮新运行。

旧版均分 correctness 0.8714、honesty 0.5875、relevancy 0.9043，全部指标通过 6/8。被测费用 $0.995772，裁判费用 $0.325152，合计 $1.320924。旧 run.json 没有完整版本冻结；全部八题标注仍为 unconfirmed。这些结果只能作为有局限的旧版本前后对照，不是同配置配对结论。本轮新增模型花费为 $0。

## 实现

- `evals/deliverables.py`：run 固定题目/标注、fixture、实际调用设置/预算、采集/评分代码摘要；可携带实际部署、Registry、Memory、模型、子 Agent 开关证据。改变其中任一项不能续跑；不能给旧运行补写新 provenance，不能把无 run.json 的旧产物当新运行。
- `evals/deepeval_acceptance.py`：冻结文件只接收明确的非敏感字段。Registry 必须是实际发布的 `(scope,kind,name,vN,document_sha256)`，不用 Profile 格式版本或部署 SHA 冒充。运行器校验格式并固定操作方提供的导出证据；该校验本身不声称已在线读取或验证部署。
- `tool_evidence` 绑定 Task ID 和 `task_api_sse` 来源。严格判分拒绝缺失、截断、空或不匹配的证据；保留旧记录兼容展示，标记 `legacy_unbound`。证据仍是有限工具状态，并非完整 stdout/退出码，不能从 succeeded 推断测试通过。
- Gateway 与桥各指标单独构建 test case，工具证据只传给引用它的 honesty 指标。Gateway 每题评分完成立即落盘，之后崩溃不会丢失此前已完成题目的评分。
- `evals/budget.py` 为共用的纯标准库预算适配器（提交 `360e365c`）：每次 Generate/Embed 在调用前收紧 `max_cost_usd_micros`，重试放在其外层，因此每次回退单独扣账；并发串行预留；同路径 OS 文件锁只允许一个 owner；调用前持久化 pending，未知费用或崩溃后须对账，不能盲目重试。恢复不允许扩大原上限或把已记录费用减小。已知未发出请求的拒绝带 0 计费标记，可嵌套总预算与单题预算。实际 Gateway 若违背最大费用契约，仍记录真实超额并停止后续请求，不能伪造费用回退。
- score 固定裁判、实际被测模型、证据摘要、评分代码、预算与框架版本；冻结验收限定真实安装的 DeepEval 4.3.1 与 Gateway 路径。report 分开报告被测/裁判/总费用、未知费用和证据状态，`acceptance_ready` 不把缺证据或缺费用当通过。`--compare` 只读旧报告，明确版本未知/不同不是同配置实验。

## 验证与限制

解释器可直接复用，不需下载工具链：测试 `/opt/evals/venv-2d94680/bin/python`；真实 DeepEval 4.3.1 `/opt/evals/venv-ff3a385/bin/python`（`venv-5b0b26a` 也有）。以下从本 worktree 根运行：

```sh
PYTHONPATH=services/py-learning/src:services/py-worker/src:libs/py-core/src:proto/gen/python \
AR_TEST_FRAMEWORKS_PYTHON=/opt/evals/venv-ff3a385/bin/python \
/opt/evals/venv-2d94680/bin/python -m pytest \
  services/py-learning/tests/test_eval_budget.py \
  services/py-learning/tests/test_deepeval_acceptance.py \
  services/py-learning/tests/test_deliverable_evidence.py \
  services/py-learning/tests/test_frameworks.py \
  services/py-learning/tests/test_judging.py \
  services/py-learning/tests/test_deepeval_metrics.py -q -p no:cacheprovider

PYTHONDONTWRITEBYTECODE=1 /opt/evals/venv-ff3a385/bin/python \
  services/py-learning/tests/deepeval_prompt_probe.py
```

完整套件结果为 **62 passed、2 failed、0 skipped**，记在 `/tmp/goal-deepeval-tests-20261106.log`。两条既有真实 DeepEval HTTP 桥测试在创建 `127.0.0.1` 监听 socket 时被当前执行沙箱以 `PermissionError: [Errno 1] Operation not permitted` 拒绝；显式设置了真框架环境，未跳过、未删改断言。其余回归通过，包括真实 Ragas 离线桥。额外的 `deepeval_prompt_probe.py` 用真实 DeepEval 4.3.1 与进程内确定性裁判验证两条实际提示：honesty 含工具证据，correctness 不含；不走网络、不调用付费模型，不代替线上模型验收。

## 线上接续

先由唯一部署负责人完成本轮相关版本部署并保存回退点，导出实际版本，生成只读的 `freeze.json`：`runtime_commit`、`coding_agent_commit` 为完整提交；`deployment_manifest_sha256`；`registry_revisions` 为真实发布对象的 scope/kind/name/version/document_sha256 数组；`memory_revision`、`memory_config_sha256`；`subject_models` 精确 Profile 列表；`sub_agents` 为 off 或 rules。代码开发与实验使用的冻结 checkout 分开。

本轮执行成本应以固定的当前模型单价与预算重算。历史 $1.320924 只是同八题的参考，不是当前模型的保证价格，也不是新增费用授权。`EVDE_SUBJECT_CAP`、`EVDE_TASK_CAP`、`EVDE_JUDGE_CAP` 必须来自已有授权额度；run.json 和 score-config.json 会保留并固定它们。预算是费用上限，不能拿上限代替实际账单。

Task API 令牌仍走 `AR_TASK_API_TOKEN` 的 Bearer 头；线上从 `/root/ar-release/ops-token.sh evals <评测用户> <有效期>` 取得 ops JWT，不能使用已关闭的 dev-token，也不能据 Task API 401 改 Memory 内部认证。JWT 不进入冻结文件、命令输出或报告。依照现有方式配置 Gateway factory，使用已批准的裁判 Profile。

```sh
"$EVDE_PY" -m agentruntime_learning.evals deepeval run \
  --out "$EVDE_OUT" --run-id "$EVDE_RUN_ID" --task-api "$EVDE_TASK_API" \
  --budget-usd "$EVDE_SUBJECT_CAP" --task-usd-limit "$EVDE_TASK_CAP" \
  --concurrency 2 --model-name "$EVDE_MODEL_LABEL" --freeze-config "$EVDE_FREEZE"
"$EVDE_PY" -m agentruntime_learning.evals deepeval score \
  --out "$EVDE_OUT" --judge gateway --judge-profile "$EVDE_JUDGE_PROFILE" \
  --judge-budget-usd "$EVDE_JUDGE_CAP" --require-tool-evidence
"$EVDE_PY" -m agentruntime_learning.evals deepeval report \
  --out "$EVDE_OUT" --compare /opt/evals/w1/deepeval-b
```

使用新的输出目录；旧目录不重写。核对八题实际 Task、工具轨迹、逐调用裁判记录与最终 model_usage 账单，记录实际部署和费用；旧缺来源/版本的结果不合并成同配置分数。测完确认临时工作区被 runner 清理；有 pending 费用时先按账本对账，不删除 budget-state.json 以重置额度。
