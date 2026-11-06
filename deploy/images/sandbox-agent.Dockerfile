# syntax=docker/dockerfile:1.7
# sandbox-agent 的镜像（docs/plan/cloud-deploy.md D-05）：K8s 沙箱 Pod 的 initContainer 用它的 install 子命令把二进制
# 拷进 emptyDir，跑命令的容器再从那里启动 agent。只有一个静态二进制，FROM scratch。构建上下文是仓库根目录。
ARG AGENT_RUST_IMAGE=rust:1.99.1-slim-bookworm

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

FROM scratch
COPY --from=build /out/sandbox-agent /sandbox-agent
USER 10001:10001
ENTRYPOINT ["/sandbox-agent"]
