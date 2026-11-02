# 开放注册与普通用户的资源限制

> 状态（2026-11-02）：开放注册已上线；`tenant_busy` 已在 task-service 准入实现默认每租户 2 个未终态 Task，并用租户作用域锁保证并发提交不超额。Model Gateway 已实现 `tenant_policy.model_allowlist` 的空数组/白名单语义；默认免费策略仍需由部署侧发布受限的 allowlist。每日/月度模型预算、防滥用、付费策略和告警仍待完成。Go 验收测试见 `services/go-task-service/internal/taskapi/server_test.go`。

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

1. **每个租户每天、每月的模型花费上限**（最要紧）。现在只有每个 Task 的预算，Task 的个数不限，所以一个账号一天的花费没有上限。
   做法：租户策略加 `model_budget: {daily_usd, monthly_usd}`（schema 改动，走 S-01）；Model Gateway 按计量记录累计租户当天、
   当月的花费，超了拒绝新的模型调用（`tenant_budget_exceeded`，设计书附录 H.6 的“预算上限”）；task-service 在准入时先查，
   超了不收新 Task。免费账号的默认值建议每天 3 美元、每月 20 美元。
2. ~~**每个租户同时在跑的 Task 数**：task-service 准入时数本租户未结束的 Task，默认 2 个；超了 `tenant_busy`（应用提示稍后再试）。~~ 已完成；策略覆盖和线上告警仍属后续。
3. ~~**免费账号的模型白名单**：默认策略的 `model_allowlist` 去掉最贵的档（例如 claude-opus），只留 mid、small 与一个 strong。~~ **执行器已完成**：Model Gateway 按租户策略区分“字段缺失=不限制”和“空数组=全部拒绝”，并在候选模型路由前拒绝不在白名单的 Profile；默认免费策略的具体 Profile 列表仍由部署侧 seed 配置。
4. **防滥用**：coding-web 注册时要求平台账号满 30 天、有验证过的邮箱（GitHub 的 `created_at` 与 `/user/emails`）；
   运维的封禁名单（`CODING_BLOCKED_LOGINS`）；出事时一键回到名单制（重新设 `CODING_ALLOWED_LOGINS`）。
5. **付费或熟人账号**：给租户发布自己的租户策略（Registry），放宽上面各项；以后有了计费再按套餐出策略。
6. **兜底与告警**：模型供应商控制台设整月花费上限；Grafana 按租户的花费面板加告警（全平台当天花费超过阈值通知运维）。
