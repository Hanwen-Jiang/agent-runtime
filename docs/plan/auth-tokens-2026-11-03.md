# 登录令牌与 agent 令牌（2026-11-03 起草）

> 状态：**草案，等人确认。** 只记问题、依据与拟定的做法，不改代码。确认后按仓库流程：先写验收测试、停下等人确认、再实现；要改 `/proto`、`/schemas` 的走 S-01。
> 涉及两个仓库：签发在 coding-agent，校验在 Runtime（`libs/go-core` 的 Ingress、task-service）。本文放在 Runtime 仓库，coding-agent 的计划第 9 节指过来。

## 1. 现状（对照代码）

### 1.1 用户登录令牌

- coding-agent 登录后用 Runtime 的**开发实现** `libs/go-core/dev/localingress` 签发令牌：`base64url(JSON{t 租户, u 用户, exp, s 权限}).base64url(HMAC-SHA256)`。形似 JWT，但没有头部与算法字段，没有 `iss`、`aud`、`kid`、`jti`、`iat`。
- 令牌放在 Cookie `cw_token`（HttpOnly、SameSite=Lax、Secure 可配），有效期 12 小时（`internal/server/server.go`）；每个请求重新验签。改写操作要带 `X-Coding-Web` 头防 CSRF。
- coding-agent 把**同一个令牌**原样转给 task-service；task-service 也用 `localingress`、同一把 `AR_DEV_SIGNING_KEY` 校验（`cmd/task-service/main.go`）。
- 第三方平台的 OAuth 令牌按（租户、用户、平台）AES-256-GCM 加密存放，**密钥由同一把签名密钥派生**（`accounts.go` 的 `tokenKey`）。
- OAuth 登录有 state（HttpOnly Cookie，10 分钟），没有 PKCE。
- 退出登录只清 Cookie；限流在进程内（`localingress` 的滑动窗口）。

问题：

1. **一把对称密钥管所有事**：持有它的任何进程（coding-agent、task-service）都能给任何租户、用户、权限签令牌；它泄露，第三方平台的令牌也跟着能解开。
2. **撤销不了**：令牌被偷或用户被停用，最长 12 小时内仍然有效；没有刷新令牌。
3. **没有受众**：浏览器里的令牌直接就是 Runtime 的令牌，任何接受这把密钥的服务都认它。
4. **换不了密钥**：没有 `kid`，换密钥等于所有人登出。
5. 限流在进程内，多副本时失效（与 CACHE-01 同一个问题）。

### 1.2 agent 令牌

- 每个沙箱环境里的 sandbox-agent 只接受带令牌的请求（`/v1/exec`、`/v1/files`、`/v1/archive` 等，`go-sandbox-agent/internal/agent/agent.go` 的 `authed`）。令牌是 `HMAC(sandbox/token-key, env_id)`：每个环境一个，不存放，同一套配置的 Worker 都能算出来（`libs/py-core/.../adapters/k8s_sandbox.py`）。作用是**只有 Worker 能叫 agent 干活**，别的 Pod、别的环境连得上端口也用不了。
- K8s 后端经 Pod 的环境变量 `AGENT_TOKEN` 交给 agent；agent 读出后 `os.Unsetenv`，并且不把 `AGENT_*` 传给命令（有测试：命令里 `env AGENT_TOKEN` 拿不到）。
- **没核实的风险**：agent 与用户命令是同一个 uid（10001），`os.Unsetenv` 改的是 Go 进程里的副本，不改 `/proc/1/environ` 里的初始环境块；同 uid 的进程一般能读它。现有测试只查了子进程的环境，没查 `/proc/1/environ`。影响面有限：令牌按 env_id 派生，泄露的只是这个环境自己的 agent 令牌，拿到它能做的（在本环境执行、读写文件）用户代码本来就能做；但它违反 D-05 与 firecracker-backend.md D-20 写明的“令牌不放在非 agent 进程能读到的地方”，Firecracker 下 agent 是 PID 1，更要守住。

## 2. 工作项

