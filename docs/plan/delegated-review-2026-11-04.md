# 用户委托代审记录（2026-11-04）

> 用户委托审阅下列工作项，并明确要求将代审结论提交、推送。本文件记录该次代审意见，不把代理审阅写成独立人工或跨模型安全审计。
> 原审阅基线为 Runtime `fabe189556d313a3ee3f344bcd9226e4f64b2ee8`、coding-agent `28082cdce5a0bddc1048580d9b76f0112c621f25`。SEC-API 当时另审阅了未提交草稿，Git blob 为 `d1823528b46739d10b6842744cfd7520160c4c32`。
> 推送前核对基线为 Runtime `73cdd049`。第 1 节单列其他会话后续修复与部署记录，不将历史发现重新写成当前未修复，也不将记录核对当作修复复验。
> 范围包括六项契约、AUTH-03、D-28、SBX-01、三项付费运行、部署顺序、G-03 Client secret 与 R-06 判定规则。本次提交只落盘审阅记录与总表入口，不改实现、冻结设计书或生产配置，不执行付费运行。

## 1. 结论与推送前的状态差异

“通过”只适用于本文件明确列出的范围。“有条件通过”中的条件仍需关闭，不能据此将实现、真运行或部署验收直接标为完成。既有人工批准的基线继续有效。

| 项目 | 原审阅发现或结论 | 推送前记录核对 |
| --- | --- | --- |
| SEC-API | 草稿方向可接受，名称规则和删除后的拒绝语义需对齐 | 总表已记录契约 `56f21cd` 获人工批准，测试 `ae8efc3`、实现 `f3b02df`、错误映射 `971f0e1` 完成，尚未部署。本次旧草稿意见不撤销该批准，最终版本的验收以对应记录为准 |
| WS-READ-02 | 通过 `ebf963e` 的队列枚举增量 | 总表已记录人工批准，测试 `1c322b2`、实现 `71b9226` 完成。新 Worker 池部署和旧队列排空仍按发布记录执行 |
| G-03-9 | 原版本存在凭据被不可信 Git helper 读取的可复现路径 | 已记录测试 `8580117`、Runtime 修复 `2e924a3`，DEP-R 第 24 版清单包含 G-03-9。专门安全评审仍未完成，不能将原复现当作修复后的结果 |
| R-06 | 原 Profile 解析把缺失价格当零，历史价格证据不足 | 已记录测试 `f1b017b`、实现 `ccf29a5`，包括缺单价拒绝加载和计量 `price_at`。权威存储、usage 延迟、同步脚本缺价写零及完整版本证据仍需按第 7 节推进 |
| DEP-R / DEP-W | 同意 Runtime → coding-web → REAL-01 的顺序，需逐包验收 | 第 24 版 Runtime 已记录部署 `6cc16b8`。DEP-W 已由其他会话认领，记录有用户另行授权。本次不重开任务或覆盖其进度 |
| EV-TB | 5 题零通过后暂缓直接扩大到 40 题 | [原因记录](ev-tb-0of5-2026-11-04.md) 已确定环境配额导致回退 universal、未使用官方镜像，报告识别 `438d2bb` 已完成。配额与镜像仓库问题仍需关闭，再按第 5 节复验 |

部署状态来自版本化文档，本次未实时读取集群。总表行中的历史“没部署”注记与第 24 版清单不一致时，以具体部署记录为准。

## 2. 关键发现与复验边界

### G-03 凭据隔离

原审阅判定为 P1。`services/py-worker/src/agentruntime_worker/workspace/operations.py` 的 `_git` 把授权头经 `GIT_CONFIG_VALUE_*` 注入 Git 子进程，同时读取用户可写的 `.git/config`。安全复现仅在临时仓库使用虚构 `FAKE_FOR_TEST` 授权头、回环地址与仅输出布尔值的 helper。仓库 URL 重写加 `protocol.ext.allow` 能启动 helper，helper 读取到了模拟授权头，最终传输失败不阻止泄漏。没有使用真实凭据。

最小验收要求是带凭据的 Git 操作隔离不可信配置和可执行 helper，并覆盖已有配置入口的回归。修复 `2e924a3` 已由另一会话提交，本记录未对其重新执行上述复现。专门安全复验未关闭前，不能仅因 Client secret 已补齐就宣称 G-03 的安全验收通过。

### R-06 缺失价格与零成功基线

