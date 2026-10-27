# Cloudflare 沙箱桥

Sandbox 的第二个 microVM 后端（docs/plan/cloud-deploy.md D-07）。Runtime 的 Worker 经 HTTPS 调这个 Cloudflare Worker；每个沙箱环境是一个 Durable Object 加一台 Cloudflare Container（Firecracker microVM），容器里跑 `sandbox-agent`。接口与转发规则见 `src/index.ts` 的开头；本机测试用的假桥（`agentruntime_core.adapters.cloudflaretest`）与它同一套。

## 部署

前置：Cloudflare 账号开了 Workers 付费版（$5/月，含一部分 Containers 用量）；本机有 Node 与 Docker（wrangler 用 Docker 构建镜像）。

```bash
cd deploy/cloudflare/sandbox-bridge
npm ci                                 # 按 package-lock.json 装（版本都写死了）
npx tsc --noEmit
npx wrangler deploy --dry-run --outdir /tmp/bridge-dist   # 只打包、构建镜像，不上传
npx wrangler secret put BRIDGE_TOKEN   # Runtime 调桥用的令牌，至少 32 个字符；同一个值写进 Runtime 的 SecretStore（见下）
npx wrangler secret put AGENT_KEY      # 派生每个沙箱的 agent 令牌，只在桥里用
npx wrangler deploy
```

镜像是 `deploy/images/sandbox-python/Dockerfile`（python:3.13-slim 加 sandbox-agent），构建上下文是仓库根（`image_build_context`）。实例类型默认 `standard-2`（1 vCPU、6 GiB、12 GB 盘），在 `wrangler.jsonc` 里改。

## 接到 Runtime

1. 桥的令牌写进 SecretStore 平台作用域的 `sandbox/cloudflare-bridge-token`（开发 SecretStore：`model-gateway dev-secret sandbox/cloudflare-bridge-token`，从标准输入读）。
2. Worker 的沙箱后端登记文件（`AR_SANDBOX_BACKENDS_FILE`）加一项：

   ```json
   "cloudflare": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": "https://agentruntime-sandbox-bridge.<账号>.workers.dev"}
   ```

3. 要用它的租户在租户策略里写 `"sandbox": {"backend": "cloudflare"}`。

真实端点的用例：`AR_LIVE_CLOUDFLARE=1`、`AR_CLOUDFLARE_BRIDGE_URL`、`AR_CLOUDFLARE_BRIDGE_TOKEN`，在 `libs/py-core` 下 `pytest tests/adapters/test_cloudflare_sandbox.py`（花钱、不进 CI）。

本机试跑：`.dev.vars` 里写 `BRIDGE_TOKEN=…`、`AGENT_KEY=…`（不提交），`npx wrangler dev`。带容器的本地开发 wrangler 只支持
Linux 与 macOS（Windows 上要在 WSL 里跑）；Windows 上可以 `npx wrangler dev --enable-containers=false` 看鉴权与路由，凡是要用到容器的请求都是 500。
Linux 或 WSL 里 `wrangler dev` 起来之后，把 `AR_CLOUDFLARE_BRIDGE_URL` 设成 `http://127.0.0.1:8787`、令牌设成 `.dev.vars` 里的，就能对本地的桥跑上面的用例（不花钱）。

## 验到哪一步了（2026-10-27）

- 过了：`tsc --noEmit`（修了三处：`getState()` 返回的是 Promise、`sleepAfter` 的类型、`override`）；`wrangler deploy --dry-run` 打包成功、
  按 `image_build_context` 用仓库根构建出镜像；Windows 上不带容器的 `wrangler dev` 里鉴权（401）、作用域格式（400）、环境 id 格式（404）。
- 没过的环节：带容器的本地运行（要 Linux 或 WSL）与真实部署。第一次部署后跑一遍上面的真实端点用例。

绑定 workspace（编码任务用）要等 D-06“工作区跟着沙箱走”：现在 Cloudflare 后端只能跑不绑定 workspace 的环境，Worker 的 shell_run 选到它会报 `unsupported_workspace`。
