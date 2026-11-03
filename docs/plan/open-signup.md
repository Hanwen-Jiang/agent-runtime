# 开放注册与普通用户的资源限制

> 状态（2026-11-03）：开放注册已上线；`tenant_busy` 已在 task-service 准入实现（租户作用域锁保证并发提交不超额），2026-11-03 起默认不设上限、要限的租户按部署配置给（第 2 节第 2 条）。Model Gateway 已实现 `tenant_policy.model_allowlist` 的空数组/白名单语义；默认免费策略 seed 已加入 `deploy/dev/default-tenant-policies.json`，Helm 云配置同时下发每日 3 美元、每月 20 美元默认额度，生产仍需完成账本 enforcement。每日/月度模型预算双重准入、防滥用、付费策略和告警仍待完成。Go 验收测试见 `services/go-task-service/internal/taskapi/server_test.go`。

## 1. 现在的做法：平台默认的租户策略

开放注册后，新账号的租户（`gh-<登录名>`、`gl-<登录名>` 等）没有自己的租户策略。Registry 里有一份**平台默认的租户策略**
（不带租户、名字为 `default`，`internal/registry` 的 `DefaultTenantPolicy`）：没有自己策略的租户读到它，固定在 Task 上的
版本写成 `default/vN`；租户自己的策略总是优先（例如运维给某个账号单独放宽）。task-service 的 effect 上限与预算、Worker 的沙箱后端
与配额、联网授权都读同一份。

线上的默认策略（`/root/ar-secrets/default-policy-seed.json`，用 `task-service registry-seed` 发布）：

| 项 | 值 | 管什么 |
| --- | --- | --- |
| effect 上限 | write_reversible | 能改工作区、跑命令；不可逆的写（推送之外的外部副作用）不行 |
| 每个 Task 的预算 | 1 美元、50 次工具调用、20 分钟 | 单个 Task 失控时止损 |
| 沙箱后端 | kata（microVM），`min_isolation: microvm` | 陌生人的代码只在虚拟机里跑（cloud-deploy.md 第 4.2 节）；不回退到 gVisor |
| 沙箱配额 | 同时 2 个、每天 2 小时、出网 2 GB/天、包镜像未命中 5 GB/天 | 沙箱资源 |
| 联网 | 只放行 GitHub、GitLab、Gitee、Codeberg、Bitbucket，一次最长 60 分钟 | 克隆、推送、同步；Task 自己联网仍要逐次申请 |
| 工作区 | 最多 10 个、每个 1 GB、只能推 `agent/**` 分支 | 存储与远端仓库 |

## 2. 还缺的（按优先级）

1. ~~**每个租户每天、每月的模型花费上限**（最要紧）。现在只有每个 Task 的预算，Task 的个数不限，所以一个账号一天的花费没有上限。
   做法：租户策略加 `model_budget: {daily_usd, monthly_usd}`（schema 改动，走 S-01）；Model Gateway 按计量记录累计租户当天、
   当月的花费，超了拒绝新的模型调用（`tenant_budget_exceeded`，设计书附录 H.6 的“预算上限”）；task-service 在准入时先查，
   超了不收新 Task。免费账号的默认值建议每天 3 美元、每月 20 美元。~~ **已实现，随第 19 版（`b4f37e2`）上线**（2026-11-03）：策略字段、默认 3/20 美元与严格解析
   （MODEL-BUDGET-01，`ad0aa6a`、`61c8765`，Helm 默认额度 `187a372`）；task-service 与 Model Gateway 共用业务库里的 `model_usage` 记录，每次模型调用先原子预留、
   按实际用量结算，超了返回 `tenant_budget_exceeded`，不重试、不换便宜模型（MODEL-BUDGET-02，`377e1db`、`b2bd9f6`、`c5ee28c`，
   见 [model-budget-enforcement-2026-11-03.md](model-budget-enforcement-2026-11-03.md)）。与供应商账单的核对仍待做；网关启动时连不上业务库就退出的问题由 `93110a6` 修了，还没部署。
2. ~~**每个租户同时在跑的 Task 数**：task-service 准入时数本租户未结束的 Task，默认 2 个；超了 `tenant_busy`（应用提示稍后再试）。~~ 2026-11-03 人定**默认不设上限**：写死每个租户 2 个太死（rev19 上 SWE-bench 50 题被拒掉 42 题），不同类型的用户该有不同策略，免费账号先由第 1 条的模型预算兜住。准入检查留着（与插入同一事务），要限的租户由 task-service 的 `AR_TENANT_ACTIVE_TASKS` 给（Chart 的 `taskService.activeTaskLimits`，“租户=个数”）。按用户类型（免费、付费、内部）的策略写进租户策略的契约（S-01）和线上告警是后续。
3. ~~**免费账号的模型白名单**：默认策略的 `model_allowlist` 去掉最贵的档（例如 claude-opus），只留 mid、small 与一个 strong。~~ **执行器已完成**：Model Gateway 按租户策略区分“字段缺失=不限制”和“空数组=全部拒绝”，并在候选模型路由前拒绝不在白名单的 Profile；默认免费策略的具体 Profile 列表仍由部署侧 seed 配置。
4. **防滥用**：coding-web 注册时要求平台账号满 30 天、有验证过的邮箱（GitHub 的 `created_at` 与 `/user/emails`）；
   运维的封禁名单（`CODING_BLOCKED_LOGINS`）；出事时一键回到名单制（重新设 `CODING_ALLOWED_LOGINS`）。
5. **付费或熟人账号**：给租户发布自己的租户策略（Registry），放宽上面各项；以后有了计费再按套餐出策略。
6. **兜底与告警**：模型供应商控制台设整月花费上限；Grafana 按租户的花费面板加告警（全平台当天花费超过阈值通知运维）。

## 7. 2026-11-03 默认免费模型白名单切片

| 字段 | 决定 |
|---|---|
| Failure | 开放注册租户在部署未挂载策略文件时可选到高价 strong Profile，缺少默认费用边界。 |
| Evidence | Model Gateway 已支持白名单和 `default` 回退；`open-signup` 明确要求部署侧发布受限 allowlist。 |
| Minimum fix | 增加可审查的 `default-tenant-policies.json`，只引用现有 Profile；显式租户条目优先，未挂载文件仍保持兼容的不限制行为。 |
| Benefit | 默认免费策略排除 `gpt-5.5` 与 `claude-opus-5-5`，保留 small/mid、`gpt-6.1-sol` 和 embedding Profile。 |
| Performance cost | 每次 Gateway 调用读取一次已有策略文件，未增加远程调用或持久状态。 |
| Implementation cost | FileAllowlist 增加一个 default 分支、一个 seed 和静态部署测试。 |
| Operational cost | `values-cloud.yaml` 默认生成内置 ConfigMap；需要变更名单时可创建外部 ConfigMap 并设置 `modelGateway.tenantPoliciesConfigMap`，名单变更可通过文件更新生效。 |
| New failure surface | 默认 seed 引用不存在的 Profile 会导致对应 Tier 无可选模型；静态测试和部署前校验覆盖该错误。 |
| Release slice | 开放注册部署配置；不改变 proto/schema，不实现日/月预算。 |
| Decision | **KEEP**；先作为显式部署 seed 发布，生产 ConfigMap rollout 仍单独验收。 |