- 原审阅 P1 位于 `services/go-model-gateway/internal/profiles/profiles.go`，缺失数字字段会成为零。`ccf29a5` 已记录修复 Profile 加载边界；同步脚本与完整成本证据按第 7 节处理。
- 原审阅 P2 位于 `services/py-learning/src/agentruntime_learning/pipeline/evaluation.py`。纯函数复现使用候选 100 次全部成功、基线 100 次全部失败、每次成本 1，`non_inferiority(...).passed` 返回 true，因为基线 `cost_per_success` 为 `None` 后跳过成本比较。推送前静态核对该分支仍存在。建议最小修正为不可自动晋级，交人工判断；本次不改实现。

## 3. 契约逐项审阅

| 工作项 | 状态与代审结论 | 难点、防错与后续范围 |
| --- | --- | --- |
| SEC-API | 原草稿有条件通过，最终契约已另获人工批准 | Put/List/Delete、身份派生 tenant/user、只回元数据且不设 Get 的方向已处理。原草稿名称规则需与 `services/go-task-service/internal/workspaceapi/environment.go` 对齐，包含开头字符和 `..` 限制。删除 Secret 后引用它的请求必须拒绝，不能去头后转发。跨用户、跨租户、并发数量限制、日志脱敏以最终契约和实现测试为准 |
| NET-OPEN | 既定三模式通过，public 扩展具体 wire 尚不确定 | `open` 仍受租户策略和 Task 授权约束。public 范围须覆盖 DNS 实际地址与重定向，拒绝私网和元数据地址。凭据只向精确授权的 HTTPS 主机注入。现有推迟状态保持，本文不开放运行时公网权限 |
| D-25 | 保持已批准基线 | [多仓库基线](d25-multirepo-contract-baseline.md) 已获人工批准。保留旧 root 兼容、多仓 Attach/Detach/ListRepos、逐仓凭据与读写权限、路径及配额边界、部分 attach 成功保留。具体 RPC 尚需 S-01，不能将基线再次标为未批准 |
| N-14 | 保持已批准语义，具体 wire 尚不确定 | Registry 版本化条目、Planner 目录、tool_loop 按需读取可继续。用户命名空间不得覆盖平台定义，版本需固定。个人配置包 D-27 不替代 Registry Skill 契约，也不据此扩大为任意脚本包 |
| N-15 | 保持已批准基线，具体 wire 尚不确定 | [契约基线](n15-n16-contract-baseline-2026-11-02.md) 的 SecretStore 引用、Server/Tool/Effect 三层、tenant/user 归属、获授权 HTTPS 端点、人工确定 effect、Gateway 持有连接和撤销语义可继续。不得信任远端自报 effect；现有部署 stdio 配置不能当作用户远程 HTTP MCP 已实现 |
| WS-READ-02 | 本次枚举增量通过，现已有后续实现 | `ebf963e` 只增 `TASK_QUEUE_WORKSPACE=7` 与生成代码，兼容方向成立。先部署新消费者，再切生产者，旧 task/program 注册保留至旧操作排空。保持旧历史的命令序列，并按实现变更执行 replay 验证 |

原审阅执行了 WS-READ-02 的 Buf breaking、目标 proto lint，以及 proto 骨架、多仓库、MCP 相关共 38 项检查，均通过。这些是契约检查，不能替代后续实现和发布验收。

## 4. AUTH-03、D-28 与 SBX-01

### AUTH-03 选型决定

采用 [auth-tokens 第 5 节](auth-tokens-2026-11-03.md) 的现阶段建议。沿用 coding-agent 的登录及账号关联、Ed25519 短期 JWT、静态受信 issuer 公钥、公开 JWKS 和 Valkey 全局限流。出现第二个独立用户应用，或有明确企业 SAML/SCIM 需求时，再评估独立身份服务。

第 24 版只是包含 AUTH-01 代码，文档仍记录 `devTokens: accept` 且未配 issuers。部署切换按该文档第 4 节继续，本选型决定不等于生产切换完成。设计书 4.14 的权威原文按既有流程更新，本次不改导出副本。

### D-28 Rust 依赖

原则通过 `libc`、`serde`、`serde_json`、`sha2`、`tar` 的用途，使用成熟归档库。完整批准必须绑定实际 `Cargo.toml` 和 `Cargo.lock`，HTTP 库仍需选定，不能把本结论作为整个传递依赖图的批准。

按 [Firecracker 计划](firecracker-backend.md) 保留工具链与 crate 成熟期要求，执行 cargo-deny 的许可证、漏洞及重复依赖检查。行为验收覆盖编码、恒定时间鉴权比较、Go/Rust 正则语义、HTTP 断开后进程组取消、Linux/Windows 黑盒、归档双向互操作和 `/proc` 令牌隔离。Go 版按原计划保留一个发布周期。D-20 的 UDS/TLS 依赖另审。

