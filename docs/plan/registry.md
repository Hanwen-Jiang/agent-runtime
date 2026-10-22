# Registry（中档工作项细化，TMP 计划 T-08）

> 实施计划第 7 节阶段 1 的中档工作项“Registry”（中 · 中，没有强档评审项）。第 7 节说“细化另行编写”，这里是细化。
> 依据：设计书 3.1、3.1.2、4.1、4.3、附录 C.4、D.7、E.1、E.3、H.1、J.3–J.5；`proto/agentruntime/registry/v1/registry.proto`；
> `proto/agentruntime/workflow/v1/task.proto` 的 `PinnedVersions`。
> 状态：第 7 节的三个问题已经人确认（2026-10-22）。第 3 节的契约变更已写好 schema 与正反例、契约测试通过，**待人审**；人审之后再写 Registry 的验收测试。

## 1. 目标

集中存放可发布、带版本的配置，并让现在用文件或桩顶替的几处改读它：

| 现在 | 顶替的是 | 接上 Registry 之后 |
| --- | --- | --- |
| task-service 的 `FilePinResolver`（`AR_PINNED_VERSIONS_FILE`） | 提交时固定版本（3.1.2 第 2 条） | `ResolvePinnedVersions`：按当时的默认版本固定，写入工作流输入 |
| Worker 的 `resolve_catalog` 桩（模板一律 program · none） | Stage Runner 查 Node Template 的 executor、effect | 按 Task 固定的版本查 Node Template |
| Planner 的 `StaticContextSource`（开发组装里写死两个模板） | Node Catalog 与 Recipe 缓存（3.2.2、D.7） | `ListNodeTemplates`（按固定版本）、`ListRecipes`（按任务族） |
| Learning Pipeline 还没有发布出口 | 3.5.1 第 5 条、J.5 | `Publish` 以影子状态发布，`Promote` 带评测证据设为默认 |

## 2. 第一期范围

**做**

1. 存储：业务库（RelationalStore）一张表，只追加。按作用域分：Node Template、Model Profile、Reasoning Template 只在平台作用域；
   Recipe 两种作用域都有（平台发布的通用做法、从某租户的 trace 学来的做法）；租户策略只在该租户的作用域。读的时候由服务端合并
   平台作用域与请求租户的作用域，租户之间互不可见（S-12a 的隔离规则照旧由 RelationalStore 负责）。
2. 版本：同一作用域、同一种类、同一名字下按发布顺序编号 v1、v2……；新版本一律是影子（SHADOW）；`Promote` 把一个版本设为默认
   （DEFAULT），原来的默认变为退役（RETIRED）。已发布的内容永不修改；退役的版本仍可按版本号读到（已固定它的 Task 还要用）。
3. 名字冲突：租户作用域的 Recipe 不得与平台作用域的 Recipe 同名（Conflict）。否则同一个 `name@vN` 在不同租户指向不同内容，
   而且租户能借同名覆盖平台的做法。
4. 接口：实现 `RegistryService` 的全部方法（`GetTenantPolicy` 只读不改语义，见第 5 节）；租户过滤与分页由服务端做。
5. 固定：`ResolvePinnedVersions` 返回提交那一刻的默认版本：全部 Node Template、Reasoning Template、Model Profile，租户策略的当前版本；
   Recipe 按请求指定的，或按任务族取默认。task-service 配了 Registry 时用它代替 `FilePinResolver`；Registry 不可用时新 Task 的准入失败
   （fail-closed，3.1.2 关键规则）。
6. Worker：Python 客户端（grpcio）；`resolve_catalog` 改为按 `pinned` 查 Node Template；Planner 的上下文改由 Registry 提供 Node Catalog
   与 Recipe。没配 Registry 时仍用现在的桩（端到端测试的默认不变）。
7. 写权限：Registry 在 task-service 里，但用单独的内部端口（不经 Ingress，不和 Task API 共用）。读方法在内部网络里不再认证（与 Model Gateway
   一样，服务间认证留给部署层的 mTLS）；`Publish`、`Promote` 另要一个发布令牌（gRPC 元数据 `authorization: Bearer …`），令牌存在
   SecretStore 平台作用域，不进日志与错误信息。
8. 开发种子：`task-service registry-seed <文件>` 把一批文档发布并设为默认，供本地与端到端测试使用（例如评测集的 Catalog）。
9. 一致性测试：同一套验收测试分别跑在 SQLite（开发实现）与 Postgres（行级安全）上，Postgres 缺二进制时按 `AR_ALLOW_MISSING_TOOLS` 跳过。