| 工作项 | Failure / Evidence | Minimum fix | 成本与新失败面 | 当前决定 |
| --- | --- | --- | --- | --- |
| AUTH-01 非对称签名与密钥分离 | 第 1.1 节问题 1 | ① Runtime 加一个生产用的 Ingress 实现（`libs/go-core`，只用标准库的 `crypto/ed25519`）：只持公钥验签，令牌用标准 JWT 紧凑格式（头部 `alg: EdDSA`、`kid`），核对 `iss`、`aud`、`exp`、`nbf`；公钥按 `iss` 与 `kid` 配置（文件或 SecretStore），一个签发方可以配允许的租户范围。② coding-agent 持私钥签发，`iss` 是 coding-agent；task-service 不再持有能签令牌的东西。③ 第三方平台令牌的加密密钥改成独立的密钥（SecretStore），不再从签名密钥派生，旧数据读到时用旧密钥解开、用新密钥重新加密。④ `localingress` 留给开发与测试，生产模式（TS-PROD-01）下不许用 | 签发方（coding-agent）仍然能给它服务的所有租户签令牌，这是多租户应用的本分，非对称签名解决的是“校验方也能伪造”和“一把密钥管所有事”；迁移期间两种令牌并存，切换后旧令牌失效（用户重新登录一次） | 待确认 |
| AUTH-02 短期令牌、受众与可撤销的会话 | 第 1.1 节问题 2、3、4 | 浏览器 Cookie 里放**不透明的会话 id**（服务端会话记录：租户、用户、建立时间、最后活动、撤销标记），不再放 Runtime 令牌；coding-agent 调 Runtime 时按会话现签一个短期令牌（5 分钟，`aud` 为 agent-runtime，带 `jti`），在进程里缓存到快过期。退出登录、停用账号、管理员撤销都是删会话记录，立即生效。会话空闲与绝对有效期可配。密钥按 `kid` 轮换：新旧公钥并存一段时间 | 会话记录先放 coding-agent 现有的存储（JSON 文件，单实例）；coding-agent 改用 Postgres 时一起搬。SSE 长连接只在建立时验令牌：经 coding-agent 代理的流断线重连时换新令牌，不需要长令牌 | 待确认；依赖 AUTH-01 |
| AUTH-03 Ingress 的生产形态与身份服务 | 设计书 4.14 的网关产品“只定义接口”，至今是开发实现；多平台登录、账号关联都写在应用里；限流在进程内 | 评估身份服务（Keycloak、Ory、Zitadel 一类）接管用户、多平台登录与账号关联，或至少把签发独立成服务并发布 JWKS；网关（Envoy 系、Kong 或云厂商的）做 TLS、全局限流（经 Valkey，CACHE-01）；OAuth 加 PKCE | 多一个要运维的组件；账号数据要迁移；设计书 4.14 要由人更新 | 待确认；排在 AUTH-01、AUTH-02 之后 |
| AGENT-TOK-01 agent 令牌不让用户命令读到 | 第 1.2 节：`/proc/1/environ` 可能可读，未核实 | 先加测试：在环境里以命令的身份读 `/proc/1/environ`、`/proc/<agent>/environ`、`/proc/<agent>/cmdline`，断言找不到令牌（K8s 的 kind 测试与本地后端各一条）。修法任选其一并在测试里证明：agent 启动后 `prctl(PR_SET_DUMPABLE, 0)`（`/proc/<pid>/` 下的文件属主变成 root，同 uid 读不到）；或令牌改经文件或继承的管道交付、读完即删，环境变量里不出现。D-28（Rust 版 agent）按同一条验收实现 | 不改接口；`PR_SET_DUMPABLE` 也会挡住对 agent 的调试与 core dump，排障时要临时关掉 | 已完成（2026-11-03）：风险属实，修法前 Linux 上的命令从 `/proc/<agent>/environ` 读到了令牌，kind 的 Pod 里同样。修法是 agent 启动时 `PR_SET_DUMPABLE=0`（`69a9a30`，设不上时不起服务）。两条测试是 `cmd/sandbox-agent` 的 Go 测试（真起一个 agent 进程）与 kind 的那一条；本地后端不跑 agent，没有令牌，所以没有本地后端的那一条。Rust 版（D-28）按同一条验收 |

## 3. 顺序

1. AGENT-TOK-01 的测试：成本最低，先确认风险是否真实。
2. AUTH-01：改动集中，收益最大（拆掉“一把密钥管所有事”）。
3. AUTH-02。
4. AUTH-03：与 CACHE-01、TS-PROD-01 一起排。
