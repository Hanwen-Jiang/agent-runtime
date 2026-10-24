# 真模型端到端（强档工作项细化，TMP 计划 T-13）

> TMP 计划 T-13（强档 · 中）。依据：设计书 1.1“可恢复”、2.5 端到端流程、3.6 执行保障、附录 K.4；TMP 计划第 0 节第 1 条；
> 实施计划 S-03（杀 Worker）、S-07（故障注入）；T-05、T-06（Model Gateway）、T-08（Registry）、T-09（Policy Engine）、T-10（工具）、T-11b（进度事件），
> 以及 T-14 登记过来的“本地拉起整套的脚本”。
> 状态：验收测试已写，**待人确认**（第 3 节的决定与第 5 节的测试）。实现在确认之后。

## 1. 目标

TMP 计划第 0 节第 1 条：一个真实的任务从 Ingress 进来，经 Planner、Stage Runner、Node Executor、Reviewer，调真模型和真工具，最后交付；杀掉 Worker 后能恢复。
T-13 的“完成”：一份端到端日志；故障注入套件通过。

## 2. 第一期范围

1. **本地拉起整套**（`scripts/dev_stack.py`，只用标准库，同 `check.py`）：`up`、`down`、`status`、`token`、`kill <组件>`、`start <组件>`、`env`。
   组件：Temporal 开发服务、task-service（Registry 打开，用 `deploy/dev/registry-seed.json` 种下）、Model Gateway（`deploy/dev/model-profiles.json`；
   供应商 `openai_compat` 或离线的 `mock`）、Worker（`AR_PLANNER=model`、`AR_REVIEWER=model`、`AR_TOOL_LOOP=model`，都经网关；平台内工具的工作区根目录）。
   `up --json` 的最后一行是各组件的地址、pid、日志位置。密钥只从 `~/.agentruntime/model-provider.env`（或 `--provider-env`）读，经 `model-gateway dev-secret`
   的标准输入写进本次运行的开发 SecretStore，不进命令行参数、环境变量与输出。live 用例与人手动演示用同一个脚本。
2. **开发种子** `deploy/dev/registry-seed.json`：桩 Planner 与真 Planner 会用到的 Node Template（`stub_answer`、`stub_confirm`、`answer`、`rewrite`）、
   `deploy/dev/model-profiles.json` 里的 Model Profile。租户策略不在种子里：`dev_stack.py up --tenant-effect 租户=effect` 给每个租户种一份
   （同时写进开发租户策略文件，task-service 按它给 effect 上限）。mock 供应商用 `deploy/dev/mock-model-profiles.json`。
3. **真模型端到端用例**（`tests/e2e/live/`，只在 `AR_LIVE_MODEL_PROVIDER=1` 时跑）：样本任务“CI 修复”（`tests/e2e/live/workspaces/ci_rounding`：
   一个小 Python 包，标准库 unittest，金额四舍五入的错误让两条用例失败）。一条完整跑完；一条在 tool_loop 中途强杀 Worker，重启后跑完。
4. **故障注入套件经真 Model Gateway**：场景 1（model 节点执行中 Worker 崩溃）与场景 4（供应商持续失败触发切换）各加经 Go Model Gateway 的用例，上游是
   `e2e_support.fake_openai`（加上按模型失败与挂起请求），故障 Worker 设了 `AR_MODEL_GATEWAY_ADDRESS` 时用 `GrpcModelGateway` 代替替身。原有的 S-07 用例照旧。
5. **端到端日志**：live 用例把 SSE 事件、Task 历史的摘要（每个 activity 的类型、Node、attempt、结局）、工具调用、计量（去掉密钥）、Verdict 与成本写进
   `tests/e2e/.live-reports/<时间>/`（不提交）：`run.json` 与给人看的时间线 `run.md`。结论摘要写回 TMP 计划 T-13。

**已知要补**（写测试时读代码发现，实现时一起做）：

- 真 Planner 的 Node Catalog 里没有工具：`dev_planner` 的静态上下文与 `RegistryContextSource` 的 `catalog.tools` 都是空的（`planner/activities.py:88`、
  `registry/catalog.py:98`），提示里不列工具，`validate.py` 又按 `catalog.tools` 校验 Goal Node 的 `tools`，所以真 Planner 规划不出用 `repo_*`、`shell_run` 的 tool_loop。
  工具与 effect 要取自 ToolDriver 的登记（T-10 的 `ToolCatalog`）。
- 真 Planner 的 `risk_authorized` 写死为 read（`planner/activities.py:89`）；Task 的 effect 上限已经来自 Task State，这一条只影响 Recipe 命中，第一期不动。

## 3. 待人确认的决定

