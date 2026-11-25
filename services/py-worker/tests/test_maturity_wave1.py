"""Wave-1 maturity tests: L1 repo_ops, L2 self_ops, L3 web_search/web_fetch, prompts V2, and 10 platform skills."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.providers.tool_driver import ToolInvocation
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.prompts import EXECUTOR_IDENTITY, PLANNER_IDENTITY, REVIEWER_IDENTITY, build_system_identity
from agentruntime_worker.tools import dev_tools
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.self_ops import TodoStore
from agentruntime_worker.tools.web import DuckDuckGoLiteProvider, SelfHostedCrawlerProvider, TavilyProvider, extract_readable, resolve_search_provider
from agentruntime_worker.workflows.verification import only_reads

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
T1_W2 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w2")
T2 = TenantContext(tenant_id="t2", user_id="u2", workspace_id="w1")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    ws = tmp_path / "workspaces" / "t1" / "w1"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "src" / "app.py").write_text(
        "def alpha(x: int) -> int:\n    return x + 1\n\nclass Runner:\n    def run(self) -> str:\n        return 'ok'\n",
        encoding="utf-8",
        newline="\n",
    )
    (ws / "src" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    (ws / "docs" / "guide.md").write_text("# Guide\nalpha usage\n", encoding="utf-8", newline="\n")
    return tmp_path


def _setup(repo: Path) -> LocalToolDriverProvider:
    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(workspace_root=repo / "workspaces", fetch_hosts=("*",)))
    return provider


# --- L1 enhancements: repo_read / repo_grep / repo_edit / repo_glob / apply_patch / repo_git ---


async def test_l1_read_grep_edit_and_glob(repo: Path) -> None:
    drv = _setup(repo).bind(T1)

    read_out = (await drv.invoke("repo_read", "v1", {"path": "src/app.py", "with_line_numbers": True, "max_lines": 2})).output
    assert "   1: def alpha(x: int) -> int:" in read_out["content"]

    grep_out = (
        await drv.invoke(
            "repo_grep",
            "v1",
            {"pattern": "ALPHA", "glob": "*.py", "case_sensitive": False, "context_lines": 1},
        )
    ).output
    assert len(grep_out["matches"]) == 1
    assert grep_out["matches"][0]["path"] == "src/app.py"
    assert len(grep_out["matches"][0]["context_after"]) == 1

    await drv.invoke(
        "repo_edit",
        "v1",
        {"path": "src/app.py", "old": "return", "new": "yield", "replace_all": True},
        idempotency_key="edit-all",
    )
    updated = (repo / "workspaces" / "t1" / "w1" / "src" / "app.py").read_text("utf-8")
    assert updated.count("yield") == 2 and "return" not in updated

    glob_out = (await drv.invoke("repo_glob", "v1", {"pattern": "**/*.py"})).output
    assert glob_out["paths"] == ["src/app.py", "src/helper.py"]

    with pytest.raises(InvalidError):
        await drv.invoke("repo_glob", "v1", {"pattern": "../**/*.py"})


async def test_apply_patch_multi_file_and_atomic_rollback(repo: Path) -> None:
    drv = _setup(repo).bind(T1)
    ws = repo / "workspaces" / "t1" / "w1"

    patch = """--- a/src/helper.py
+++ b/src/helper.py
@@ -1,1 +1,2 @@
 VALUE = 1
+VALUE_TWO = 2
--- /dev/null
+++ b/src/new_mod.py
@@ -0,0 +1,2 @@
+def greet() -> str:
+    return "hi"
--- a/docs/guide.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# Guide
-alpha usage
"""
    res = (await drv.invoke("apply_patch", "v1", {"patch": patch}, idempotency_key="patch-ok")).output
    assert res["applied"] == 3
    assert "VALUE_TWO = 2" in (ws / "src" / "helper.py").read_text("utf-8")
    assert (ws / "src" / "new_mod.py").read_text("utf-8").startswith("def greet")
    assert not (ws / "docs" / "guide.md").exists()

    bad_patch = """--- a/src/helper.py
+++ b/src/helper.py
@@ -1,2 +1,2 @@
-VALUE = 1
+VALUE = 999
 VALUE_TWO = 2
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,1 @@
-NON_EXISTENT_LINE
+BROKEN
"""
    with pytest.raises(InvalidError):
        await drv.invoke("apply_patch", "v1", {"patch": bad_patch}, idempotency_key="patch-fail")
    assert "VALUE = 1" in (ws / "src" / "helper.py").read_text("utf-8"), "失败时第 1 个文件不能被部分写入"

    git_patch = """--- a/.git/HEAD
