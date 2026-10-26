# syntax=docker/dockerfile:1.7
# sandbox-agent 的镜像（docs/plan/cloud-deploy.md D-05）：K8s 沙箱 Pod 的 initContainer 用它的 install 子命令把二进制
# 拷进 emptyDir，跑命令的容器再从那里启动 agent。只有一个静态二进制，FROM scratch。构建上下文是仓库根目录。
ARG GO_IMAGE=golang:1.27-bookworm

FROM ${GO_IMAGE} AS build
WORKDIR /src/services/go-sandbox-agent
COPY services/go-sandbox-agent/ ./
ENV GOWORK=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOFLAGS=-mod=readonly
RUN --mount=type=cache,target=/root/.cache/go-build go build -trimpath -ldflags="-s -w" -o /out/sandbox-agent ./cmd/sandbox-agent

FROM scratch
COPY --from=build /out/sandbox-agent /sandbox-agent
USER 10001:10001
ENTRYPOINT ["/sandbox-agent"]