**不做（登记到后续）**

- `expand_template`（Recipe 展开为子图）：要先有 Recipe 正文的子图约定落地并接进 Stage Runner，属于 S-04 的核心链路，单独排期。
- Model Gateway 改从 Registry 读 Model Profile、租户白名单（现在读文件）；Recipe 缓存随 Model Profile 更新而失效（3.1.2 第 4 条）。
- 租户策略的生效语义：收紧立即对运行中的 Task 生效、放宽只对新 Task 生效（3.1.2 第 2 条）；task-service 的 `FilePolicyResolver` 暂不换。
- 影子流量的分流与对比、J.3 的非劣效检验：`Promote` 只核对评测证据的格式与结论，检验本身由 Learning Pipeline 做（T-16、R-06）。
- Registry 不可用时运行中的 Task 用本地只读缓存（3.1.2 关键规则）：第一期 Worker 失败即按 transient 重试，缓存后补。

## 3. 契约变更（要经 S-01 流程）

`PublishRequest` 对所有种类都只带一个 JSON `document`，而 `/schemas` 里没有这几种文档的 schema；Node Template 的 proto 消息有强类型字段，
Registry 要从文档里取出这些字段。文档由 Learning Pipeline（Python）发布、由 Go 解析，是跨语言的契约，按设计书 4.1 应当放在 `/schemas`。
新增五个 schema（已经人确认；proto 不动）：

| 文件 | 内容（要点） |
| --- | --- |
| `node_template.schema.json` | description（给 Planner 看）；executor、effect、role（附录 A）；output（model 必填，其他执行者不带）；output_schema_id；params_schema；defaults（字段同 Atomic Node：budget、retry、cache、fallback、reasoning、quality）；implementation（model：tier 与 prompt；program：tool，不写时工具就是模板本身）；rollback_template（write_reversible 必填，其他 effect 不带）；supports_idempotency_lookup；eval_set。D.4、D.5、E.3 的规则同 Atomic Node：act 必须是写操作，写操作的默认配置不缓存，write_irreversible 的默认配置不重试，read 的缓存要 TTL，reasoning 只用于 generate |
| `recipe.schema.json` | task_family、description、risk（最高 effect）、typical_cost_usd；stages：Stage 的形状（goal、exit_when、budget、nodes，没有 seq）；tiers、context、budget 直接引用 `task_plan.schema.json` 的同名字段；hooks |
| `model_profile.schema.json` | 与 `deploy/dev/model-profiles.json` 的一条相同（附录 H.1 的子集）；id 形如 provider/model@version，等于发布时的名字；只收 go-model-gateway 的 `profiles` 包认识的字段；三项单价都必填（缺的会按 0 计、被选成最便宜的） |
| `tenant_policy.schema.json` | effect_ceiling、model_allowlist（Profile id，空数组表示一个都不允许）、hooks、budget（新 Task 的预算）；租户是作用域，不写在正文里 |
| `registry_evaluation.schema.json` | `Promote` 的评测证据：eval_set（名字与版本）、task_family、samples、baseline（第一次晋升时没有）与 candidate（成功率、可选的 95% 置信区间、每次成功成本）、thresholds（δ、ε）、passed、waiver（人工豁免：审批人与理由，带 waiver 时 passed 必须为 true）、report、evaluated_at |

与草案的差别：

- **Recipe 的节点不引用 `atomic_node.schema.json`**：Atomic Node 的写操作必须带幂等键，而幂等键要在实例化时按 task_id、node_id 生成，Recipe 里写不出来。Recipe 里的 atomic 节点只写 template 与本 Recipe 的配置（after、inputs、output_schema、budget、retry、cache、fallback、tier、reasoning、quality，字段定义引用 Atomic Node 的），executor、effect、role、output、idempotency_key 不准写：前四项取自引用的模板版本（已发布的版本不再改变），幂等键由代码生成。所以 D.4、D.5 依赖 effect 的规则在 Recipe 上由 Registry 发布时对照模板检查，schema 查不了。Recipe 的 Stage 里也可以有 Goal Node（例如 ci_fix 的 tool_loop），按 `goal_node.schema.json` 校验；template 模式展开只接受全是 Atomic Node 的 Recipe，留给 `expand_template`。
- **`defs.schema.json` 加了两项**（只加不改）：`model_capability`（generate、structured、tools、stream、embed；契约测试要求枚举只在 defs 里定义）与 `hook_set`（挂载点 → Hook 名的列表，Recipe 与租户策略共用，形状与 Worker 的 `HookCall` 一致）。
- Model Profile 的 class 与 Tier 名比 go-model-gateway 严：以字母开头、不含连字符（网关允许 `[a-z0-9][a-z0-9_-]*`）。这样拼出的 `<class>@<tier>` 一定符合 defs 的 `tier`；现有的 Profile 都符合。网关的规则以后收紧到一致。

