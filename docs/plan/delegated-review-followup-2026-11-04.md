# 第二轮代审记录（2026-11-04）

本轮读取 Runtime `933c8f0b`、coding-agent `715223e` 的 main，两个仓库当时均无开放 PR。发布前另核对 Runtime 的 APP-LIVE 认领 `8712ff3f`、PREVIEW-1 测试 `e3b1bf4e` 与响应头修复 `9c077f99`。下列判定区分产品契约、具体协议字段和实现验收。上一篇记录保留当时的审阅事实。[首轮代审](delegated-review-2026-11-04.md)

## G-03-9　专项安全评审完成，暂缓凭据操作放行

修复 `2e924a3` 将带凭据的对账、push、sync fetch 移到独立的临时裸仓库，忽略全局和工作副本的 Git 配置。四组本地复验均堵住了原报告的 URL 重写路径。**尚需关闭的是 sync 中普通 Git 命令与带凭据传输共用同一个操作环境的问题。**

| 核对内容 | 证据与结论 |
| --- | --- |
| 原 URL 重写攻击 | 用虚构凭据、回环地址和仅写标记的 helper 测试。同一恶意配置放入直接访问的工作副本时，阳性对照确实写出标记。走 Runtime 的 mount/archive × push/sync 四条入口时，helper 均未执行，传输错误均未暴露虚构密码或编码后的值。原问题的这条路径通过。 |
| 同步预检 | `sync` 在同一个 lease 中先执行普通 `git status --porcelain`，再调用带凭据的 `fetch`。按预检使用的 Git 配置，在临时仓库配置一个无凭据、只写本地标记的 `core.fsmonitor`，`git status` 退出 0 且标记出现。证据证明仓库代码能在传输前的操作环境中运行。预检本身未收到授权头，后续授权头泄漏没有复现。 |
| 仓库自带回归 | 6 项中浅仓库兼容的 2 项通过。其余 4 项在测试域名无法解析时抛出 `TransientError(git_unavailable)`，先于防泄漏断言退出。应让夹具使用确定的本地失败端点或接住预期错误，使标记和脱敏断言在 CI 中真正执行。 |
| 放行条件 | 在上述预检确会运行仓库回调的条件下，证明它无法影响随后带凭据的传输。若当前 lease 内无法保证，就把不可信预检与持凭据传输隔离，再对 mount、archive 及目标真实后端复验。GitHub App 在 DEP-W 时保持关闭；`8712ff3f` 已另获上线授权并认领 APP-LIVE。此处给出凭据操作的技术放行意见，现有 OAuth 路径也在范围内。未核对线上启用结果。 |

证据位于 [sync 与持凭据 fetch](../../services/py-worker/src/agentruntime_worker/workspace/operations.py)、[仓库安全回归](../../services/py-worker/tests/test_workspace_git_config.py)。本轮使用本地测试后端，没有读取真实 Client secret 或 GitHub 令牌。

## 四份契约　代审通过下列语义，可据此编制 S-01 变更

| 工作项 | 通过的边界 | 具体接口的验收条件 |
| --- | --- | --- |
| N-14 用户 Skill | Registry 发布不可变版本。用户 Skill 只能扩展平台，Planner 获取名称、描述等目录信息，tool_loop 按需读取已选版本正文。用户目录和 D-27 配置包不能替代登记、授权或版本选择。 | 发布 v2 后，已固定 v1 的任务仍读 v1；用户同名条目不能遮蔽平台条目；非授权主体不能读取正文。当前 Registry 枚举尚无 Skill 类型。 |
| N-15 用户 MCP | tenant/user 是连接与工具的归属边界。Server、Tool、Effect 三层都须授权，effect 由受信登记方确定。SecretStore 提供引用，只向获授权的 HTTPS 目标注入。Gateway 管理会话，撤销后旧 lease 立即失效。 | 远程 HTTP 登记与调用需证明同租户另一用户被拒、server/tool 已登记但 effect 越权仍被拒、错 host 不发凭据、断开或撤销后旧 lease 不可再用。现有实现是部署侧 stdio。 |
| NET-OPEN 公网授权 | `deny/allowlist/open` 仍受租户策略与当次 Task 批准约束。租户显式允许 open 才可发 `scope=public`；旧授权无 scope 视为 hosts。拒绝 IP 字面量、非公网地址、云元数据、CGNAT、25 端口；DNS 使用已校验地址拨号，重定向重新准入，CONNECT 还校验 SNI。联网预算与撤销规则继续生效。 | public 不扩大凭据的目标范围。须在租户 `network.modes`、授权 scope、普通主机匹配和 CONNECT SNI 四处验证新旧客户端行为。当前网关仍按 grant.hosts 放行。保留已有推迟排期。 |
| D-25 多仓工作区 | 旧 root 省略 repo 继续有效。multi 创建时固定布局，仓库使用安全的单层子目录，读写操作明确选 repo。逐仓隔离凭据、主机授权和分支限制；推送幂等键包含 repo。部分 attach 失败保留已成功项，存档、复制、恢复和 replay 覆盖完整布局。 | 当前 Workspace RPC 尚无 AttachRepo/DetachRepo/ListRepos。提交 wire 时需明确目标仓有未提交改动时的 detach 行为；建议默认拒绝有损移除。已批准的 fixture 只证明其他仓不受影响。旧 root 后补远端按独立待定事项处理。 |

