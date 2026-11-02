"""评测看板（Eval Dashboard）的静态服务（docs/plan/eval-observability-and-runtime-fixes.md 第 11.12 节）。只用标准库。

跑在已有的 learning 镜像里（不为一个静态页面加新依赖）：页面是 --dir 下的 index.html（ConfigMap 挂进来，由发布步骤写），
scripts/eval_dashboard.py --public 生成。

- 只答 /、/index.html、/api/live.json 与 /healthz，只收 GET、HEAD；目录里的别的文件一概不给，请求的路径不拼进文件名。
- 每次请求现读文件：ConfigMap 更新后不用重启。还没发布时 / 回 404，/healthz 仍是 200。
- 响应带安全头（页面只有内联的脚本与样式，只许连回同源取 /api/live.json），错误信息不回显请求内容，不露 Python 版本，
  不记访问日志（入口有）。

实时的一栏（第 11.16 节）：/api/live.json 用**写死的**几条 MetricsQL 查 VictoriaMetrics（AR_EVAL_METRICS_URL）里的评测指标
（runtime_eval_*，agentruntime_learning.evals.live 发的），合成每次运行的进度、结果、花费与分数；客户端给的查询参数一概不用，
生产租户的指标一条也不出去。结果缓存 20 秒。VictoriaMetrics 连不上时返回最后一份并标明 stale；没配地址时 source 为 none，
页面只显示静态数据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

PAGE_NAME = "index.html"
PAGE_PATHS = frozenset({"/", "/index.html"})
HEALTH_PATH = "/healthz"
LIVE_PATH = "/api/live.json"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Cloudflare 可以短暂缓存；发布后几分钟内更新。
PAGE_CACHE_CONTROL = "public, max-age=300"
LIVE_CACHE_CONTROL = "public, max-age=15"
REQUEST_TIMEOUT_S = 30

METRICS_URL_ENV = "AR_EVAL_METRICS_URL"
LIVE_TTL_S = 20.0
METRICS_TIMEOUT_S = 5.0
# VictoriaMetrics 的默认保留期是一个月；更早的运行靠静态页里写死的历史。
LOOKBACK = "31d"
# 多久没有新的用例结果、也没结束的运行算“停了”（SWE-bench 一题可能要半小时以上）。
STALLED_AFTER_S = 2 * 3600
MAX_RUNS = 80

_K = "eval_run_id, eval_process"
# 写死的查询：只有 runtime_eval_* 指标。键是合成时用的名字。
QUERIES: Final[dict[str, str]] = {
    "planned": f"max by ({_K}, eval_suite, eval_subject, eval_split) (last_over_time(runtime_eval_cases_planned_total[{LOOKBACK}]))",
    "carried": f"max by ({_K}) (last_over_time(runtime_eval_cases_carried_total[{LOOKBACK}]))",
    "carried_cost": f"max by ({_K}) (last_over_time(runtime_eval_cost_carried_usd_sum[{LOOKBACK}]))",
    "cases": f"sum by ({_K}, eval_outcome) (last_over_time(runtime_eval_cases_total[{LOOKBACK}]))",
    "cost": f"sum by ({_K}) (last_over_time(runtime_eval_case_cost_usd_sum[{LOOKBACK}]))",
    "finished": f"max by ({_K}, eval_model) (last_over_time(runtime_eval_finished_total[{LOOKBACK}]))",
    "valid": f"min by (eval_run_id, eval_valid) (last_over_time(runtime_eval_runs_total[{LOOKBACK}]))",
    "scores": (
        f"max by (eval_run_id, eval_metric) (last_over_time(runtime_eval_score_sum[{LOOKBACK}]) / last_over_time(runtime_eval_score_count[{LOOKBACK}]))"
    ),
    "started": f"min by (eval_run_id) (tfirst_over_time(runtime_eval_cases_planned_total[{LOOKBACK}]))",
    "updated": f'max by (eval_run_id) (tlast_over_time({{__name__=~"runtime_eval_(cases|cases_planned|finished)_total"}}[{LOOKBACK}]))',
}

Fetch = Callable[[str, float], bytes]


def _http_get(url: str, timeout_s: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 — 地址来自部署配置，查询是写死的
        return bytes(resp.read())


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _value(sample: Mapping[str, Any]) -> float | None:
    try:
        value = float(sample["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def combine(results: Mapping[str, list[dict[str, Any]]], now: float) -> list[dict[str, Any]]:
    """把各条查询的结果合成每次运行一行。一次运行可以分几个进程跑（续跑）：已跑完的数与花费取各进程“带过来的 + 自己跑完的”的最大值，
    各种结果的条数取各进程之和。"""
    runs: dict[str, dict[str, Any]] = {}
    procs: dict[tuple[str, str], dict[str, float]] = {}

    def run_of(labels: Mapping[str, str]) -> dict[str, Any] | None:
        rid = labels.get("eval_run_id")
        if not rid:
            return None
        return runs.setdefault(rid, {
            "run_id": rid, "suite": "", "subject": "", "split": "", "model": "", "planned": 0, "done": 0,
            "outcomes": {}, "cost_usd": 0.0, "finished": False, "valid": None, "scores": {}, "started_at": None, "updated_at": None,
        })

    def proc_of(labels: Mapping[str, str]) -> dict[str, float]:
        return procs.setdefault((labels.get("eval_run_id", ""), labels.get("eval_process", "")), {"carried": 0.0, "carried_cost": 0.0, "done": 0.0, "cost": 0.0})

    for sample in results.get("planned", []):
        labels, value = sample.get("metric", {}), _value(sample)
        run = run_of(labels)
        if run is None or value is None:
            continue
        run["planned"] = max(run["planned"], int(value))
        for key in ("suite", "subject", "split"):
            run[key] = run[key] or str(labels.get(f"eval_{key}", ""))
        proc_of(labels)
    for name, field in (("carried", "carried"), ("carried_cost", "carried_cost"), ("cost", "cost")):
        for sample in results.get(name, []):
            labels, value = sample.get("metric", {}), _value(sample)
            if run_of(labels) is not None and value is not None:
                proc_of(labels)[field] += value
    for sample in results.get("cases", []):
        labels, value = sample.get("metric", {}), _value(sample)
        run = run_of(labels)
        if run is None or value is None:
            continue
        outcome = str(labels.get("eval_outcome", ""))
        run["outcomes"][outcome] = run["outcomes"].get(outcome, 0) + int(value)
        if outcome != "error":
            proc_of(labels)["done"] += value
    for sample in results.get("finished", []):
        labels = sample.get("metric", {})
        run = run_of(labels)
        if run is not None and (_value(sample) or 0) > 0:
            run["finished"] = True
            run["model"] = run["model"] or str(labels.get("eval_model", ""))
    for sample in results.get("valid", []):
        labels = sample.get("metric", {})
        run = run_of(labels)
        if run is not None and labels.get("eval_valid") in ("true", "false"):
            run["valid"] = labels["eval_valid"] == "true" and run["valid"] is not False
    for sample in results.get("scores", []):
        labels, value = sample.get("metric", {}), _value(sample)
        run = run_of(labels)
        if run is not None and value is not None and labels.get("eval_metric"):
            run["scores"][str(labels["eval_metric"])] = round(value, 6)
    for name, field in (("started", "started_at"), ("updated", "updated_at")):
        for sample in results.get(name, []):
            run, value = run_of(sample.get("metric", {})), _value(sample)
            if run is not None and value is not None:
                run[field] = value
    for (rid, _), p in procs.items():
        run = runs.get(rid)
        if run is None:
            continue
        run["done"] = max(run["done"], int(round(p["carried"] + p["done"])))
        run["cost_usd"] = max(run["cost_usd"], round(p["carried_cost"] + p["cost"], 6))
    rows = []
    for run in runs.values():
        if not run["suite"]:
            continue  # 只有零星的分数、没有开始记录的（例如别的版本发的）不列
        updated = run["updated_at"]
        run["status"] = "finished" if run["finished"] else "stalled" if updated is None or now - updated > STALLED_AFTER_S else "running"
        run["done"] = min(run["done"], run["planned"]) if run["planned"] else run["done"]
        run["started_at"], run["updated_at"] = _iso(run["started_at"]), _iso(updated)
        rows.append(run)
    rows.sort(key=lambda r: (r["status"] != "running", -(datetime.fromisoformat(r["updated_at"]).timestamp() if r["updated_at"] else 0)))
    return rows[:MAX_RUNS]


class LiveSource:
    """/api/live.json 的数据：按写死的查询读 VictoriaMetrics，缓存 LIVE_TTL_S 秒；连不上时给最后一份并标明。"""

    def __init__(self, metrics_url: str | None, *, fetch: Fetch | None = None, clock: Callable[[], float] = time.time, ttl_s: float = LIVE_TTL_S) -> None:
        url = (metrics_url or "").strip().rstrip("/")
        if url and urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"{METRICS_URL_ENV} 要是 http 或 https 的地址")
        self.url = url
        self.fetch = fetch or _http_get
        self.clock = clock
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._body: bytes | None = None
        self._at = float("-inf")
        self._good: dict[str, Any] | None = None

    def _query(self, query: str) -> list[dict[str, Any]]:
        raw = self.fetch(f"{self.url}/api/v1/query?{urllib.parse.urlencode({'query': query})}", METRICS_TIMEOUT_S)
        data = json.loads(raw)
        if data.get("status") != "success" or not isinstance(data.get("data", {}).get("result"), list):
            raise ValueError("unexpected answer")
        return list(data["data"]["result"])

    def get(self) -> bytes:
        with self._lock:
            now = self.clock()
            if self._body is not None and now - self._at < self.ttl_s:
                return self._body
            doc: dict[str, Any]
            if not self.url:
                doc = {"source": "none", "stale": False, "generated_at": _iso(now), "runs": []}
            else:
                try:
                    results = {name: self._query(q) for name, q in QUERIES.items()}
                    doc = {"source": "metrics", "stale": False, "generated_at": _iso(now), "runs": combine(results, now)}
                    self._good = doc
                except Exception:  # noqa: BLE001 — 只说连不上，不回显地址与异常（可能带内部地址）
                    doc = {**(self._good or {"source": "metrics", "generated_at": None, "runs": []}), "stale": True, "error": "metrics_unreachable"}
            self._body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
            self._at = now
            return self._body


def make_server(root: Path, host: str, port: int, *, live: LiveSource | None = None) -> ThreadingHTTPServer:
    """绑定地址、还没开始服务的 HTTP 服务；port 为 0 时随机分配。live 不给时 /api/live.json 回 source 为 none。"""
    page = Path(root) / PAGE_NAME
    source = live or LiveSource(None)

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def version_string(self) -> str:
            return "eval-dashboard"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — 基类的参数名
            return

        def _reply(self, status: int, body: bytes, content_type: str, **headers: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, status: int, **headers: str) -> None:
            self._reply(status, (HTTPStatus(status).phrase.lower() + "\n").encode(), "text/plain; charset=utf-8", **headers)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # 基类的错误页会回显请求里的内容（方法、版本），这里只说状态。
            self._text(code)

        def _get(self) -> None:
            path = urlsplit(self.path).path
            if path == HEALTH_PATH:
                self._reply(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8", Cache_Control="no-store")
                return
            if path == LIVE_PATH:
                # 查询参数一概不用：查什么在服务端写死。
                self._reply(HTTPStatus.OK, source.get(), "application/json; charset=utf-8", Cache_Control=LIVE_CACHE_CONTROL)
                return
            if path not in PAGE_PATHS:
                self._text(HTTPStatus.NOT_FOUND)
                return
            try:
                body = page.read_bytes()
            except OSError:
                self._text(HTTPStatus.NOT_FOUND)
                return
            self._reply(HTTPStatus.OK, body, "text/html; charset=utf-8", Cache_Control=PAGE_CACHE_CONTROL)

        def _not_allowed(self) -> None:
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, Allow="GET, HEAD")

        do_GET = do_HEAD = _get  # noqa: N815 — 基类按方法名分派
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.evals.dashboard_server", description="评测看板的静态服务")
    parser.add_argument("--dir", type=Path, required=True, help="放 index.html 的目录")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — 在 Pod 里监听，入站由 NetworkPolicy 限定
    parser.add_argument("--metrics-url", default=os.environ.get(METRICS_URL_ENV, ""), help=f"VictoriaMetrics 的地址（默认取 {METRICS_URL_ENV}）")
    args = parser.parse_args(argv)
    with make_server(args.dir, args.host, args.port, live=LiveSource(args.metrics_url)) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