JSON Schema 表达不了、由 Registry 发布时用代码检查的（写进第 5 节的验收）：Model Profile 的 id 等于发布名、Tier 名不重复；Node Template 的 rollback_template 指向存在的模板；Recipe 引用的模板版本存在、risk 不低于各节点模板的 effect、Recipe 节点的 cache 与 retry 对照模板的 effect 符合 D.4、Node id 唯一、after 引用存在且无环。

流程：强档写 schema 与正反例（`schemas/examples`）→ 跑契约测试 → 人审 → 再写 Registry 的验收测试。

## 4. 难点与防错

- **只追加**：`Publish` 永远新增一行，没有更新与删除的路径；同一版本号不会出现两次（唯一约束，并发发布时一个成功、一个 Conflict 后重试取下一个号）。
- **影子不外泄**：默认查询（不带版本）只返回 DEFAULT；影子版本只能按版本号显式读到；`ResolvePinnedVersions` 只固定 DEFAULT。
- **固定之后不受新发布影响**：按 `PinnedVersions` 查询时，新发布、新晋升都不改变结果；退役的版本照样读得到。
- **租户隔离**：租户作用域的条目只有该租户读得到；平台作用域的条目所有租户都读得到但改不了；查询条件由服务端拼，不来自请求的过滤参数以外的任何输入。
  缺租户的请求拒绝（与 S-02 的约定一致：denied）。
- **晋升要证据**：`Promote` 没有评测证据、证据不合 schema、或 `passed` 不是 true，一律拒绝，不改默认版本；证据原样存下，可追溯。
- **fail-closed**：Registry 不可用时，task-service 不接新 Task（transient，客户端重试）；Worker 的 `resolve_catalog` 不回退到“程序 · 无副作用”的桩
  （否则把写操作当成无副作用执行，等于绕过 effect 规则）。
- **令牌不外泄**：发布令牌只在 gRPC 元数据里，日志、错误、计量都不出现；令牌不对是 denied，不说明是缺了还是错了。
- **确定性**：Registry 的查询都在 activity 里；工作流只用写进输入的 `PinnedVersions`，重放不再查 Registry。

## 5. 验收（确认契约之后据此写测试）

- 存储与版本：发布编号、只追加、影子不外泄、晋升与退役、按版本读退役版本、并发发布不重号；SQLite 与 Postgres 各跑一遍。
- 作用域：平台条目所有租户可读、租户条目跨租户不可见、租户 Recipe 与平台同名被拒、缺租户拒绝。
- 晋升：没有证据、证据不合 schema、结论不通过时拒绝且默认版本不变；通过时返回原默认版本。
- 固定：`ResolvePinnedVersions` 只含 DEFAULT；之后的发布与晋升不改变按固定版本的查询结果。
- 写权限：没有令牌或令牌错误时 `Publish`、`Promote` 为 denied，读方法不受影响；令牌不出现在错误与日志里。
- task-service：配了 Registry 时新 Task 的工作流输入里是 Registry 给的固定版本；Registry 不可用时提交失败（transient），不启动工作流。
- Worker：`resolve_catalog` 按 `pinned` 返回模板的 executor、effect；Registry 不可用时 activity 失败（transient），不回退桩。
  Planner 的 Node Catalog 与 Recipe 来自 Registry。
- 端到端：用 `registry-seed` 种下模板，一个 Task 的 program 节点按 Registry 里登记的 effect 执行（例如登记为 write_reversible 的模板带上幂等键）。

## 6. 依赖

S-01（契约变更）、S-02（RelationalStore、SecretStore）、S-12a（行级安全的覆盖检查）、S-03（工作流输入里的 `PinnedVersions`）、S-09（Planner 的上下文接口）。

## 7. 人的决定（2026-10-22）

1. 第 3 节的契约变更：同意新增五个 schema。
2. 第 2 节的范围切分：`expand_template`、Model Gateway 改读 Registry、租户策略的生效语义放到后续。
3. 写权限用发布令牌（第 2 节第 7 条）作为 mTLS 之前的过渡。
