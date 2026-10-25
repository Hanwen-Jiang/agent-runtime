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
