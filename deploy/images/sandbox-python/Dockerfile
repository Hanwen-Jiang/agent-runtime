# syntax=docker/dockerfile:1.7
# 沙箱里跑命令的镜像：python:3.13-slim 加 sandbox-agent（docs/plan/cloud-deploy.md D-05、D-07）。构建上下文是仓库根目录。
# - Cloudflare Containers（deploy/cloudflare/sandbox-bridge/wrangler.jsonc 引用这个文件）：agent 是容器的主进程，
#   桥经 AGENT_TOKEN、AGENT_WORKDIR、AGENT_ADDR 配置它。
# - K8s 后端：同一个镜像也能用（Pod 的 command 指向 initContainer 注入的 agent，ENTRYPOINT 不起作用）。
# 以 10001 运行；/workspace 归 10001 所有。
ARG AGENT_RUST_IMAGE=rust:1.99.1-slim-bookworm
ARG PYTHON_IMAGE=python:3.13-slim

FROM ${AGENT_RUST_IMAGE} AS build
# Rust 版 agent（services/rs-sandbox-agent，D-28）：musl 静态链接。根 Cargo workspace 的成员都要在（cargo 读 workspace 的清单），
# 只编译 agent；rust-toolchain.toml 固定的版本、组件与 musl 目标由 rustup 按需装上。
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY libs/rs-sys/ libs/rs-sys/
COPY libs/rs-transfer/ libs/rs-transfer/
COPY services/rs-sandbox-agent/ services/rs-sandbox-agent/
COPY services/rs-fc-manager/ services/rs-fc-manager/
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/src/target \
    cargo build --locked --release --target x86_64-unknown-linux-musl -p rs-sandbox-agent \
    && install -D -m 0755 target/x86_64-unknown-linux-musl/release/sandbox-agent /out/sandbox-agent

FROM ${PYTHON_IMAGE}
COPY --from=build /out/sandbox-agent /usr/local/bin/sandbox-agent
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 sandbox && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace && chown 10001:10001 /workspace
ENV AGENT_WORKDIR=/workspace AGENT_ADDR=:7070 HOME=/tmp PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 10001:10001
EXPOSE 7070
ENTRYPOINT ["/usr/local/bin/sandbox-agent"]
