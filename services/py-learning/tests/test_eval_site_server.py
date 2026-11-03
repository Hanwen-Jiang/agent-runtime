"""评测网站（docs/plan/eval-site.md 第 2、4 节）：dashboard_server 的新路径。

三页：/ 主页、/results（原来的看板，页面仍是 --dir 下的 index.html）、/datasets；数据：/api/site.json（索引、框架、同步状态）、
/api/datasets/<名字>.json（名字只认索引里有的）。主页与 /datasets 的页面是镜像里的静态壳，内容全部来自接口。
其余路径 404，请求的路径不拼进文件名；/index.html 永久跳到 /results（旧链接）。

实现时 test_eval_dashboard_server.py 里“/ 是看板”的两条要改成 /results（那是本计划有意的改动）。
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentruntime_learning.evals.dashboard_server import make_server
from agentruntime_learning.evals.site_catalog import build_catalog
from agentruntime_learning.evals.site_sync import SiteSync

RESULTS_PAGE = "<!doctype html><title>Eval Dashboard</title>"


def request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@pytest.fixture
def evalsets(tmp_path: Path) -> Path:
    root = tmp_path / "evalsets"
    case = {"kind": "task", "id": "own_a", "split": "dev", "category": "general",
            "review": {"status": "confirmed", "confirmed_by": "Hanwen-Jiang", "confirmed_at": "2026-10-20"},
            "description": "d", "planner": {"input": {"message": "plan my week", "context": {}},
                                            "expect": {"triage": "full", "risk": "none", "shape": {}}},
            "rounds": [], "rubric": []}
    (root / "own" / "task_cases").mkdir(parents=True)
    (root / "own" / "task_cases" / "a.json").write_text(json.dumps(case), "utf-8")
    (root / "site.json").write_text(json.dumps({
        "frameworks": [{"id": "task-loop", "kind": "authored", "title": {"zh": "自研", "en": "in-house"}}],
        "datasets": [{"name": "own", "path": "own", "source_type": "authored", "framework": "task-loop",
                      "eval_category": "planning", "work_item": "S-09", "title": {"zh": "自编", "en": "own"}}],
    }), "utf-8")
    return root


@pytest.fixture
def site(tmp_path: Path, evalsets: Path) -> Iterator[int]:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "index.html").write_text(RESULTS_PAGE, "utf-8")
    server = make_server(pages, "127.0.0.1", 0, site=SiteSync.local(evalsets))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_three_pages(site: int) -> None:
    for path in ("/", "/datasets"):
        status, headers, body = request(site, "GET", path)
        assert status == 200 and headers["content-type"] == "text/html; charset=utf-8", path
        assert b"<!doctype html>" in body.lower()[:200]
        assert headers["content-security-policy"].startswith("default-src 'none'")
    status, _, body = request(site, "GET", "/results")
    assert status == 200 and body.decode("utf-8") == RESULTS_PAGE, "评测结果仍是发布步骤写的看板"


def test_the_old_link_moves_to_results(site: int) -> None:
    status, headers, _ = request(site, "GET", "/index.html")
    assert status == 301 and headers["location"] == "/results"


def test_the_home_page_links_both_subpages(site: int) -> None:
    _, _, body = request(site, "GET", "/")
    html = body.decode("utf-8")
    assert 'href="/results"' in html and 'href="/datasets"' in html


def test_the_site_index_has_datasets_frameworks_and_sync_status(site: int) -> None:
    status, headers, body = request(site, "GET", "/api/site.json")
    assert status == 200 and headers["content-type"] == "application/json; charset=utf-8"
    doc = json.loads(body)
    assert [d["name"] for d in doc["datasets"]] == ["own"]
    assert doc["frameworks"][0]["id"] == "task-loop"
    assert doc["sync"]["source"] == "local"
    assert headers["cache-control"].startswith("public, max-age="), "可以短暂缓存，几分钟内更新"


def test_a_dataset_is_served_by_name(site: int) -> None:
    status, _, body = request(site, "GET", "/api/datasets/own.json")
    assert status == 200
    doc = json.loads(body)
    assert [i["id"] for i in doc["items"]] == ["own_a"] and doc["items"][0]["question"] == "plan my week"


@pytest.mark.parametrize("path", [
    "/api/datasets/nope.json", "/api/datasets/../site.json", "/api/datasets/%2e%2e%2fown.json", "/api/datasets/own",
    "/api/datasets/OWN.json", "/api/datasets/own.json/x", "/datasets/own", "/static/../../etc/passwd", "/site.json",
])
def test_anything_else_is_404(site: int, path: str) -> None:
    status, _, body = request(site, "GET", path)
    assert status == 404 and b"own_a" not in body


def test_query_strings_change_nothing(site: int) -> None:
    _, _, plain = request(site, "GET", "/api/datasets/own.json")
    _, _, with_query = request(site, "GET", "/api/datasets/own.json?split=holdout&include=all")
    assert plain == with_query, "查询参数一概不用：留出集不能靠参数要出来"


def test_writes_are_refused(site: int) -> None:
    for method in ("POST", "PUT", "DELETE"):
        status, headers, _ = request(site, method, "/api/site.json")
        assert status == 405 and headers["allow"] == "GET, HEAD"


def test_without_a_site_source_the_datasets_api_says_unavailable(tmp_path: Path) -> None:
    pages = tmp_path / "p"
    pages.mkdir()
    server = make_server(pages, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = request(server.server_address[1], "GET", "/api/site.json")
        assert status == 200 and json.loads(body)["sync"]["source"] == "none" and json.loads(body)["datasets"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_catalog_follows_the_synced_copy(tmp_path: Path, evalsets: Path) -> None:
    """数据跟着同步下来的那一份走：evalsets 变了，下一次请求（缓存过期后）就看到新的，不用重启。"""
    catalog_before = build_catalog(evalsets).index()
    (evalsets / "own" / "task_cases" / "b.json").write_text((evalsets / "own" / "task_cases" / "a.json").read_text("utf-8").replace("own_a", "own_b"), "utf-8")
    catalog_after = build_catalog(evalsets).index()
    assert catalog_before["datasets"][0]["counts"]["shown"] == 1 and catalog_after["datasets"][0]["counts"]["shown"] == 2