1. **live 用例不进 CI 与夜间任务**：花钱、有随机性。人手动跑（`AR_LIVE_MODEL_PROVIDER=1`），结果写进 TMP 计划。没设时整目录跳过（不是失败）。
2. **样本任务选 CI 修复**，不选批量发票归类：只用平台内工具，不需要外部系统；结果能独立核对（测试自己再跑一遍 unittest、核对测试文件没被改）。
3. **租户 effect 上限 write_reversible**（`repo_edit`、`shell_run` 都是 write_reversible）：不出现 write_irreversible，不需要人工审批；审批路径已由 S-05、T-09、D16 的用例覆盖。
4. **成本上限每条 live 用例 3 美元**（按网关计量的标价，不是 a6api 的实价）。用例结束按计量合计核对，超过即失败；Task 本身的 budget 用租户默认值（R-01 评审 2.2：
   `budget_check` 现在少算 tool_loop 的花费，不能指望它拦）。
5. **“故障注入套件换成真 Gateway 重跑”的范围**：全套 S-07 照旧用故障 Worker 跑通；场景 1、4 另加经真 Go Model Gateway 的用例，上游用可注入故障的假端点，不打真供应商。
   真供应商下的恢复只由 live 的“杀 Worker”用例证明。
6. **杀 Worker 的时机**：等 Task 的事件历史里出现至少一次完成的 `call_tool` 之后、Task 结束之前强杀；不追求“恰好在模型调用中途”（真模型的时间不可控）。
   重启后 Task 要完成，已完成的 activity 不重新调度，工具调用不重复完成。
7. **缓存命中率只记录、不判定**：日志里写每类调用的 `cache_read_tokens` 占输入的比例。R-03 的“history 滚动断点”据此再定。
8. **Registry 打开**（T-08 是 T-13 的依赖），用第 2 节第 2 条的开发种子。
9. **模型**：沿用 `deploy/dev/model-profiles.json`，不为 T-13 另配；按单价选档（R-02：第一期按单价）。

## 4. 难点与防错

- **密钥不外泄**：密钥只经 `dev-secret` 的标准输入；用例检查网关、Worker、task-service 的输出与日志文件都不含密钥（失败信息里不回显密钥）。
- **恢复后不重复**：已完成的 activity 不重新调度（`history.assert_clean` 的第 6 条）；每个 `call_tool` 至多完成一次；工作区的最终状态由测试独立核对。
- **真模型的随机性**：只断言结局与能独立核对的结果（unittest 通过、测试文件没改、事件历史干净、能回放），不断言 Stage 形状、Round 数或步数。
- **确定性**：真模型数据下的历史也要能用当前代码回放（Claim-Check 引用经共用的 ObjectStore 解开）。
- **租户**：计量全部记在 Task 的租户名下；工作区按租户与 workspace 划分。
- **rationale 不下传**：SSE 的任何载荷里都没有 `rationale`。
- **成本**：每条用例按计量核对上限（第 3 节第 4 条）。

## 5. 验收（测试已写，待人确认）

| 验收 | 测试 |
| --- | --- |
| 脚本拉起整套：`up --json` 给出五个组件（temporal、task-service、registry、model-gateway、worker）的地址与日志；`status` 看得到都在运行；健康检查通过 | `tests/e2e/test_dev_stack.py` |
| 经整套交付一个 Task（离线：mock 供应商、桩决策）：model 节点经网关，计量记在 Task 的租户名下；Task 固定的版本来自 Registry | 同上 |
| `env` 打印各组件的环境变量，不含密钥；`down` 停掉全部进程，再 `down` 不报错 | 同上 |
| 开发种子能被 `registry-seed` 载入（幂等），每份文档合各自的 schema | 同上 |
| 真 Planner 的 Node Catalog 带上 ToolDriver 登记的工具与 effect（静态上下文与 Registry 上下文都是） | `services/py-worker/tests/test_planner_tool_catalog.py` |
| 场景 1 经真网关：model 节点的上游请求挂起时杀 Worker，重启后交付一次；网关只有一条成功的计量；历史干净、能回放 | `tests/e2e/faults/test_fault_s1_model_node_crash.py` |
| 场景 4 经真网关：同档的一个上游模型持续失败，网关切到同档的另一个，activity 不重试；整档失败时 Node 有限次重试后失败、交给 Reviewer | `tests/e2e/faults/test_fault_s4_model_vendor_failover.py` |
| live：CI 修复从 Ingress 到交付：DONE；unittest 在工作区通过、测试文件没改；用到真 Planner、tool_loop、Reviewer 与 `repo_*`、`shell_run`；SSE 有进度事件、没有 rationale；计量都在租户名下、合计不超过上限；不泄露密钥；历史干净、能回放；写出端到端日志 | `tests/e2e/live/test_t13_ci_fix.py` |
| live：tool_loop 中途杀 Worker，重启后 DONE；已完成的 activity 不重调度、工具调用不重复完成；结果同上能独立核对 | 同上 |
