"""SSE 经入口时是不是逐条到达（docs/plan/cloud-deploy.md D-02 验收 4）。只用标准库。

    python scripts/sse_probe.py --base-url https://agent-runtime.example.com --task-id <正在跑的 Task> --token-file <令牌文件>

订阅 GET /v1/tasks/<id>/events，记下每一块数据（事件或 15 秒一次的心跳注释）到达的时间。代理（Traefik、Cloudflare）缓冲响应时，
所有数据会在连接结束时一起到；逐条转发时，数据在流的整个时长里陆续到达。
退出码：0 逐条到达；1 被缓冲（流持续了不止 3 秒，数据却都在结束前 0.2 秒内才到）；2 无法判断（流太短，换一个跑得久的 Task，
例如等人工回复的）。令牌用 task-service dev-token 签发；从文件或标准输入读，不放在命令行参数里。
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--token-file", default="-", help="令牌所在的文件；- 表示标准输入")
    parser.add_argument("--max-seconds", type=float, default=120, help="最多听多久")
    args = parser.parse_args(argv)
    token = (sys.stdin.read() if args.token_file == "-" else open(args.token_file, encoding="utf-8").read()).strip()
    url = f"{args.base_url.rstrip('/')}/v1/tasks/{args.task_id}/events"
    # Cloudflare 的 Browser Integrity Check 拒绝 Python-urllib 的默认 User-Agent（错误 1010）。
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream", "User-Agent": "agentruntime-sse-probe/1"})
    start = time.monotonic()
    arrivals: list[tuple[float, str]] = []
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("text/event-stream"):
                print(f"响应不是 SSE：{ctype}", file=sys.stderr)
                return 1
            while time.monotonic() - start < args.max_seconds:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    arrivals.append((time.monotonic() - start, text[:80]))
    except urllib.error.HTTPError as err:
        print(f"HTTP {err.code}：{err.reason}", file=sys.stderr)
        return 1
    end = time.monotonic() - start
    for at, text in arrivals:
        print(f"{at:7.2f}s  {text}")
    print(f"流持续 {end:.2f}s，收到 {len(arrivals)} 行")
    if len(arrivals) < 2 or end < 3:
        print("无法判断：流太短。换一个跑得久的 Task。")
        return 2
    if arrivals[0][0] > end - 0.2:
        print("被缓冲：所有数据都在连接结束时才到。")
        return 1
    print("逐条到达。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