+++ b/.git/HEAD
@@ -1,0 +1,1 @@
+ref: refs/heads/evil
"""
    with pytest.raises(DeniedError):
        await drv.invoke("apply_patch", "v1", {"patch": git_patch}, idempotency_key="patch-git")


async def test_repo_git_readonly_and_guards(repo: Path) -> None:
    ws = repo / "workspaces" / "t1" / "w1"
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=ws, check=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

    drv = _setup(repo).bind(T1)
    status = (await drv.invoke("repo_git", "v1", {"subcommand": "status", "args": ["--short"]})).output
    assert status["exit_code"] == 0
    log = (await drv.invoke("repo_git", "v1", {"subcommand": "log", "args": ["-n", "1", "--oneline"]})).output
    assert "init" in log["stdout"]

    with pytest.raises(InvalidError):
        await drv.invoke("repo_git", "v1", {"subcommand": "commit", "args": ["-m", "x"]})
    with pytest.raises(DeniedError):
        await drv.invoke("repo_git", "v1", {"subcommand": "branch", "args": ["-D", "main"]})
    with pytest.raises(DeniedError):
        await drv.invoke("repo_git", "v1", {"subcommand": "log", "args": ["-c", "alias.x=!id"]})


# --- L2 self_ops: todo_write / file_inspect / memory_search ---


async def test_l2_todo_write_and_file_inspect(repo: Path) -> None:
    provider = _setup(repo)
    drv1 = provider.bind(T1)
    drv1_w2 = provider.bind(T1_W2)
    drv2 = provider.bind(T2)

    out1 = (
        await drv1.invoke(
            "todo_write",
            "v1",
            {"todos": [{"id": "s1", "content": "Inspect codebase", "status": "completed"}]},
            idempotency_key="todo-1",
        )
    ).output
    assert out1["counts"]["completed"] == 1

    out_merge = (
        await drv1.invoke(
            "todo_write",
            "v1",
            {"todos": [{"id": "s2", "content": "Patch bug", "status": "in_progress"}], "replace": False},
            idempotency_key="todo-2",
        )
    ).output
    assert out_merge["total"] == 2
    assert [i["id"] for i in out_merge["items"]] == ["s1", "s2"]

    other = (
        await drv1_w2.invoke(
            "todo_write",
            "v1",
            {"todos": [{"id": "x1", "content": "Isolated", "status": "pending"}]},
            idempotency_key="todo-3",
        )
    ).output
    assert other["total"] == 1
    assert drv2 is not drv1

    store = TodoStore()
    await store.write(ToolInvocation(arguments={"todos": [{"id": "1", "content": "Task A", "status": "pending"}]}, idempotency_key="t-a", credential=None, tenant_id="t1", workspace_id="w1", task_id="a"))
    out_b = await store.write(ToolInvocation(arguments={"todos": [{"id": "2", "content": "Task B", "status": "completed"}]}, idempotency_key="t-b", credential=None, tenant_id="t1", workspace_id="w1", task_id="b"))
    assert out_b["total"] == 1 and out_b["items"][0]["id"] == "2"

    insp = (await drv1.invoke("file_inspect", "v1", {"path": "src/app.py"})).output
    assert insp["language"] == "python" and not insp["is_binary"]
    assert insp["line_count"] == 6
    assert [s["name"] for s in insp["symbols"]] == ["alpha", "Runner", "run"]


async def test_l2_memory_search_registered_when_memory_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = WorkerConfig(data_dir=tmp_path, memory_address="127.0.0.1:50061", dev_signing_key="test-secret")
    provider = dev_tools(cfg)
    names = {s.name for s in await provider.bind(T1).list_tools()}
    assert "memory_search" in names

    from google.protobuf import json_format

    from agentruntime.memory.v1 import memory_pb2
    from agentruntime_worker.memory import WorkerMemoryClient

    async def fake_search(self: Any, request: memory_pb2.SearchRequest) -> memory_pb2.SearchResponse:
        assert request.tenant.tenant_id == "t1" and request.trigger == memory_pb2.SEARCH_TRIGGER_NODE_START
        item = memory_pb2.RetrievedItem(score=0.95)
        item.memory_item.schema_id = "memory"
        json_format.ParseDict(
            {
                "id": "m1",
                "kind": "lesson",
                "scope": "tenant",
                "trust": 0.92,
                "content": {"title": "Use apply_patch", "guidance": "Prefer atomic patches"},
            },
            item.memory_item.value,
        )
        return memory_pb2.SearchResponse(items=[item])

    monkeypatch.setattr(WorkerMemoryClient, "search", fake_search)
    res = (await provider.bind(T1).invoke("memory_search", "v1", {"query": "patch atomicity", "top_k": 3})).output
    assert res["total"] == 1 and res["hits"][0]["id"] == "m1" and res["hits"][0]["title"] == "Use apply_patch"


# --- L3 web_fetch readable/redirect & web_search providers ---


class _SearchAndFetchHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/hop1":
            self.send_response(302)
            self.send_header("Location", "/article")
            self.end_headers()
            return
        if parsed.path == "/article":
            body = b"<html><head><style>.x{}</style><script>alert(1)</script></head><body><nav>menu</nav><main><h1>Title</h1><p>Clean body content.</p></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/ddg":
            html_body = (
                '<html><body>'
                '<a class="result__a" href="/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">Python Docs</a>'
                '<a class="result__snippet">Official Python documentation</a>'
                '</body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_body)))
            self.end_headers()
            self.wfile.write(html_body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        if self.path == "/v1/search" or self.path == "/tavily":
            payload = json.dumps({
                "results": [
                    {"title": "Allowed Doc", "url": "https://docs.python.org/3/library/urllib.html", "content": "urllib info", "score": 0.98},
                    {"title": "Other Site", "url": "https://example.org/other", "content": "filtered out"},
                ]
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def mock_web() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _SearchAndFetchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


async def test_web_fetch_readable_and_safe_multihop(repo: Path, mock_web: str) -> None:
    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(fetch_hosts=("127.0.0.1",), allow_private_fetch=True))
    out = (await provider.bind(T1).invoke("web_fetch", "v1", {"url": f"{mock_web}/hop1"})).output
    assert out["status"] == 200
    assert out["redirects"] == [f"{mock_web}/article"]
    assert "Clean body content." in out["text"]
    assert "alert(1)" not in out["text"] and "menu" not in out["text"]
    assert "Raw" not in extract_readable("<script>hide</script><p>Visible</p>")
    assert extract_readable("<script>hide</script><p>Visible</p>") == "Visible"


async def test_web_search_providers_and_fallback(mock_web: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ddg = DuckDuckGoLiteProvider(endpoint=f"{mock_web}/ddg")
    ddg_res = await ddg.search("python", max_results=5)
    assert len(ddg_res) == 1 and ddg_res[0].url == "https://docs.python.org/3/" and "Official" in ddg_res[0].snippet

    assert isinstance(resolve_search_provider(env={}), DuckDuckGoLiteProvider)
    assert isinstance(resolve_search_provider(secret_value=b"tvly-123", env={}), TavilyProvider)
    assert isinstance(resolve_search_provider(env={"AR_CRAWLER_ENDPOINT": mock_web}), SelfHostedCrawlerProvider)

    crawler = SelfHostedCrawlerProvider(mock_web)
    crawler_res = await crawler.search("urllib", max_results=5)
    assert len(crawler_res) == 2

    monkeypatch.setenv("AR_CRAWLER_ENDPOINT", mock_web)
    provider = LocalToolDriverProvider()
    register_platform_tools(provider, PlatformToolsConfig(fetch_hosts=("*",)))
    out = (
        await provider.bind(T1).invoke(
            "web_search",
            "v1",
            {"query": "urllib", "include_domains": ["python.org"]},
        )
    ).output
    assert out["provider"] == "self_hosted_crawler"
    assert out["total"] == 1 and out["results"][0]["domain"] == "docs.python.org"


# --- Verification & Prompts & 10 Skills Schema ---


def test_only_reads_includes_l1_l2_l3_read_tools() -> None:
    for name, args in (
        ("repo_glob", {"pattern": "**/*.py"}),
        ("repo_git", {"subcommand": "status"}),
        ("file_inspect", {"path": "a.py"}),
        ("memory_search", {"query": "x"}),
        ("web_search", {"query": "y"}),
        ("skill_read", {"name": "bugfix-tdd"}),
    ):
        assert only_reads(name, args), f"{name} 应当计入只读行为"
    assert not only_reads("apply_patch", {"patch": "--- a/x\n+++ b/x\n"})


def test_prompts_v2_contain_tiers_and_boundaries() -> None:
    assert "apply_patch" in EXECUTOR_IDENTITY and "todo_write" in EXECUTOR_IDENTITY and "web_search" in EXECUTOR_IDENTITY
    assert "effect_ceiling" in EXECUTOR_IDENTITY
    assert "web_search" in PLANNER_IDENTITY
    assert "apply_patch" in REVIEWER_IDENTITY
    rendered = build_system_identity("executor", runtime_context="【可用技能】bugfix-tdd")
    assert rendered.startswith(EXECUTOR_IDENTITY) and "bugfix-tdd" in rendered


def test_platform_skills_all_10_match_skill_schema() -> None:
    root = Path(__file__).resolve().parents[3]
    skills_doc = json.loads((root / "deploy" / "dev" / "platform-skills.json").read_text("utf-8"))
    entries = skills_doc["entries"]
    assert len(entries) == 10
    names = {e["name"] for e in entries}
    assert {
        "explore-repo",
        "coding-engineering",
        "bugfix-tdd",
        "write-tests",
        "refactor-safely",
        "docs-architecture",
        "eval-authoring",
        "security-review",
        "commit-and-pr",
        "deliver-files",
    } == names

    schemas_dir = root / "schemas"
    skill_schema = json.loads((schemas_dir / "skill.schema.json").read_text("utf-8"))
    defs_schema = json.loads((schemas_dir / "defs.schema.json").read_text("utf-8"))
    registry_store = {
        skill_schema["$id"]: skill_schema,
        "https://agentruntime.example/schemas/defs.schema.json": defs_schema,
        "defs.schema.json": defs_schema,
    }
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    reg = Registry().with_resources([(uri, DRAFT202012.create_resource(doc)) for uri, doc in registry_store.items()])
    validator = jsonschema.Draft202012Validator(skill_schema, registry=reg)
    for entry in entries:
        assert entry["kind"] == "skill"
        validator.validate(entry["document"])
