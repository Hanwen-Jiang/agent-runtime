# syntax=docker/dockerfile:1.7
# 沙箱里跑命令的镜像：python:3.13-slim 加 sandbox-agent（docs/plan/cloud-deploy.md D-05、D-07）。构建上下文是仓库根目录。
# - Cloudflare Containers（deploy/cloudflare/sandbox-bridge/wrangler.jsonc 引用这个文件）：agent 是容器的主进程，
#   桥经 AGENT_TOKEN、AGENT_WORKDIR、AGENT_ADDR 配置它。
# - K8s 后端：同一个镜像也能用（Pod 的 command 指向 initContainer 注入的 agent，ENTRYPOINT 不起作用）。
# 以 10001 运行；/workspace 归 10001 所有。
ARG GO_IMAGE=golang:1.27-bookworm
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${GO_IMAGE} AS build
WORKDIR /src/services/go-sandbox-agent
COPY services/go-sandbox-agent/ ./
ENV GOWORK=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOFLAGS=-mod=readonly
RUN --mount=type=cache,target=/root/.cache/go-build go build -trimpath -ldflags="-s -w" -o /out/sandbox-agent ./cmd/sandbox-agent

FROM ${PYTHON_IMAGE}
COPY --from=build /out/sandbox-agent /usr/local/bin/sandbox-agent
RUN groupadd --gid 10001 sandbox && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace && chown 10001:10001 /workspace
ENV AGENT_WORKDIR=/workspace AGENT_ADDR=:7070 HOME=/tmp PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 10001:10001
EXPOSE 7070
ENTRYPOINT ["/usr/local/bin/sandbox-agent"]