- N-14/N-15 的冻结依据是 Q70～73。Registry/Planner 相关 36 项通过；MCP 清单 3 项和现有 stdio Gateway 19 项通过。N-15 包列出 10 个候选正反例。相关通过数覆盖现有基础能力，用户远程 HTTP 的行为需在实现时验收。[人工基线](manual-acceptance-baseline-2026-11-03.md) · [Skill 与 Planner](feedback-2026-11-01.md) · [MCP 契约包](n15-n16-contract-baseline-2026-11-02.md)
- NET-OPEN 的环境契约清单 2 项、Worker 授权与 allowlist 2 项、netguard 包及网关行为 6 项通过。网关包在 Windows 默认优化编译时遇到编译器崩溃，关闭该包的优化后跑通。D-25 有 16 个候选正反例，2 项清单结构测试通过；实际多仓操作待交付。[联网与多仓设计](environment-config.md) · [网关准入](../../services/go-egress-gateway/internal/proxy/proxy.go) · [D-25 基线](d25-multirepo-contract-baseline.md)

## 本轮其他发现和排期

**[P2] EV-TB-2 会误报官方环境已使用。** `_environment_check` 只检查环境表中是否出现项目镜像名称。创建失败也保留镜像名。输入“官方镜像 FAILED”，或“官方镜像 FAILED + 通用镜像 READY”，都返回 `environment_used=True`。这会污染“使用官方环境”的解决率。复用成功创建的既有证据（如 `ready_at`），保留曾就绪后已销毁的有效记录，补这两条回归后再扩大到 40 题。现有环境判断的 7 项测试通过，均未覆盖这两个输入。[评测判定](../../services/py-learning/src/agentruntime_learning/benchmarks/swebench.py) · [环境记录](../../proto/agentruntime/environment/v1/environment.proto)

| 工作项 | 当前决定与下一次验收 |
| --- | --- |
| SBX-01 / WS-READ-01 | 一次交付已批准。草案 `42fe4fe` 经 `a8ffe95` 正式批准，breaking baseline 已更新。6 项 Sandbox 契约 + 350 项 schema 检查通过。本轮未重跑 Buf。进入实现验收，需保留 DISK-B、G-03-9 行为，验证身份隔离、取消和真实后端。 |
| R-06 | R-06-B 的基线零成功、缺失或未知成本、同步缺价行为通过 86 项定向回归。R-06-C 在本轮基线时已认领，待审每次 attempt 的价格与 usage 证据，以及 Learning 从权威账本读结算状态的后续接线。 |
| HIST-01-WEB | Runtime 四项游标、分页与参数测试，coding-agent 三项服务端及三项浏览器回归通过。批量加载、续传和回退在定向用例中通过，coding-agent 的 `715223e` 晚于已部署的 `b4bb9a9`。 |
| AUTH-03 / D-28 | 已明确维持当前身份服务，Rust 依赖清单已批准。D-28 进入实现，最终依赖和行为验收随实际 Cargo 提交检查。 |
| DELTA | 用户已定默认开启。当前有增量事件类型和免写业务库分支，仍待内部推送入口、调用方和租户授权、Worker 命名空间访问、Valkey 最多 10 分钟、禁入 Temporal/trace 及按租户关闭的行为测试。Valkey 最新决定 `60a625a` 已撤销 AOF/PVC。 |
| MX-Q / MX-PUB、EV-REAL | 改编题的机器标签保留 draft，人工抽查才进入能力分发布。EV-REAL 比较实际交付的规则策略与无子代理，开跑前锁定样本、质量判定和可接受成本倍数。以总表 §7.1 的最新逐项预算为准（MX-RUN ≤ $200，EV-REAL ≤ $40，EV-TB ≤ $80）。 |
| SITE-UI / D-23 | SITE-UI 等三页样稿或可点击样页再作视觉审阅。D-23 网络密钥的首个真实服务和客户端仍待选定，WEB-SEC 页面已有分开控制的功能开关。 |
| DEP-R / DEP-W / REAL-01 | 部署记录为 Runtime 第 24 版 `6cc16b8`、coding-web `b4bb9a9`。REAL-01 的真 Task、恢复演练、大型 ClaimCheck、真后端配额回退按 gate 逐项取证。Resend 密钥、域名验证和真实 Cloudflare 桥是外部交付项。PREVIEW-1 已改选免费证书覆盖的一级子域，预览上线改由下述 Cookie 隔离验收把关。 |

**PREVIEW-1 测试代审。** `e3b1bf4e` 的响应头测试通过审阅，可作为实现回归。在 `e3b1bf4e` 上该用例因转发 Domain 与后端设置的 `ar_preview` 失败；随后合入的 `9c077f99` 已修复响应头，本轮复验该用例通过。上线还需一个真实浏览器用例：环境页脚本能直接经 `document.cookie` 设置符合域匹配规则的父域 Cookie，而 coding-web 读取的会话 Cookie 名称为 `cw_token`。仅改写网关的 Set-Cookie 响应无法覆盖这个入口。应用应证明预览页无法影响平台身份 Cookie 的选择；本轮未复现跨站登录或令牌窃取。[网关测试](../../services/go-egress-gateway/internal/proxy/preview_test.go) · [浏览器 Cookie 域规则](https://developer.mozilla.org/en-US/docs/Web/API/Document/cookie)

本轮验证集中在上述契约、回归与本地复现。现场部署和付费运行沿用各自的执行记录。[部署记录](cloud-deploy.md) · [线上验收清单](cloud-deploy-gates-2026-11-02.md) · [计划总表](all-plans-dag-2026-11-03.md)
