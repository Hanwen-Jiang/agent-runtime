"""构建自托管 E2B 的模板（e2b 分支，docs/plan/e2b-backend.md 第 4 节、E-03）。只用标准库。

模板从 Runtime 的沙箱镜像构建（与 K8s 后端、Cloudflare 后端同一份 Dockerfile 打出的镜像），镜像必须按 digest 固定：

    uv run --no-sync python scripts/e2b_template.py --api-url http://e2b-node.internal:3000 \\
        --name agentruntime-python --image ghcr.io/agentruntime/sandbox-python@sha256:<64 位> [--cpu 2 --memory-mb 2048]

- E2B 的 API key 读 AR_E2B_API_KEY（或 AR_E2B_API_KEY_FILE 指的文件），不从命令行传（会进 shell 历史与进程列表）。
- 私有镜像仓库的凭据读 AR_E2B_REGISTRY_USERNAME、AR_E2B_REGISTRY_PASSWORD（两个都给才用）。
- 步骤：POST /v3/templates（名字、CPU、内存）→ POST /v2/templates/{id}/builds/{build}（fromImage）→ 轮询
  GET /templates/{id}/builds/{build}/status，直到 ready 或 error。成功时在标准输出打一行 JSON：模板名、id、build id、镜像。
- 不打印 key 与仓库密码；构建日志只打 E2B 返回的消息。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class E2BError(RuntimeError):
    pass


def api_key(env: dict[str, str]) -> str:
    key = env.get("AR_E2B_API_KEY", "").strip()
    if not key and env.get("AR_E2B_API_KEY_FILE"):
        key = Path(env["AR_E2B_API_KEY_FILE"]).read_text("utf-8").strip()
    if not key:
        raise E2BError("要设 AR_E2B_API_KEY 或 AR_E2B_API_KEY_FILE")
    return key


def call(api_url: str, key: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(api_url.rstrip("/") + path, data=data, method=method)
    request.add_header("X-API-Key", key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=60) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            message = str(json.loads(raw).get("message") or "")
        except (ValueError, AttributeError):
            message = ""
        raise E2BError(f"E2B 返回 HTTP {err.code}：{message[:300]}") from None
    except (urllib.error.URLError, OSError) as err:
        raise E2BError(f"连不上 E2B 的 api（{type(err).__name__}）") from None


def build(
    api_url: str, key: str, *, name: str, image: str, cpu: int, memory_mb: int, env: dict[str, str], poll_s: float = 2.0, timeout_s: float = 1800
) -> dict[str, Any]:
    if not _NAME.match(name):
        raise E2BError("模板名只能是小写字母、数字、- 与 _")
    if not _IMAGE.match(image):
        raise E2BError("镜像要按 digest 固定（…@sha256:<64 位十六进制>），复现时才知道模板里是什么")
    _, created = call(api_url, key, "POST", "/v3/templates", {"name": name, "cpuCount": cpu, "memoryMB": memory_mb})
    template_id, build_id = str(created["templateID"]), str(created["buildID"])
    start: dict[str, Any] = {"fromImage": image}
    user, password = env.get("AR_E2B_REGISTRY_USERNAME", ""), env.get("AR_E2B_REGISTRY_PASSWORD", "")
    if user and password:
        start["fromImageRegistry"] = {"type": "registry", "username": user, "password": password}
    call(api_url, key, "POST", f"/v2/templates/{template_id}/builds/{build_id}", start)
    deadline = time.monotonic() + timeout_s
    offset = 0
    while True:
        _, status = call(api_url, key, "GET", f"/templates/{template_id}/builds/{build_id}/status?logsOffset={offset}")
        for entry in status.get("logEntries") or []:
            print(f"[{entry.get('level', 'info')}] {entry.get('message', '')}", file=sys.stderr)
        offset += len(status.get("logEntries") or [])
        if status.get("status") == "ready":
            return {"template": name, "template_id": template_id, "build_id": build_id, "image": image}
        if status.get("status") == "error":
            reason = (status.get("reason") or {}).get("message", "")
            raise E2BError(f"模板构建失败：{reason[:500]}")
        if time.monotonic() > deadline:
            raise E2BError("模板构建超时")
        time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 Runtime 的沙箱镜像构建自托管 E2B 的模板")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--image", required=True, help="按 digest 固定的镜像，…@sha256:…")
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=2048)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        result = build(args.api_url, api_key(env), name=args.name, image=args.image, cpu=args.cpu, memory_mb=args.memory_mb, env=env)
    except E2BError as err:
        print(f"e2b_template：{err}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
