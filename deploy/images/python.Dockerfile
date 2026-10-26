# syntax=docker/dockerfile:1.7
# Runtime 的 Python 服务镜像：worker、memory、learning（docs/plan/cloud-deploy.md D-01）。构建参数 PACKAGE 是 uv 工作区里的包名，
# 镜像名见 images.json，一般用 scripts/build_images.py 构建；构建上下文是仓库根目录（.dockerignore 在那里）。
#
# - 依赖按 uv.lock 装（--locked），不在镜像里重新解析；不装开发与测试依赖。
# - 工作区成员以可编辑方式装在 /app 下、保持仓库里的目录结构：代码按源码位置找 /schemas 与评测集
#   （例如 agentruntime_worker/planner/validate.py 用 parents[5]），装进 site-packages 就找不到了。
# - uv 要看到整个工作区才能核对 uv.lock，所以 tests/ 下的成员只复制 pyproject.toml。
# - 字节码在构建时编译好，运行时不写 .pyc（根文件系统只读）；以 10001 运行。
ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.6

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY libs/py-core/ libs/py-core/
COPY proto/gen/python/ proto/gen/python/
COPY services/py-worker/ services/py-worker/
COPY services/py-memory/ services/py-memory/
COPY services/py-learning/ services/py-learning/
COPY tests/contract/pyproject.toml tests/contract/
COPY tests/deploy/pyproject.toml tests/deploy/
COPY tests/e2e/pyproject.toml tests/e2e/
COPY schemas/ schemas/
ARG PACKAGE
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --package "${PACKAGE}"

FROM ${PYTHON_IMAGE}
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:${PATH} PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["python"]
