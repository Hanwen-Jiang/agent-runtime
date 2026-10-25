# 在线多租户 coding agent：MVP 计划（v0.2.0-alpha）

> 2026-10-24。参考 [Aster Code](../reference/aster-project.md) 第 3 节与 [在本 Runtime 上重写 Aster Code](../reference/runtime-products-and-rag.md) 第 2 节。
> 用户要求：越快越好，第一版不求严丝合缝，先出能用的成品，带前端；先实现、后补测（2026-10-24 经人确认，暂不走 CLAUDE.md 的“先写验收测试、停下等确认”）。
> 代码放在本仓库 `/products/coding`（2026-10-24 经人确认）。Runtime 一侧只做 MVP 必需的最小改动。

## 1. 第一版做什么

用户在网页上登录（开发登录：租户 + 用户名），新建项目（Git 地址，或内置的示例仓库），用一句话下任务（“修好 CI”“加一个接口”）。任务在 Runtime 上跑：Planner 规划、tool_loop 读代码、改代码、在 Docker 沙箱里跑测试、Reviewer 判定。网页实时显示进度时间线，遇到审批弹出确认卡片；跑完看改动（diff），选择提交、下载补丁或丢弃。

不做（第二版再说）：GitHub 登录与推送、开 PR、token 级流式输出、多人协作、计费、云端 microVM、编辑器插件。

## 2. 架构

```text
浏览器（单页应用，原生 JS，无构建步骤）
  │  HTTPS + Cookie（HttpOnly，里面是 Runtime 的令牌）
  ▼
coding-web（Go，products/coding，只用标准库 + 本仓库的 go-core）
  ├─ 开发登录：用 AR_DEV_SIGNING_KEY 签发 Runtime 令牌（libs/go-core/dev/localingress）
  ├─ 项目：<工作区根>/<租户>/<workspace> 下的 Git 克隆；元数据存 JSON 文件
  ├─ 任务：转发到 Task API（SubmitTask、CancelTask、SignalTask、QueryTask，connect 的 HTTP + JSON）
  ├─ 进度：代理 Task API 的 SSE（浏览器的 EventSource 不能带 Authorization 头）
  └─ 改动：git diff、提交、下载补丁、丢弃
  ▼
task-service（Task API、Session 与 Task 工作流）→ Worker（Planner、tool_loop、Reviewer）
  └─ ToolDriver：repo_read、repo_list、repo_grep、repo_edit 在宿主机上读写工作区；
     shell_run 在一次性 Docker 容器里跑（不联网、限 CPU 与内存），工作区挂载到 /workspace
```

每个项目一个 Session：同一项目的写任务由 Session 工作流串行，不会互相踩。

## 3. 与 Aster Code 的对应

