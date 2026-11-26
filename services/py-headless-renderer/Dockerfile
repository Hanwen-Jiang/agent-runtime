# syntax=docker/dockerfile:1.7
# Isolated headless Chromium renderer for go-crawler-gateway external JS-heavy pages.
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.52.0-noble

FROM ${PLAYWRIGHT_IMAGE}
RUN groupadd --gid 10001 --non-unique headless \
    && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin --non-unique headless \
    && python3 -m pip install --no-cache-dir --break-system-packages playwright==1.52.0 \
    && mkdir -p /app \
    && chmod -R a+rX /ms-playwright || true
COPY services/py-headless-renderer/app.py /app/app.py
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp AR_HEADLESS_ADDR=0.0.0.0:8098
WORKDIR /app
USER 10001:10001
EXPOSE 8098
ENTRYPOINT ["python3", "/app/app.py"]
