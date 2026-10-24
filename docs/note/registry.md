# Registry：版本、影子发布、晋升证据与固定版本

> 学习笔记，2026-10-24。对应代码：Go 的 Registry `services/go-task-service/internal/registry/`（`registry.go` 规则、`service.go` RPC、`store.go`、`validate.go`、
> `seed.go` 开发种子）；Worker 一侧 `services/py-worker/src/agentruntime_worker/registry/`（`client.py` GrpcRegistry、`catalog.py` 的 RegistryCatalog、
> RegistryContextSource、RegistryStagePolicy）；开发种子 `deploy/dev/registry-seed.json`。设计依据：设计书 3.1.2；细化 `docs/plan/registry.md`。

## 1. 存什么

| 种类 | 作用域 | 用在哪 |
| --- | --- | --- |
| Node Template | 平台 | Planner 的 Node Catalog；Stage Runner 执行前核对 executor、effect |
| Recipe | 平台或租户（同名时平台优先，租户发布与平台同名的是 Conflict） | Triage 的 light（按任务族命中）；声明 Hook |
| Model Profile | 平台 | 将来 Model Gateway 改读这里（现在读文件） |
| 租户策略 | 租户 | effect 上限、模型白名单、Hook、预算 |

文档按 `/schemas` 里对应的 JSON Schema 校验，另加 schema 表达不了的检查（`validate.go`）。业务库一张表 `registry_entries`，按作用域隔离由 RelationalStore 负责（Postgres 行级安全），
SQL 不写租户条件。

## 2. 版本与发布

```
Publish（带发布令牌）→ 新版本 vN，状态 SHADOW（影子），不影响任何 Task
Promote（带发布令牌 + 达标的评测证据，passed 必须是 true）→ 它变成 DEFAULT，原默认 RETIRED（仍可按版本号读）
租户策略没有影子：发布即成为当前版本
```

- 同一作用域、种类、名字下按发布顺序编号；document 一经发布不再修改。
- 发布令牌放在 SecretStore 平台作用域的 `registry/publish-token`；缺了、不对或没配置一律 denied，说明相同，令牌不进日志与错误。
- 读方法不认证（内部端口），但一律要租户：缺租户 denied。Registry 只开在单独的内部端口上，Task API 的端口上没有它。

## 3. 固定版本：运行中的 Task 不受发布影响

提交 Task 时 task-service 调 `ResolvePinnedVersions`：全部模板与 Model Profile 的默认版本、租户策略的当前版本、任务族恰好一个默认 Recipe 时固定它。
这份清单写进 Task 工作流的输入；之后按固定的版本列模板、取策略。所以 Learning Pipeline 晋升一个新 Recipe，只有之后提交的 Task 才用它。

**还有一个缺口**：真 Planner 每一轮列 Recipe 取的是当前默认版本，运行中的 Task 重规划时看得到新晋升的 Recipe。根子是还没有“全局 Registry 修订号”（要改 proto），已登记。

## 4. Worker 怎么用

- `resolve_catalog`（RegistryCatalog）：按 Task 固定的版本查模板的 executor、effect（`authoritative=True`），Stage Runner 据此核对 Atomic Node：
  executor 不符、effect 写低、模板没登记都拒绝（在审批与执行之前）。
- 真 Planner 的规划上下文（RegistryContextSource）：Node Catalog 与 Recipe 来自 Registry，effect 上限跟 Task State 的 constraints；工具另由 ToolDriver 的登记补上。
- `load_stage_policy`（RegistryStagePolicy）：Task 固定的租户策略与 Recipe 的 Hook，按“租户策略、Recipe”的顺序合并去重；固定的版本取不到不可重试地失败（按拒绝处理）。

## 5. 开发种子

`task-service registry-seed <文件>` 把一批文档发布并设为默认（不走令牌与证据，证据记 `{"source":"registry-seed"}`），与当前默认版本内容相同的跳过，重复运行幂等。
`scripts/dev_stack.py up` 启动前种 `deploy/dev/registry-seed.json`（桩 Planner 与真 Planner 用到的模板、全部开发 Model Profile），再按 `--tenant-effect` 种租户策略。

## 6. 面试速查

**Q：为什么新版本先做影子，而不是直接发布？**
A：发布和生效分开：影子版本可以先评测、对比，达标了才带着证据晋升为默认；没达标就留在影子状态，什么都不影响。回滚也简单：把原来的版本重新设为默认。

**Q：运行中的 Task 为什么必须固定版本？**
A：一个 Task 可能跑几天，中途换了模板或 Recipe，前后行为不一致，结果没法解释，也没法回放评测。所以提交时固定一份版本清单，整个 Task 都按它走。

**Q：晋升证据由谁算？Registry 怎么知道它是真的？**
A：证据由评测方（Learning Pipeline 或评测 harness）算，Registry 校验格式并要求 passed 为 true；它不重新算统计量，所以发布令牌要严格控制，
理想是 Learning 有自己的角色与令牌（已登记）。

**Q：租户自己的 Recipe 和平台的同名怎么办？**
A：平台优先，租户发布同名的直接 Conflict，避免租户的版本悄悄覆盖平台的。
