# syntax=docker/dockerfile:1.7
# Ubuntu 24.04 provides matching system Python 3.12, LibreOffice and pyuno.
# Pin BASE_IMAGE to the reviewed digest in the release command; a floating base
# is deliberately not accepted as a production reproducibility claim.
ARG BASE_IMAGE=ubuntu:24.04
ARG AGENT_RUST_IMAGE=rust:1.99.1-slim-bookworm
FROM ${AGENT_RUST_IMAGE} AS agent
ARG CARGO_BUILD_JOBS=4
ENV CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS}
RUN apt-get update && apt-get install -y --no-install-recommends musl-tools && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY libs/rs-sys/ libs/rs-sys/
COPY libs/rs-transfer/ libs/rs-transfer/
COPY services/rs-sandbox-agent/ services/rs-sandbox-agent/
COPY services/rs-fc-manager/ services/rs-fc-manager/
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/src/target \
    cargo build --locked --release --target x86_64-unknown-linux-musl -p rs-sandbox-agent \
    && install -D -m 0755 target/x86_64-unknown-linux-musl/release/sandbox-agent /out/sandbox-agent
FROM ${BASE_IMAGE}
ARG DEBIAN_FRONTEND=noninteractive
ARG UBUNTU_SNAPSHOT=20261110T000000Z
COPY --from=agent /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN sed -i "s|http://archive.ubuntu.com/ubuntu/|https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/|g;s|http://security.ubuntu.com/ubuntu/|https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/|g" /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends python3.12 python3.12-venv python3-pip python3-uno \
       libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils fontconfig locales ca-certificates \
       fonts-noto-cjk fonts-noto-core fonts-noto-extra fonts-liberation2 fonts-dejavu-core libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8
RUN python3.12 -m venv --system-site-packages /opt/file-engines
COPY requirements-file-engines.lock /opt/requirements-file-engines.lock
# CPU torch is installed first so the dependency install does not select CUDA.
RUN /opt/file-engines/bin/pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 torchvision==0.29.1 \
    && /opt/file-engines/bin/pip install --no-cache-dir -r /opt/requirements-file-engines.lock
COPY scripts/file-engines/prefetch_models.py /opt/prefetch_models.py
COPY deploy/images/file-engines/models.lock.json /opt/models.lock.json
RUN /opt/file-engines/bin/python /opt/prefetch_models.py --manifest /opt/models.lock.json --output /opt/file-models
COPY services/py-worker/src/agentruntime_worker/files /opt/engine/agentruntime_worker/files
COPY --from=agent /out/sandbox-agent /usr/local/bin/sandbox-agent
RUN touch /opt/engine/agentruntime_worker/__init__.py \
    && dpkg-query -W > /opt/system-packages.lock \
    && fc-cache -f \
    && find /usr/share/fonts -type f -exec sha256sum {} \; | sort > /opt/fonts.lock \
    && /usr/bin/python3 -c 'import uno' \
    && useradd --uid 10001 --create-home engine \
    && mkdir -p /workspace && chown 10001:10001 /workspace
ENV PATH=/opt/file-engines/bin:$PATH PYTHONPATH=/opt/engine \
    FILE_ENGINES_MODEL_DIR=/opt/file-models HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    AR_UNO_PYTHON=/usr/bin/python3 AR_SOFFICE_BIN=/usr/bin/soffice \
    PYTHONDONTWRITEBYTECODE=1 HOME=/tmp LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TZ=UTC \
    AGENT_WORKDIR=/workspace AGENT_ADDR=:7070 \
    AGENT_ENV_PASSTHROUGH=PATH,PYTHONPATH,FILE_ENGINES_MODEL_DIR,HF_HUB_OFFLINE,TRANSFORMERS_OFFLINE,AR_UNO_PYTHON,AR_SOFFICE_BIN,LANG,LC_ALL,TZ
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/sandbox-agent"]
