# syntax=docker/dockerfile:1.7
# Runtime 的 Go 服务镜像：task-service、model-gateway（docs/plan/cloud-deploy.md D-01）。构建参数与镜像名见 images.json，
# 一般用 scripts/build_images.py 构建；构建上下文是仓库根目录（.dockerignore 在那里）。
#
# - 静态二进制（CGO_ENABLED=0；SQLite 用的是纯 Go 的 modernc.org/sqlite），运行镜像是 distroless 的 static，没有 shell。
# - 各模块的 go.mod 用 replace 指向 libs/go-core 与 proto/gen/go，所以 GOWORK=off、只复制这三处。
# - 以 10001 运行（与 Chart 的 securityContext 一致）；根文件系统只读时可写的只有挂载的卷与 /tmp。
# - /schemas 一并放进去：task-service 的 Registry 用它校验 document（AR_SCHEMAS_DIR）。
ARG GO_IMAGE=golang:1.27-bookworm
ARG RUNTIME_IMAGE=gcr.io/distroless/static-debian12:nonroot

FROM ${GO_IMAGE} AS build
ARG MODULE
ARG MAIN
WORKDIR /src
COPY libs/go-core/ libs/go-core/
COPY proto/gen/go/ proto/gen/go/
COPY ${MODULE}/ ${MODULE}/
WORKDIR /src/${MODULE}
ENV GOWORK=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOFLAGS=-mod=readonly
RUN --mount=type=cache,target=/root/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -ldflags="-s -w" -o /out/app ${MAIN}

FROM ${RUNTIME_IMAGE}
COPY --from=build /out/app /usr/local/bin/app
COPY schemas/ /schemas/
ENV AR_SCHEMAS_DIR=/schemas
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/app"]
