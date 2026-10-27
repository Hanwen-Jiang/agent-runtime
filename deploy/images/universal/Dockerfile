# syntax=docker/dockerfile:1.7
# 沙箱的多语言运行环境 universal（docs/plan/cloud-deploy.md D-14；设计书改动稿 W6）：Python 3.13、Node 22 LTS、Go、JDK 21
# 加 Maven、Rust stable、build-essential、git，加 sandbox-agent。所有后端用同一个镜像（K8s 直接用，Cloudflare 与 e2b 分支的
# E2B 按它构建），换后端不涉及迁移语言环境。构建上下文是仓库根目录。
#
# - 每种工具链一层（从官方镜像 COPY），升级一种语言只换一层。版本写死、发布满两周（与仓库的依赖规则一致）：
#   Python 3.13.16（2026-09-30）、Node 22.23.3（2026-09-23）、Go 1.27.2、Temurin 21.0.11、Maven 3.10.0（2026-10-01）、
#   Rust 1.99.0。升级时改这里的 ARG，并跑 tests/deploy/test_images.py（AR_TEST_IMAGES=1 时在镜像里编译、测试五种语言的样例）。
# - 以 10001 运行、根文件系统只读：各工具的缓存与下载目录都指到 /tmp（K8s 后端给的 emptyDir）或工作区，不写镜像里的目录。
#   包镜像的地址（PIP_INDEX_URL、npm_config_registry、GOPROXY 等，D-15）由 Runtime 建环境时注入，不写死在镜像里。
ARG PYTHON_IMAGE=python:3.13.16-slim-bookworm
ARG NODE_IMAGE=node:22.23.3-bookworm-slim
ARG GO_IMAGE=golang:1.27.2-bookworm
ARG JDK_IMAGE=eclipse-temurin:21.0.11_10-jdk-jammy
ARG MAVEN_IMAGE=maven:3.10.0-eclipse-temurin-21
ARG RUST_IMAGE=rust:1.99.0-slim-bookworm
ARG AGENT_GO_IMAGE=golang:1.27-bookworm

FROM ${AGENT_GO_IMAGE} AS agent
WORKDIR /src/services/go-sandbox-agent
COPY services/go-sandbox-agent/ ./
ENV GOWORK=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOFLAGS=-mod=readonly
RUN --mount=type=cache,target=/root/.cache/go-build go build -trimpath -ldflags="-s -w" -o /out/sandbox-agent ./cmd/sandbox-agent

FROM ${NODE_IMAGE} AS node
FROM ${GO_IMAGE} AS go
FROM ${JDK_IMAGE} AS jdk
FROM ${MAVEN_IMAGE} AS maven
FROM ${RUST_IMAGE} AS rust

# 底层就是 Python 的官方镜像（Debian bookworm）。
FROM ${PYTHON_IMAGE}

# 1. 系统工具：编译器（Rust 的链接、Python 的 C 扩展）、git 与常用的解压工具。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git ca-certificates curl xz-utils unzip zip pkg-config libssl-dev procps \
    && rm -rf /var/lib/apt/lists/*

# 2. Node 22 LTS（npm、npx、corepack）。
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack

# 3. Go。
COPY --from=go /usr/local/go /usr/local/go

# 4. JDK 21 与 Maven。
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
COPY --from=maven /usr/share/maven /usr/share/maven

# 5. Rust stable（rustup 管的工具链只读；下载的 crate 放 CARGO_HOME=/tmp/.cargo）。
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo /usr/local/cargo

# 6. sandbox-agent 与运行用户。
COPY --from=agent /out/sandbox-agent /usr/local/bin/sandbox-agent
RUN groupadd --gid 10001 sandbox && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /workspace && chown 10001:10001 /workspace \
    && ln -s /usr/share/maven/bin/mvn /usr/local/bin/mvn

ENV PATH=/usr/local/cargo/bin:/usr/local/go/bin:/opt/java/openjdk/bin:/usr/share/maven/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/tmp \
    AGENT_WORKDIR=/workspace AGENT_ADDR=:7070 \
    PYTHONDONTWRITEBYTECODE=1 PIP_CACHE_DIR=/tmp/.cache/pip PIP_DISABLE_PIP_VERSION_CHECK=1 \
    npm_config_cache=/tmp/.npm npm_config_update_notifier=false npm_config_fund=false \
    GOPATH=/tmp/go GOCACHE=/tmp/.cache/go-build GOMODCACHE=/tmp/go/pkg/mod GOTOOLCHAIN=local GOFLAGS=-modcacherw \
    JAVA_HOME=/opt/java/openjdk MAVEN_HOME=/usr/share/maven MAVEN_ARGS="-Dmaven.repo.local=/tmp/.m2/repository" \
    RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/tmp/.cargo
WORKDIR /workspace
USER 10001:10001
EXPOSE 7070
ENTRYPOINT ["/usr/local/bin/sandbox-agent"]
