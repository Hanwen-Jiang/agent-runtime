"""docs/plan/eval-observability-and-runtime-fixes.md 第 11.16 节：评测看板的 /api/live.json。

- 只用写死的几条查询读 VictoriaMetrics（假的 VictoriaMetrics 记下收到的查询），客户端给的查询参数一概不用；只查 runtime_eval_*。
- 合成每次运行一行：进度（续跑的几个进程也算对）、各种结果的条数、花费、分数、状态（进行中、已结束、停了）。
- 缓存 20 秒；VictoriaMetrics 连不上时返回最后一份并标明 stale，不回显地址与异常；没配地址时 source 为 none。
- CSP 带 connect-src 'self'；静态页照旧。
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.evals import dashboard_server as ds

NOW = 1_800_000_000.0
VM = "http://vm.observability.svc:8428"


def sample(value: float, ts: float = NOW, **labels: str) -> dict[str, Any]:
    return {"metric": labels, "value": [ts, str(value)]}


def series() -> dict[str, list[dict[str, Any]]]:
    """两次运行：task-loop 跑完了；SWE-bench 分两个进程（第一个崩了，第二个续跑，正在跑）。"""
    a = {"eval_run_id": "20261102T1000Z-worker-ab12", "eval_process": "p1"}
    b1 = {"eval_run_id": "sol-rev18", "eval_process": "q1"}
    b2 = {"eval_run_id": "sol-rev18", "eval_process": "q2"}
    meta_a = {"eval_suite": "task-loop", "eval_subject": "worker", "eval_split": "dev"}
    meta_b = {"eval_suite": "swebench", "eval_subject": "gpt-6.1-sol", "eval_split": "verified"}
    return {
        "planned": [sample(26, **a, **meta_a), sample(50, **b1, **meta_b), sample(50, **b2, **meta_b)],
        "carried": [sample(12, **b2)],
        "carried_cost": [sample(7.5, **b2)],
        "cases": [
            sample(9, **a, eval_outcome="passed"), sample(17, **a, eval_outcome="failed"),
            sample(12, **b1, eval_outcome="submitted"), sample(1, **b1, eval_outcome="error"), sample(5, **b2, eval_outcome="submitted"),
        ],
        "cost": [sample(0.2, **a), sample(7.5, **b1), sample(3.0, **b2)],
        "finished": [sample(1, **a, eval_model="anthropic/claude-sonnet-5-5")],
        "valid": [sample(1, eval_run_id=a["eval_run_id"], eval_valid="true")],
        "scores": [sample(0.346, eval_run_id=a["eval_run_id"], eval_metric="success_rate")],
        "started": [sample(NOW - 3600, eval_run_id=a["eval_run_id"]), sample(NOW - 7200, eval_run_id="sol-rev18")],
        "updated": [sample(NOW - 1800, eval_run_id=a["eval_run_id"]), sample(NOW - 60, eval_run_id="sol-rev18")],
    }


class FakeVM:
    def __init__(self, data: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.data = data if data is not None else series()
        self.queries: list[str] = []
        self.down = False

    def __call__(self, url: str, timeout_s: float) -> bytes:
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == VM and parts.path == "/api/v1/query"
        [query] = urllib.parse.parse_qs(parts.query)["query"]
        self.queries.append(query)
        if self.down:
            raise OSError(f"connection refused to {VM}")
        name = next(k for k, q in ds.QUERIES.items() if q == query)
        return json.dumps({"status": "success", "data": {"resultType": "vector", "result": self.data.get(name, [])}}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def live(vm: FakeVM, clock: Clock | None = None) -> ds.LiveSource:
    return ds.LiveSource(VM, fetch=vm, clock=clock or Clock())


def doc(source: ds.LiveSource) -> dict[str, Any]:
    return json.loads(source.get())


def test_only_the_fixed_queries_are_sent() -> None:
    vm = FakeVM()
    doc(live(vm))
    assert sorted(vm.queries) == sorted(ds.QUERIES.values())
    for q in vm.queries:
        names = {n for n in q.replace("(", " ").replace("[", " ").replace("{", " ").replace("|", " ").replace('"', " ").split() if n.startswith(("runtime_", "gen_ai"))}
        assert all(n.startswith("runtime_eval_") for n in names), f"只查评测的指标：{q}"


def test_runs_are_combined_from_the_series() -> None:
    d = doc(live(FakeVM()))
    assert d["source"] == "metrics" and d["stale"] is False
    runs = {r["run_id"]: r for r in d["runs"]}
    a, b = runs["20261102T1000Z-worker-ab12"], runs["sol-rev18"]
    assert (a["suite"], a["subject"], a["split"], a["model"]) == ("task-loop", "worker", "dev", "anthropic/claude-sonnet-5-5")
    assert (a["planned"], a["done"], a["outcomes"], a["status"], a["valid"]) == (26, 26, {"passed": 9, "failed": 17}, "finished", True)
    assert a["scores"] == {"success_rate": 0.346} and a["cost_usd"] == pytest.approx(0.2)
    assert b["status"] == "running" and b["planned"] == 50
    assert b["done"] == 17, "续跑：第二个进程带过来的 12 题加它跑完的 5 题（出错的不算跑完）"
    assert b["cost_usd"] == pytest.approx(10.5), "带过来的 7.5 加这个进程的 3.0"
    assert b["outcomes"] == {"submitted": 17, "error": 1}
    assert d["runs"][0]["run_id"] == "sol-rev18", "进行中的排在前面"
    assert a["updated_at"] == "2027-01-15T07:30:00Z"


def test_a_run_without_news_for_long_is_stalled() -> None:
    data = series()
    data["updated"] = [sample(NOW - ds.STALLED_AFTER_S - 10, eval_run_id="sol-rev18")]
    runs = {r["run_id"]: r for r in doc(live(FakeVM(data)))["runs"]}
    assert runs["sol-rev18"]["status"] == "stalled"


def test_results_are_cached_for_twenty_seconds() -> None:
    vm, clock = FakeVM(), Clock()
    source = live(vm, clock)
    doc(source)
    sent = len(vm.queries)
    clock.now += 19
    doc(source)
    assert len(vm.queries) == sent, "20 秒内不再查"
    clock.now += 2
    doc(source)
    assert len(vm.queries) == 2 * sent


def test_when_the_metrics_store_is_down_the_last_answer_is_kept_and_marked() -> None:
    vm, clock = FakeVM(), Clock()
    source = live(vm, clock)
    good = doc(source)
    vm.down = True
    clock.now += 30
    body = source.get()
    d = json.loads(body)
    assert d["stale"] is True and d["error"] == "metrics_unreachable" and d["runs"] == good["runs"]
    assert b"vm.observability" not in body and b"refused" not in body, "不回显地址与异常"


def test_down_from_the_start_gives_an_empty_stale_answer() -> None:
    vm = FakeVM()
    vm.down = True
    d = doc(live(vm))
    assert d["stale"] is True and d["runs"] == []


def test_without_an_address_there_is_no_live_data() -> None:
    d = doc(ds.LiveSource(None))
    assert d["source"] == "none" and d["runs"] == []
    with pytest.raises(ValueError):
        ds.LiveSource("file:///etc/passwd")


# --- 经 HTTP ---


@pytest.fixture
def site(tmp_path: Path) -> Iterator[tuple[int, Path, FakeVM]]:
    root = tmp_path / "site"
    root.mkdir()
    vm = FakeVM()
    server = ds.make_server(root, "127.0.0.1", 0, live=live(vm))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], root, vm
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_the_endpoint_ignores_client_queries(site: tuple[int, Path, FakeVM]) -> None:
    port, _, vm = site
    status, headers, body = request(port, "/api/live.json?query=" + urllib.parse.quote('runtime_model_cost_usd_sum{tenant_id!=""}'))
    assert status == 200 and headers["content-type"] == "application/json; charset=utf-8"
    assert sorted(set(vm.queries)) == sorted(ds.QUERIES.values()), "客户端给的查询一条也不转发"
    assert json.loads(body)["runs"]
    assert "connect-src 'self'" in headers["content-security-policy"]
    assert headers["cache-control"] == ds.LIVE_CACHE_CONTROL


def test_the_static_page_is_still_served(site: tuple[int, Path, FakeVM]) -> None:
    port, root, _ = site
    (root / "index.html").write_text("<!doctype html><title>x</title>", "utf-8")
    status, headers, body = request(port, "/")
    assert status == 200 and body.startswith(b"<!doctype html>")
    assert "connect-src 'self'" in headers["content-security-policy"]
    assert request(port, "/api/other.json")[0] == 404