| Aster Code | MVP |
| --- | --- |
| C1 客户端与 Coding API | 网页 + coding-web 的 JSON API；事件经 SSE 推送 |
| C2 任务生命周期 | 提交前记下基线 commit；任务状态就是 Runtime 的 Task 状态 |
| C3 角色与 Playbook | 先不分角色，用 Runtime 现有的 Planner 与 tool_loop |
| C4 Workspace 与范围 | 项目 = Git 克隆；不做快照与分支管理 |
| C5 检索 | 用现有的 repo_grep，不建向量索引 |
| C6 PatchSet、提交与 Apply | 改动直接落在工作区；网页上看 diff，选择提交、下载补丁或丢弃 |
| C7 验证与执行 | shell_run 在 Docker 沙箱里跑 |
| C8 编辑器桥接 | 不做 |
| C9 发布与云端交接 | 不做（第二版：GitHub App 推送到 agent/* 分支并开草稿 PR） |
| C10 技能与 MCP | 不做 |
| C11 账户与计量 | 开发登录；用量取 Task 的 usage 显示 |

## 4. 步骤

| 步骤 | 内容 | 完成标志 |
| --- | --- | --- |
| P1 | coding-web 服务：登录、项目、任务、SSE 代理、diff 与提交 | `go build` 通过；用 mock 模型能下任务并看到 task_finished |
| P2 | 网页：登录页、项目列表、任务面板（输入框、时间线、审批卡片）、改动页（diff、提交、下载、丢弃）、文件浏览 | 在浏览器里走通一遍 |
| P3 | Docker 沙箱：Worker 设 `AR_TOOL_SHELL_SANDBOX=docker` 时 shell_run 在容器里跑 | 示例仓库的 `python -m unittest` 在容器里跑通 |
| P4 | `scripts/dev_stack.py up --coding`：编译并拉起 coding-web，工作区与 Worker 共用 | 一条命令起整套，打开网页即可用 |
| P5 | 冒烟测试 + 发 v0.2.0-alpha 预发行 | Go 单测与一条端到端冒烟通过 |

**进度（2026-10-24）**：P1–P4 完成；P5 的冒烟测试完成（`products/coding` 的 Go 测试、Worker 沙箱配置测试），并用真模型从网页走通一遍：示例仓库的“修好 CI”任务 Agent 改用 `Decimal` + `ROUND_HALF_UP` 修好了 pricing.py，Reviewer 核对 5 条测试全部通过后判 done，两次 `python -m unittest` 都在 Docker 沙箱里跑；12 次模型调用，按计量约 0.10 美元。网页上看进度、看 diff、提交都验证过。

## 5. 安全底线（第一版也要有）

- 租户只来自登录得到的令牌；项目、任务都按租户隔离，路径一律校验在工作区里。
- 开发登录只在 `CODING_DEV_LOGIN=1` 时打开，页面明确标出“测试用”。
- 沙箱：一次性容器、`--network none`、CPU、内存与进程数上限，只挂载本项目的工作区。
- Git 克隆只接受 https 地址，禁用 hooks；不把凭据写进地址。

## 6. 第二版（v0.3）：GitHub 登录与推送

2026-10-25 用户定：第二版先做 GitHub 登录与推送。

- **登录**：GitHub OAuth App 替换开发登录（开发登录只在 `CODING_DEV_LOGIN=1` 时留着，供离线测试）。租户是 `gh-<GitHub 登录名>`（个人就是只有一个成员的租户，同 Aster Code 云端版），用户是登录名；开发登录不许用 `gh-` 开头的租户名，免得冒充。
- **令牌**：OAuth 令牌只存在服务端，按租户、用户加密（AES-256-GCM，密钥由签名密钥派生），不进浏览器、日志、Git 配置与地址。Git 操作用环境变量传 `http.extraheader`，命令行参数里也没有令牌。
- **项目**：可以从“我的仓库”里选；私有仓库用令牌克隆。记下 `owner/repo` 与基线分支。
- **推送与 PR**：由人点按钮触发（模型提议、人来应用）。没有分支时新建 `agent/<描述>-<随机>`，提交未提交的改动，只推这个分支（引用由服务端拼，不能推到别的分支，不强推），再用 GitHub API 开草稿 PR；之后再推就更新同一个 PR。“回到基线分支”拉取最新的基线、清掉分支与 PR 记录。
- **回调地址**：OAuth App 的回调地址要固定，`dev_stack` 给 coding-web 固定端口 8095，回调是 `http://127.0.0.1:8095/api/auth/github/callback`；Client ID 与 Secret 放在 `~/.agentruntime/github-oauth.env`（仓库外，不提交）。

之后：token 与工具状态的流式输出（需要 Runtime 的 R8）；按 CLAUDE.md 补齐验收测试；每个任务一个分支与 PatchSet；GitHub App（按仓库授权、短期令牌）替换 OAuth App；配额与计费；编辑器插件。

## 7. 界面 v0.4：参照成熟的 agent 界面重做

2026-10-25 用户要求：风格向 Claude 的略带复古、写实的风格靠；输入框随对话阶段变化；输入框下放沙箱等级、模型倾向、已用 token；最右侧放环境、上下文、输出、改动、后台任务的状态。

### 7.1 参照

| 产品 | 借鉴什么 |
| --- | --- |
| Claude（网页、桌面） | 暖色纸面底色、陶土色强调、衬线体的标题与回答、无气泡的助手正文；输入框是一张卡片，下沿一排工具，右下角是模型选择与发送，运行时发送变成停止；产物在右侧面板打开 |
| Claude Code（桌面、网页） | 会话右侧的面板（改动、终端、任务、计划、PR）；权限模式；状态行里的上下文占用与花费；运行时可以继续输入、排队；Esc 中断；后台任务；收尾给出下一步 |
| Codex（网页、CLI） | 沙箱等级：只读、工作区可写、完全访问，加审批策略；任务列表与状态；先审改动再开 PR |
| Cursor Agent | 输入框里的模式与模型选择、上下文占用的圆环、排队的消息、待办清单、后台 agent 列表 |
| Devin、Jules、Copilot coding agent | 工作区面板（终端、编辑器、计划）；执行前确认计划；产出是 PR |
| Windsurf、Cline | 计划与执行的切换；每个任务的 token 与花费；检查点 |

### 7.2 定下的做法

- **风格**：纸面底色（浅色 `#FAF9F5`，深色 `#262624`），陶土色强调（`#C96442`），标题与 Agent 的回答用衬线体，界面控件用无衬线体；细线边框、少阴影；跟随系统的深浅色。不用 Anthropic 的标志。
- **布局**：三栏。左栏是项目（进行中的项目带标记）；中间是对话（正文最宽 760px）；右栏是状态（约 340px，可收起；窄屏变成抽屉）。改动的 diff、文件、提交历史在一个宽抽屉里打开，不挤在右栏。
- **输入框随阶段变化**：
  - 空闲：描述任务，Enter 发送。
  - 运行中：上沿显示当前一步、轮次与用时，带“停止”（Esc 也行）；还能继续输入，发出去的排在当前任务之后（同一 Session 串行）。
  - 等人回复：输入框变成回复卡片，问题、选项按钮与补充说明都在这里，不用回到对话里找。
  - 结束：上方给下一步的建议（审阅改动、提交、推送并开 PR、继续完善）；失败或终止时给“重试”。
- **输入框下的工具条**：沙箱（只读、工作区可写；“完全访问”标为未开放）、模型倾向（省钱、均衡、最强）、本项目已用的 token 与花费（点开看输入、输出、缓存读的分项）。
- **右栏**：环境（Runtime 连接、沙箱、分支与 HEAD、来源）、上下文（当前任务的目标、完成条件、约束、已验证事实、假设、待问问题、各轮的判定与花费，token 分项）、输出（最近一次的交付与结局）、改动（文件列表、提交、推送、补丁、丢弃）、后台任务（本租户所有项目里进行中与排队的任务，可跳转、可停止）。

### 7.3 这些开关落到 Runtime 的哪里

- **沙箱等级**：effect 上限。只读是 `read`（只能读代码、回答，不改文件、不跑命令，`shell_run` 是 write_reversible）；工作区可写是 `write_reversible`（命令在断网的一次性容器里跑）。Task API 没有逐个任务的上限，effect 上限来自租户策略：coding-web 在提交前把开发租户策略文件里这个租户的上限改成所选的等级，同一租户的“改写策略、提交”串行，task-service 在提交那一刻把上限固定进 Task。缺口：正式部署要在 `SubmitTaskRequest` 加一个只能调低的 `effect_ceiling`（走 S-01）。
- **模型倾向**：放进 `inputs.constraints.model_preference`，进入 Task State 的约束；Planner 的提示说明它的含义，规划 model 节点的 Tier 时参考（只能选可用的 Tier）。tool_loop 与 Reviewer 的 Tier 是固定配置，不受它影响。
- **已用 token**：`QueryTask` 的 usage；任务结束时缓存进 coding-web 的任务记录，项目合计在前端相加。

## 8. 工作区与会话

2026-10-25 用户要求：一个工作区可以开很多会话；侧栏上半部分放一些入口。用户选定“每个会话一份独立副本”。

- **工作区**（原来的“项目”）是一个基线仓库：`<工作区根>/<租户>/<工作区>`，由克隆、示例或空仓库建出来，之后不直接在里面改。
- **会话**是一段对话（Runtime 的一个 Session）加一份自己的工作副本：从基线仓库本地克隆（`git clone --local`，对象用硬链接，得到一个完整、独立的仓库，Docker 沙箱里也能用 git），检出基线分支；GitHub 工作区再把远端换回 GitHub、尽量拉到基线分支的最新提交。目录与 Runtime 的 workspace id 都是 `<工作区>--<会话>`，Worker 的工具与沙箱只看得到这份副本。会话之间互不干扰、可以同时跑；改动、提交、补丁、推送都按会话，GitHub 工作区里一个会话一个 `agent/*` 分支、一个 PR。
- **草稿**：“新建会话”只打开草稿，第一条消息发出时才开会话（复制副本），标题取这条消息的第一行（最多 40 字），可以改名。
- **迁移**：会话出现之前的项目（记着 Runtime Session、推送记录或任务）读到时变成这个工作区的 `main` 会话，直接用工作区自己的目录与原来的任务记录；删 `main` 会话只删记录，不删目录。
- **接口**：工作区 `GET/POST /api/projects`、`DELETE /api/projects/{p}`（连同所有会话的副本）；会话 `GET /api/sessions`、`POST /api/projects/{p}/sessions`，其余（任务、事件、回复、取消、改动、提交、补丁、推送、回到基线、文件、提交历史、环境、改名、删除）都在 `/api/projects/{p}/sessions/{s}/…` 下；`GET /api/tasks/active` 带上工作区与会话。
- **界面**：左栏上半部分是“新建会话、搜索（Ctrl K）、工作区、进行中”，下半部分按工作区分组列出会话（可折叠，悬停出现“+”开新会话、“…”改名或删除）。中间多了“工作区”一览（每个工作区的会话数、在跑的会话、最近的会话，新建会话、删除工作区）与“进行中”（所有会话里在跑与排队的任务，可跳转、可停止）。地址是 `#/w/<工作区>/s/<会话>`、`#/w/<工作区>/new`、`#/workspaces`、`#/activity`。
- **还没做**：会话之间合并改动（现在靠各自推 PR，或在基线仓库外手动合并）；归档会话；清理长期不用的副本。

## 9. 多平台登录与账号关联

2026-10-25 用户要求：支持 GitHub、Gitee 等各家 Git 平台的 OAuth 注册与登录，再加 Google；登录页 GitHub 是主按钮，其余 Git 平台收在它下面一个小的“其他 Git 平台”副按钮里。用户选定“一个账号关联多个平台”。

- **登录方式**（`products/coding/internal/server/oauth.go`）：GitHub、Gitee、GitLab（含自建）、Gitea/Forgejo/Codeberg（默认 Codeberg，可指向自建）、Bitbucket Cloud 是 Git 平台；Google 只用来认人（要求邮箱已验证）。都走 OAuth2 授权码流程，回调 `/api/auth/<平台>/callback`；配置是 `CODING_<平台>_CLIENT_ID`、`_CLIENT_SECRET`（可选 `_URL`、`_API_URL`、`_SCOPES`、`_NAME`），配了才打开。
- **账号**（`accounts.go`）：账号仍是（租户、用户）。平台账号按平台给的不变编号认（`identities.json`：`<平台>:<编号>` → 租户、用户）。第一次用某家登录就注册，租户 `<前缀>-<登录名>`（gh、gitee、gl、gitea、bb、google，被占用时加随机后缀），开发登录不许用这些前缀。登录后“连接”别家（`/api/auth/<平台>/login?link=1`），之后用任何一家登录都是这个账号；一个平台账号只能关联一个本地账号；平台注册的账号至少留一个登录方式。多平台之前的 GitHub 账号按 GitHub 编号认回原来的 `gh-` 租户，账号文件就地迁移。
- **令牌**：按（租户、用户、平台）加密存放；会过期的（GitLab 2 小时、Gitee 1 天、Gitea、Bitbucket 1–2 小时）用前两分钟内自动用续期令牌换新；续不上时提示重新连接。API 调用出错时的提示不带令牌（Gitee 的令牌在查询参数里，连接失败的错误不带地址）。
- **各家的差别**（`providers.go`）：仓库列表只列能推送的（GitLab `min_access_level=30`、Bitbucket `role=contributor`）；推送的 Basic 认证 GitHub 是“令牌 + x-oauth-basic”，GitLab、Gitea 是 `oauth2` + 令牌，Bitbucket 是 `x-token-auth` + 令牌，Gitee 是登录名 + 令牌；草稿 GitHub 用 `draft`（不支持时退回普通 PR），GitLab 用 `Draft:` 标题前缀，Gitea 用 `WIP:`，Bitbucket 先试 `draft` 字段、不认就退回普通 PR，Gitee 开普通 PR；同一分支已有打开的 PR/MR 时直接用它。提交作者：GitHub 用 noreply 地址，其余用公开邮箱或 `<登录名>@users.noreply.<主机>`。
- **工作区**：`git_provider` 与 `repo` 记下平台与仓库路径（旧的 `github_repo` 读到时转换）；按克隆地址的主机认出是哪一家。
- **界面**：登录页（GitHub 主按钮、“其他 Git 平台”副按钮展开 Gitee/GitLab/Gitea/Bitbucket、Google、开发登录；开发环境里没配置的也列出来、不可点）；左下角菜单“账号与平台”（连接、断开）；新建工作区按已连接的平台选仓库；推送卡片与下一步按平台改叫法（GitLab 叫 Merge Request）。平台标记 GitHub 用它的标志，其余是品牌色字母块，不照搬各家标志。
- **验证**：各家都只对着替身服务测过（`oauth_test.go`：换令牌的 client 认证方式、Gitee 的查询参数与 User-Agent、续期、关联与冲突、旧账号迁移、各家的 PR/MR 请求）。没在真平台上验：Gitee 推送时用登录名作用户名、Bitbucket 创建草稿 PR 的字段都是按文档与常见做法推的。
- **还没做**：同一家接多个实例（例如两个自建 GitLab）；GitHub App（按仓库授权）；Bitbucket Server / Data Center；把 Google 等身份与企业 SSO 打通。