### SBX-01 有条件通过一期方向

[SBX-01 草案](sbx-01-sandbox-service-2026-11-03.md) 的三期划分和一期读取活跃环境、只读挂卷、复用存档清单及块的方向可接受。接受的前提是最小修正以下三处，之后再进入一期契约与实现验收。

1. 无活跃环境的 Diff/Log 继续回退既有工作流路径，明确它不属于“不建沙箱”的一期文件读取快路径。不要为这两个方法提前搬入生命周期管理。
2. 第 7 节“唯一约束已经保证并存单写”不成立。`sandbox/ledger.py` 依靠租户 `sandbox_quota_lock` 与计数，同会话可共享环境。二期复用现有权威算法，切换前排空旧所有者，不能凭空假定唯一约束。
3. migration 201～210 不能整体移交。201～204、206、207 属于 Sandbox，205、208、210 属于 Workspace/archive，209 位于 `tools/deliver.py`，属于文件交付。一期迁移所有者保持现状，二期逐表明确责任。

一期按完整 tenant/workspace/session 授权，拒绝越界路径和符号链接。服务令牌采用 SecretStore 平台作用域、每调用方一个，校验其内部作用域。二期等待 PERF-00 部署后一周数据，设计书原文变更仍按草案规定处理。

## 5. 付费运行

| 项目 | 代审决定 | 启动和扩大条件 |
| --- | --- | --- |
| MX-RUN | 第一轮实验协议通过 | 沿用已接受的“不设活动总额上限、逐项限额并实时计费”。固定候选、数据版本、调用计划和估算。普通组件格子与 BFCL 先跑，高价模型只在既定 plan/judge 角色先 20×1，满足晋级条件再 40×3，E2E 只给优胜者做 10 题子集。限额阻止后续调用，不承诺已在途请求的最终账单绝不超额。MX-Q 未确认改编题不能发布为权威能力分 |
| EV-REAL | 配对设计通过，启动条件未齐 | 同题、同主模型、同价格版本，比较无子代理和规则子代理。启动前固定样本数、预算、质量改善与可接受成本倍数，并确认待测规则确已实现。指标含规划、拆分、DeepEval 正确性和全部实际成本 |
| EV-TB | 暂缓直接跑 40 题 | 已有 5 题 0/5、$5.29 记录。先关闭配额和官方镜像问题，同环境跑官方 oracle 单题，再跑 Runtime 同题，最后 5 题 smoke。只有环境与判分有效后才扩大样本。旧 5 题预算不自动覆盖 40 题 |

依据为 [矩阵计划](capability-matrix-evals.md)、[子代理计划](sub-agent.md) 和 [EV-TB 原因记录](ev-tb-0of5-2026-11-04.md)。历史估价不是当前供应商报价，本次没有发起付费调用。

## 6. 部署顺序与 G-03 Client secret

同意 DEP-R → DEP-W → REAL-01 的顺序，每一包固定镜像、chart、迁移与回滚点。

- DEP-R 已由另一会话记录完成第 24 版。本次不追认整个 main 均已部署或验收。后续包仍需核对 PG、数据层和应用侧 Valkey Secret、AUTH 切换、新增模型的 evals 租户范围，以及新增 RPC 的对应批准。
- DEP-W 已由另一会话实施。合 lane/d 时 `.env` 对齐 Runtime 的 16 KiB／64 变量，并执行组合验收。G-03 凭据隔离的专门复验不能被普通部署成功替代。
- REAL-01 按 [gate 清单](cloud-deploy-gates-2026-11-02.md) 记录真实结果。Kata、池与恢复、配额回退、数据库恢复和大型 ClaimCheck 需逐项证据，Cloudflare token 与付费预览证书仍按外部前置处理。

G-03 使用目标 **GitHub App 自身生成的 Client secret**，不能拿旧 OAuth App secret 或 App 私钥替代。经既有 SOPS 交付到 `agent-runtime` 命名空间的 `coding-oauth` Secret，键为 `CODING_GITHUB_APP_CLIENT_SECRET`。App 私钥单独按既有 `coding-github-app` 挂载方式管理。仓库和聊天不保存 secret 原值。本次未读取真实 secret，不能声称它已配置或可用。

## 7. R-06 成本来源、价格与门槛决定

本节明确 [已批准基线](r04-r06-secret-cost-contract-baseline-2026-11-03.md) 的待定事项，实施仍按原工作项和契约验收流程推进。

