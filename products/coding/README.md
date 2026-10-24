# Coding Agent（MVP）

建在本仓库 Runtime 上的在线多租户编码智能体：网页上登录、建项目、用一句话下任务；Agent 读代码、改代码、在 Docker 沙箱里跑测试，Reviewer 判定是否完成；你在网页上看进度、批准需要确认的步骤、审阅 diff，再提交、下载补丁或丢弃。计划与取舍见 [docs/plan/coding-agent-mvp.md](../../docs/plan/coding-agent-mvp.md)。

## 跑起来

前置：Go、uv、Temporal CLI、Docker（沙箱用）。在仓库根目录：

```bash
uv run --no-sync python scripts/dev_stack.py up --coding --sandbox docker
```

离线试用（不调真模型，Agent 只会回一句桩回复）加 `--provider mock --decisions stub`。真模型的地址与密钥放在 `~/.agentruntime/model-provider.env`（见 CLAUDE.md）。

命令输出里的 `Coding` 就是网页地址。用任意租户名与用户名登录（开发登录，仅供测试），新建项目时选“示例仓库”可以直接试“修好 CI”的任务。

停掉：`uv run --no-sync python scripts/dev_stack.py down`。

## 组成

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| coding-web | `cmd/coding-web`、`internal/server` | Go，只用标准库与 `libs/go-core`：开发登录（签发 Runtime 令牌，放 HttpOnly Cookie）、项目（`<工作区根>/<租户>/<项目>` 下的 Git 仓库）、经 Task API 下任务、代理 SSE、diff 与提交 |
| 网页 | `internal/web/static` | 原生 JS 单页应用，无构建步骤，CSP 只允许同源脚本与样式 |
| 示例仓库 | `internal/samples` | 内嵌的“CI 修复”示例 |
| 沙箱 | Worker 的 `shell_run`（`AR_TOOL_SHELL_SANDBOX=docker`） | 一次性容器，只挂载本项目的工作区，不联网，限 CPU、内存与进程数 |

## 已知限制

开发登录（没有密码与 OAuth）；元数据存 JSON 文件，单实例；不推送到 GitHub、不开 PR；进度只到节点级，没有 token 级流式输出；同一项目的写任务由 Session 串行。
