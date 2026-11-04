"""SITE-SMOKE（docs/plan/eval-site.md 第 5 节最后一行；总表 SITE-UI）：评测网站三页在真浏览器里打得开、用得了。

真的 dashboard_server 读仓库里的 evalsets；测试在它前面加一层代理，给页面注入一段探针，用 headless 的 Chrome 或 Edge 打开
（找不到就跳过；AR_TEST_BROWSER 可以指定）。探针做完把结果 POST 回代理。

1. 主页：没有脚本错误；题量的数字与 evalsets 一致；中英切换。
2. 数据集页：没有脚本错误；显示的题数与 evalsets 一致；按数据集分组时每个数据集一组；搜索能筛；展开一题有内容；中英切换。
3. 手机宽度（390）：主页与数据集页都不横向滚动，展开一道成对比较的题也不。
4. /results 能打开、没有脚本错误。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.evals.dashboard_server import make_server
from agentruntime_learning.evals.site_catalog import build_catalog
from agentruntime_learning.evals.site_sync import SiteSync

REPO = Path(__file__).resolve().parents[3]
EVALSETS = REPO / "services" / "py-learning" / "evalsets"
DASHBOARD_SCRIPT = REPO / "scripts" / "eval_dashboard.py"


def find_browser() -> str | None:
    if os.environ.get("AR_TEST_BROWSER"):
        return os.environ["AR_TEST_BROWSER"]
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge", "microsoft-edge"):
        if found := shutil.which(name):
            return found
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
            if root and (Path(root) / rel).is_file():
                return str(Path(root) / rel)
    return None


BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="没有找到 Chrome 或 Edge（可以用 AR_TEST_BROWSER 指定）")

# 探针共用的部分：收集脚本错误，等条件成立，最后把结果交回。
PROBE_HEAD = """
const out = { errors: [], steps: {} };
addEventListener("error", (e) => out.errors.push(String(e.message)));
addEventListener("unhandledrejection", (e) => out.errors.push(String(e.reason && e.reason.stack || e.reason)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = async (fn, ms = 15000) => { const end = Date.now() + ms; for (;;) { let v; try { v = fn(); } catch (e) { v = null; } if (v) return v; if (Date.now() > end) return null; await sleep(50); } };
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const overflow = () => document.documentElement.scrollWidth - document.documentElement.clientWidth;
const finish = () => fetch("/__result", { method: "POST", body: JSON.stringify(out) });
"""

HOME_PROBE = PROBE_HEAD + """
(async () => {
  try {
    await wait(() => $$("#set-stats .stat").length >= 3);
    out.steps.stats = $$("#set-stats .stat").map((s) => s.textContent);
    out.steps.overflow = overflow();
    $("#lang").click();
    await sleep(100);
    out.steps.lang = document.documentElement.lang;
    out.steps.title = $("h1").textContent;
  } catch (ex) { out.errors.push(String(ex && ex.stack || ex)); }
  await finish();
})();
"""

DATASETS_PROBE = PROBE_HEAD + """
const shown = () => { const m = ($("#shown").textContent || "").match(/\\d+/); return m ? Number(m[0]) : null; };
const regroup = async (value) => { const sel = $("#grouping"); sel.value = value; sel.dispatchEvent(new Event("change")); await sleep(100); };
(async () => {
  try {
    await wait(() => $$("#groups section.group").length > 0, 30000);
    out.steps.shown = shown();
    out.steps.groups = $$("#groups section.group").length;
    out.steps.overflow = overflow();
    await regroup("dataset");
    out.steps.dataset_groups = $$("#groups section.group").length;
    const pair = $$("details.row").find((d) => d.closest("section.group") && d.closest("section.group").querySelector("h3").textContent.includes("JudgeBench"));
    const first = pair || $("details.row");
    first.querySelector("summary").click();
    await wait(() => first.querySelector(".body"));
    out.steps.expanded = !!first.querySelector(".body dl") && first.open;
    out.steps.expanded_pairwise = !!pair && !!first.querySelector(".blk-candidates");
    await sleep(100);
    out.steps.overflow_expanded = overflow();
    const search = $("#search"); search.value = "astropy"; search.dispatchEvent(new Event("input")); await sleep(100);
    out.steps.searched = shown();
    search.value = ""; search.dispatchEvent(new Event("input"));
    $("#lang").click(); await sleep(100);
    out.steps.lang = document.documentElement.lang;
    out.steps.title = $("h1").textContent;
  } catch (ex) { out.errors.push(String(ex && ex.stack || ex)); }
  await finish();
})();
"""

RESULTS_PROBE = PROBE_HEAD + """
(async () => {
  await sleep(1500);
  out.steps.title = document.title;
  out.steps.overflow = overflow();
  await finish();
})();
"""


class Site:
    """真的看板服务，前面一层代理：给页面注入探针，收探针的结果。"""

    def __init__(self, pages: Path) -> None:
        self.upstream = make_server(pages, "127.0.0.1", 0, site=SiteSync.local(EVALSETS))
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        up = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.probe = ""
        self.results: list[dict[str, Any]] = []
        self.done = threading.Event()
        site = self

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 — http.server 的方法名
                if self.path == "/__probe.js":
                    return self._send(200, site.probe.encode(), "text/javascript")
                try:
                    with urllib.request.urlopen(up + self.path, timeout=30) as resp:
                        body, status, kind = resp.read(), resp.status, resp.headers.get("Content-Type", "")
                except urllib.error.HTTPError as err:
                    body, status, kind = err.read(), err.code, err.headers.get("Content-Type", "")
                if kind.startswith("text/html"):
                    body = body.replace(b"</body>", b'<script src="/__probe.js"></script></body>', 1)
                self._send(status, body, kind)

            def do_POST(self) -> None:  # noqa: N802
                data = self.rfile.read(int(self.headers["Content-Length"]))
                site.results.append(json.loads(data))
                site.done.set()
                self._send(200, b"ok", "text/plain")

            def _send(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.proxy.server_address[1]}"

    def open(self, path: str, probe: str, profile: Path, *, width: int = 1400, height: int = 900) -> dict[str, Any]:
        """用 headless 浏览器打开 path，等探针交回结果。"""
        self.probe, self.done = probe, threading.Event()
        assert BROWSER is not None
        cmd = [BROWSER, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", "--no-default-browser-check",
               f"--window-size={width},{height}", f"--user-data-dir={profile}", self.url + path]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert self.done.wait(90), f"等不到 {path} 的探针结果"
        finally:
            proc.kill()
            proc.wait(30)
        return self.results[-1]

    def close(self) -> None:
        self.proxy.shutdown()
        self.upstream.shutdown()


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    pages = tmp_path_factory.mktemp("pages")
    reports = tmp_path_factory.mktemp("reports")
    proc = subprocess.run([sys.executable, str(DASHBOARD_SCRIPT), "--public", "--reports", str(reports), "--out", str(pages / "index.html")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    s = Site(pages)
    yield s
    s.close()


@pytest.fixture(scope="module")
def expected() -> dict[str, int]:
    catalog = build_catalog(EVALSETS)
    shown = sum(d["counts"]["shown"] for d in catalog.datasets)
    return {"shown": shown, "datasets": sum(1 for d in catalog.datasets if d["counts"]["shown"])}


def test_the_home_page(site: Site, expected: dict[str, int], tmp_path: Path) -> None:
    r = site.open("/", HOME_PROBE, tmp_path / "home")
    assert not r["errors"], r["errors"]
    assert any(str(expected["shown"]) in s for s in r["steps"]["stats"]), f"公开的题数与 evalsets 一致：{r['steps']['stats']}"
    assert r["steps"]["lang"] == "en" and r["steps"]["title"] == "Evaluations"
    assert r["steps"]["overflow"] <= 1


def test_the_datasets_page(site: Site, expected: dict[str, int], tmp_path: Path) -> None:
    r = site.open("/datasets", DATASETS_PROBE, tmp_path / "datasets")
    steps = r["steps"]
    assert not r["errors"], r["errors"]
    assert steps["shown"] == expected["shown"], "显示的题数与 evalsets 一致"
    assert steps["groups"] >= 5, "默认按评测类别分组"
    assert steps["dataset_groups"] == expected["datasets"], "按数据集分组时每个数据集一组"
    assert steps["expanded"] and steps["expanded_pairwise"], "展开一道成对比较的题，看得到两个回答"
    assert 0 < steps["searched"] < expected["shown"], "搜索能筛"
    assert steps["lang"] == "en" and steps["title"] == "Datasets and frameworks"


@pytest.mark.parametrize("path, probe", [("/", HOME_PROBE), ("/datasets", DATASETS_PROBE)], ids=["home", "datasets"])
def test_phone_width_does_not_scroll_sideways(site: Site, tmp_path: Path, path: str, probe: str) -> None:
    r = site.open(path, probe, tmp_path / "phone", width=390, height=844)
    assert not r["errors"], r["errors"]
    assert r["steps"]["overflow"] <= 1, f"{path} 在 390 宽时横向多出 {r['steps']['overflow']} 像素"
    if "overflow_expanded" in r["steps"]:
        assert r["steps"]["overflow_expanded"] <= 1, f"展开一道成对比较的题后横向多出 {r['steps']['overflow_expanded']} 像素"


def test_the_results_page_opens(site: Site, tmp_path: Path) -> None:
    r = site.open("/results", RESULTS_PROBE, tmp_path / "results")
    assert not r["errors"], r["errors"]
    assert r["steps"]["title"]