1. **权威存储**采用已有业务 PostgreSQL `model_usage`，按既有 attempt/reservation 标识去重，Gateway 结算，Learning 读取同一事实源。Task State 仅保存引用或摘要，不新增平行账本。
2. **价格版本**在每次 provider attempt 开始／预留时冻结。复用不可变 Registry 修订，现有记录保留可复算的实际费率、币种与单位、`price_at` 和 usage，包括缓存及实际发生的附加费用。仅 `price_at` 或可变 profile ID 不足以唯一标识历史价格。后续价格变化只影响新 attempt，不自创价格过期天数。
3. **未知价格**在调用前返回可分类错误，不按零计费，不自动重试或转用同样缺价的候选。明确验证免费允许零。`sync_model_profiles.py` 遇到应更新／加入的缺价项时报告错误并保留原文件，不写零或静默跳过；目录同步对未纳入的候选沿用既有显式过滤规则。
4. **usage 延迟**保留原预留额，实际金额为空，等待可验证的供应商账单再结算同一记录。明确未计费的失败释放预留，已计费的失败记实际费用。不设置自动超时退款，不新增轮询扫描服务。
5. **Learning 发布**含 pending/unknown 成本的比较不可自动晋级，不通过删除失败或高成本样本来绕过。保留现有两组各至少 100 样本、成功率容忍差 2 个百分点、每成功成本容忍增幅 5%、95% 置信界限。具体为候选成功率 Wilson 下界不低于基线观测值减 0.02，候选每成功成本的上界不高于基线的 1.05 倍。成本包含全部尝试与失败，基线零成功／成本无法定义时交人工判断。租户日月预算和实验质量门槛分别管理。

实现位置为 `libs/go-core/modelbudget/ledger.go`、`services/go-model-gateway/internal/gateway/budget.go`、`services/go-model-gateway/internal/profiles/profiles.go`、`scripts/sync_model_profiles.py` 和 `services/py-learning/src/agentruntime_learning/pipeline/evaluation.py`。

## 8. 风险、复杂度与验收边界

按实施计划 6.2，确定性风险集中在 WS-READ-02 的旧工作流 replay；并发风险集中在 Secret 数量约束、现有预算结算与 SBX 二期所有者交接；租户隔离风险集中在 Secret 主体、联网授权、Workspace 路径与成本账本范围。未给出证据的项目保持不确定，不因文档推送而标为已处理。SBX 的迁移归属和唯一约束描述属于已定位的草案事实错误，最小修正范围见第 4 节。

| 变更 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Git 凭据隔离复验 | 用户配置启动 helper 读取授权头 | 原基线虚构凭据复现成功，修复已提交 | 针对现有修复验证配置隔离，不增加旁路凭据系统 | 关闭实际泄漏路径 | 本次复验不增加运行时开销 | 定向回归测试 | 现有发布 gate | 过严限制可能阻断合法 Git 行为，需兼容验证 | G-03 上线验收 | KEEP |
| 复用账本并固定价格 | 缺价变零、历史无法复算、重复结算 | 原加载器与现有账本字段已核对 | 在现有结算记录补必要价格证据，缺价拒绝 | 成本可复算，门槛有效 | 同次数据库写入增加少量字段，不加远端读取 | 字段迁移、映射与定向测试 | 沿用现有数据库和保留策略 | 供应商账单差异需显式处置 | R-06 首版 | KEEP |
| 未知成本阻止自动晋级 | 缺 usage 或基线零成功绕过成本门槛 | 纯函数零成功复现与既有 nullable actual | 现有发布门禁加有限分类结果 | 防止无成本依据晋级 | 本地常数判断 | 小规模门禁测试 | 少数运行需人工判断 | 可晋级候选等待账单 | R-06 首版 | KEEP |
| 新账本、后台扫描、提前搬生命周期 | 并无新增在范围内的失败证据 | 当前状态和既有路径可覆盖首版 | 保留现有状态和一期只读边界 | 降低耦合及运维 | 无新增调用 | 不增加实现 | 无新服务 | 无新增机制 | 后续仅凭具体证据再议 | DEFER |

保留条件是上述具体失败仍存在；若对应凭据注入、可变价格来源或自动晋级路径被移除，则同步移除不再有用途的检查。评审不要求扩张到额外状态机、证明记录或周期性远端全量扫描。

本次落盘验证限于文档内容、引用与 diff。原审阅测试证据见第 2、3 节，未重跑全仓测试、修复后的安全复现、付费模型请求或生产集群验收。
