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

## 5. 安全底线（第一版也要有）

- 租户只来自登录得到的令牌；项目、任务都按租户隔离，路径一律校验在工作区里。
- 开发登录只在 `CODING_DEV_LOGIN=1` 时打开，页面明确标出“测试用”。
- 沙箱：一次性容器、`--network none`、CPU、内存与进程数上限，只挂载本项目的工作区。
- Git 克隆只接受 https 地址，禁用 hooks；不把凭据写进地址。

## 6. 第二版（v0.3）

GitHub 登录与 GitHub App（推送到 `agent/*` 分支、开草稿 PR）；token 与工具状态的流式输出（需要 Runtime 的 R8：事件落库与增量事件）；按 CLAUDE.md 补齐验收测试；每个任务一个分支与 PatchSet；配额与计费；编辑器插件。
