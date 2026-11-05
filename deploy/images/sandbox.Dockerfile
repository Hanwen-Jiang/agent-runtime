# syntax=docker/dockerfile:1.7
# 单一语言的精简沙箱镜像（docs/plan/cloud-deploy.md D-14）：官方的语言镜像加 sandbox-agent，给明确只用一种语言的租户
# （默认是 deploy/images/universal）。BASE_IMAGE 由 deploy/images/images.json 给出（版本写死、发布满两周）。构建上下文是仓库根目录。
# 以 10001 运行、根文件系统只读；缓存目录指到 /tmp。Python 的那个仍是 deploy/images/sandbox-python（Cloudflare 桥引用它）。
# 工具链要的变量列在 AGENT_ENV_PASSTHROUGH 里，sandbox-agent 才交给命令。
ARG BASE_IMAGE
ARG AGENT_RUST_IMAGE=rust:1.99.1-slim-bookworm

FROM ${AGENT_RUST_IMAGE} AS agent
# Rust 版 agent（services/rs-sandbox-agent，D-28）：musl 静态链接。根 Cargo workspace 的成员都要在（cargo 读 workspace 的清单），
# 只编译 agent；rust-toolchain.toml 固定的版本、组件与 musl 目标由 rustup 按需装上。
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY libs/rs-sys/ libs/rs-sys/
COPY services/rs-sandbox-agent/ services/rs-sandbox-agent/
COPY services/rs-fc-manager/ services/rs-fc-manager/
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/src/target \
    cargo build --locked --release --target x86_64-unknown-linux-musl -p rs-sandbox-agent \
    && install -D -m 0755 target/x86_64-unknown-linux-musl/release/sandbox-agent /out/sandbox-agent

FROM ${BASE_IMAGE}
COPY --from=agent /out/sandbox-agent /usr/local/bin/sandbox-agent
USER root
RUN groupadd --gid 10001 sandbox && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace && chown 10001:10001 /workspace
ENV HOME=/tmp AGENT_WORKDIR=/workspace AGENT_ADDR=:7070 \
    npm_config_cache=/tmp/.npm npm_config_update_notifier=false \
    GOPATH=/tmp/go GOCACHE=/tmp/.cache/go-build GOMODCACHE=/tmp/go/pkg/mod GOTOOLCHAIN=local GOFLAGS=-modcacherw \
    AGENT_ENV_PASSTHROUGH="npm_config_cache npm_config_update_notifier GOPATH GOCACHE GOMODCACHE GOTOOLCHAIN GOFLAGS"
WORKDIR /workspace
USER 10001:10001
EXPOSE 7070
ENTRYPOINT ["/usr/local/bin/sandbox-agent"]
