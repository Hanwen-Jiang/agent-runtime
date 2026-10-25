# Coding Agent（MVP）

建在本仓库 Runtime 上的在线多租户编码智能体：网页上登录、建项目、用一句话下任务；Agent 读代码、改代码、在 Docker 沙箱里跑测试，Reviewer 判定是否完成；你在网页上看进度、批准需要确认的步骤、审阅 diff，再提交、下载补丁或丢弃。计划与取舍见 [docs/plan/coding-agent-mvp.md](../../docs/plan/coding-agent-mvp.md)。

## 跑起来

前置：Go、uv、Temporal CLI、Docker（沙箱用）。在仓库根目录：

```bash
uv run --no-sync python scripts/dev_stack.py up --coding --sandbox docker
```

离线试用（不调真模型，Agent 只会回一句桩回复）加 `--provider mock --decisions stub`。真模型的地址与密钥放在 `~/.agentruntime/model-provider.env`（见 CLAUDE.md）。

命令输出里的 `Coding` 就是网页地址（默认 http://127.0.0.1:8095）。用任意租户名与用户名登录（开发登录，仅供测试），新建工作区时选“示例仓库”可以直接试“修好 CI”的任务。

### 打开第三方登录（GitHub、Gitee、GitLab、Gitea、Bitbucket、Google）

在各家建一个 OAuth 应用，回调地址都是 `http://127.0.0.1:8095/api/auth/<平台>/callback`，把 Client ID 与 Secret 写进 `~/.agentruntime/oauth.env`（仓库外，不提交；只有 GitHub 时的旧文件 `github-oauth.env` 也照样读），重新 `up --coding`。配了哪几家，登录页就出现哪几家：

| 平台 | 在哪里建应用 | 回调里的 `<平台>` | 权限 |
| --- | --- | --- | --- |
| GitHub | Settings → Developer settings → OAuth Apps | `github` | 默认 scope `repo read:user` |
| Gitee | 设置 → 第三方应用 → 创建应用 | `gitee` | 勾选 user_info、projects、pull_requests |
| GitLab（含自建） | User Settings → Applications | `gitlab` | 勾选 api、read_user；自建的填 `CODING_GITLAB_URL` |
| Gitea / Forgejo / Codeberg | Settings → Applications → OAuth2 Applications | `gitea` | 默认指向 Codeberg；自建的填 `CODING_GITEA_URL`（可加 `CODING_GITEA_NAME`） |
| Bitbucket Cloud | Workspace settings → OAuth consumers | `bitbucket` | Account: Read、Repositories: Write、Pull requests: Write |
| Google（只用来登录） | Google Cloud Console → Credentials → OAuth client ID（Web） | `google` | openid、email、profile |

```text
CODING_GITHUB_CLIENT_ID = <Client ID>
CODING_GITHUB_CLIENT_SECRET = <Client Secret>
CODING_GITLAB_CLIENT_ID = <Application ID>
CODING_GITLAB_CLIENT_SECRET = <Secret>
CODING_GITLAB_URL = https://git.example.com
```

**一个账号，多个平台**：第一次用哪一家登录就自动注册（租户 `<前缀>-<登录名>`：gh、gitee、gl、gitea、bb、google）；登录后在左下角菜单的“账号与平台”里连接别家，之后用其中任何一家登录都是同一个账号，工作区里可以混放各家的仓库。新建工作区时从已连接的平台里选仓库（私有仓库用你的授权克隆）。会话里的任务跑完，在“改动”页点“推送并开草稿 PR”（GitLab 是 Merge Request）：给这个会话新建 `agent/…` 分支、提交、只推这个分支、开草稿；之后再推会更新同一个；合并后点“回到 main”开始下一轮（或者开一个新会话）。

令牌只存在服务端（按账号与平台加密），不进浏览器、日志与 `.git/config`；git 用环境变量传认证头；GitLab、Gitee、Gitea、Bitbucket 的令牌会过期，用前自动续期。

停掉：`uv run --no-sync python scripts/dev_stack.py down`。

## 界面

参照 Claude 的版式（设计取舍见计划第 7、8 节）。一个**工作区**（一个代码仓库）下可以开很多**会话**；每个会话一段对话加一份自己的工作副本（从工作区本地克隆），会话之间互不干扰、可以同时跑，GitHub 工作区里一个会话一个分支、一个 PR。

左栏上半部分是新建会话、搜索（Ctrl K）、工作区、进行中，下半部分按工作区分组列出会话；中间是会话的对话（或工作区一览、进行中的任务）；右栏是当前会话的状态（环境、上下文、输出、改动、后台任务），窄屏时收进抽屉。diff、文件与提交历史在宽抽屉里打开。

- **输入框随阶段变化**：运行中上沿显示轮次、当前一步与用时，带“停止”（Esc 也行），这时发出的消息排在当前任务之后；Agent 要你确认时，问题与选项出现在输入框里；结束后上方给下一步（审阅改动、提交、推送并开 PR、继续、重试）。
- **输入框下方**：沙箱等级（只读、工作区可写；对下一个任务生效，提交时固定成它的 effect 上限）、模型倾向（省钱、均衡、最强；作为 `model_preference` 约束交给 Planner）、本项目的花费。Token 数要等 Runtime 在 `QueryTask` 里回报 usage。
- 快捷键：Enter 发送，Shift+Enter 换行，Esc 停止当前任务，`/` 回到输入框。外观（跟随系统、浅色、深色）在左下角的菜单里。

## 组成

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| coding-web | `cmd/coding-web`、`internal/server` | Go，只用标准库与 `libs/go-core`：开发登录（签发 Runtime 令牌，放 HttpOnly Cookie）、工作区（`<工作区根>/<租户>/<工作区>` 下的基线仓库）与会话（`<工作区>--<会话>` 下的工作副本）、经 Task API 下任务、代理 SSE、diff 与提交 |
| 网页 | `internal/web/static` | 原生 JS 单页应用，无构建步骤，CSP 只允许同源脚本与样式 |
| 示例仓库 | `internal/samples` | 内嵌的“CI 修复”示例 |
| 沙箱 | Worker 的 `shell_run`（`AR_TOOL_SHELL_SANDBOX=docker`） | 一次性容器，只挂载本项目的工作区，不联网，限 CPU、内存与进程数 |

## 已知限制

GitHub 用的是 OAuth App（用户级令牌、权限按 scope 而不是按仓库），以后换 GitHub App；不支持 fork 后提 PR（要有仓库的写权限）；Gitee、Bitbucket 的一些细节（Gitee 推送时的用户名、Bitbucket 创建草稿 PR 的字段）只对着替身测过，还没在真平台上验；每家只能配一个实例（例如不能同时接两个自建 GitLab）；令牌加密的密钥由开发签名密钥派生，生产要换 SecretStore；元数据存 JSON 文件，单实例；进度只到节点级，没有 token 级流式输出；同一项目的写任务由 Session 串行。沙箱等级靠在提交前改写开发租户策略文件实现（按租户串行），正式部署要在 Task API 加逐个任务的 effect 上限。
