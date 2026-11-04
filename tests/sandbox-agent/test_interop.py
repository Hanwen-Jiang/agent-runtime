"""两版之间的互通与对照（D-28 验收 1、2）：

- golden：Go 版导出的存档（golden/go-export.tar，make_golden.py 生成）导进哪一版，清单都与 Go 版当时的清单逐项一致；
- 交叉：Go 导出 → Rust 导入、Rust 导出 → Go 导入，清单逐项一致；
- 对照：同一组请求（清单、列目录、搜索与各种错误）两版的状态码与响应体逐字节相同。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import fixture_tree
import pytest
from conftest import linux_only

GOLDEN = Path(__file__).resolve().parent / "golden"


@linux_only
def test_the_go_golden_archive_imports_into_every_version(make_agent) -> None:  # type: ignore[no-untyped-def]
    want = json.loads((GOLDEN / "go-export.manifest.json").read_bytes())["entries"]
    a = make_agent()
    res = a.request("PUT", "/v1/archive?max_bytes=1048576", (GOLDEN / "go-export.tar").read_bytes())
    assert res.status == 204, res.body
    got = a.request("GET", "/v1/manifest")
    assert got.json()["entries"] == want
    assert got.body == (GOLDEN / "go-export.manifest.json").read_bytes(), "清单逐字节一致"


@linux_only
def test_the_golden_tree_exports_the_same_manifest(make_agent) -> None:  # type: ignore[no-untyped-def]
    a = make_agent()
    import os

    old = os.umask(0o022)
    try:
        fixture_tree.make(a.workdir)
    finally:
        os.umask(old)
    assert a.request("GET", "/v1/manifest").body == (GOLDEN / "go-export.manifest.json").read_bytes()


PAIRS = [(s, d) for s in ("go", "rust") for d in ("go", "rust")]


@pytest.mark.parametrize("src,dst", PAIRS, ids=[f"{s}-to-{d}" for s, d in PAIRS])
def test_archives_cross_between_versions(both, src: str, dst: str) -> None:  # type: ignore[no-untyped-def]
    a = both(src)
    fixture_tree.make(a.workdir)
    res = a.request("GET", "/v1/archive?max_bytes=1048576")
    assert res.status == 200
    b = both(dst)
    imp = b.request("PUT", "/v1/archive?max_bytes=1048576", res.body)
    assert imp.status == 204, imp.body
    assert b.request("GET", "/v1/manifest").body == a.request("GET", "/v1/manifest").body
    again = b.request("GET", "/v1/archive?max_bytes=1048576")
    c = both(src)
    assert c.request("PUT", "/v1/archive?max_bytes=1048576", again.body).status == 204, "再导回原来那一版"
    assert c.request("GET", "/v1/manifest").body == a.request("GET", "/v1/manifest").body


GREP_CORPUS = {
    "ascii.txt": "abc 123 def\nfoo_bar baz\n\tTabbed line\nend.\n",
    "unicode.txt": "Straße STRASSE\n٣ arabic three\nélan vital\nΣίσυφος\nnaïve café\n",
    "crlf.txt": "one\r\ntwo\r\n\r\nthree",
    "invalid.bin.txt": b"ok \xff\xfe bytes\nhalf \xe2\x82 euro\n".decode("latin-1"),
    "long.txt": "start" + "ж" * 700 + "\n",
    "sub/deeper/x.py": "def f():\n    return 42  # answer\n",
    "sub/.git/config": "should not be searched\n",
}
GREP_PATTERNS = [
    "abc", r"\d+", r"\w+", r"\s", r"\bbar\b", r"\Bar", "(?i)strasse", "(?i)straße", r"\pL+", r"\p{Greek}+", r"[[:alpha:]]+", r"[[:^digit:]]",
    r"^end\.$", r"^$", r"\x{FFFD}", ".", r"(?s).", r"\Q.\E", r"a{,3}", r"a{2}", r"(?P<n>foo)", r"(?<n>foo)", r"\z", r"\A", r"\C",
    r"(?U)a+", r"[^\x00-\x7f]", r"\pN", r"\D", r"\W", r"\S", "é", r"(?i)É", "Σ", r"(?i)σ", r"x*", r"\v", r"\f", r"[\d]", r"[\w-]",
    r"[^\s]", r"(?m)^t", r"\x41", r"\101", "(", "[z-a]", "a**", r"(?=x)", r"\1", "a{1001}", r"\8", r"\pX", "(?i", "*", "a{2,1}",
    r"[[:word:]]+", r"\_", r"\e", r"\%", "x{1000}", "(?-u:.)", "(?u)",
    # 随机比对（tests/sandbox-agent 的 fuzz）找出来过的：标志之后的重复作用在前一项上；代理区。
    "b(?U)(?i){1,3}", "a*(?i)*", "ж(?s){1,3}", r"\p{Cs}", r"\P{Cs}", r"[\p{Cs}x]", r"\x{D800}", r"[\x{D7FF}-\x{E000}]", r"\p{greek}",
    r"\p{Old_Italic}", r"(?i)\W", "[^\\x00-\\x{10FFFF}]", "(a{100}){100}", "(a{10}){100}", "(?ii)A", "(?i-i)a", "(?)a", "(?P<1a>x)",
]


def _grep_all(agent: Any) -> list[tuple[int, bytes]]:
    out = []
    for p in GREP_PATTERNS:
        res = agent.request("POST", "/v1/grep", json.dumps({"pattern": p, "max_matches": 1000}).encode())
        out.append((res.status, res.body if res.status == 200 else str(res.code).encode()))
    return out


def test_grep_regex_semantics_match_go(both) -> None:  # type: ignore[no-untyped-def]
    agents = {}
    for which in ("go", "rust"):
        a = both(which)
        for rel, body in GREP_CORPUS.items():
            p = a.workdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode("latin-1") if rel == "invalid.bin.txt" else body.encode())
        agents[which] = _grep_all(a)
    diffs = [(p, g, r) for p, g, r in zip(GREP_PATTERNS, agents["go"], agents["rust"]) if g != r]
    assert not diffs, "\n".join(f"{p!r}: go={g!r}\n    rust={r!r}" for p, g, r in diffs)


_TOKENS = ["a", "b", "é", "ж", "Σ", "1", "٣", " ", ".", "^", "$", "|", "(", ")", "(?:", "(?i)", "(?m)", "(?s)", "(?U)", "(?i:", "(?P<n>", "[",
           "]", "[^", "-", "*", "+", "?", "{", "}", "{2}", "{1,3}", "{,2}", "{2,}", "\\", "\\d", "\\D", "\\w", "\\W", "\\s", "\\S",
           "\\b", "\\B", "\\A", "\\z", "\\pL", "\\PL", "\\p{Greek}", "\\p{^Greek}", "[:alpha:]", "[[:^space:]]", "\\x41",
           "\\x{416}", "\\101", "\\Q", "\\E", "\\.", "\\<", "&&", "--", "~~", "#", "\\n", "x", "refund", "Straße", "(?-i)", "()",
           "[\\w-]", "[a-z]", "[z-a]"]


def test_grep_random_patterns_match_go(both) -> None:  # type: ignore[no-untyped-def]
    """随机拼的正则（固定种子）两版的结果逐字节相同：收不收、匹配到哪些行。"""
    import random

    rng = random.Random(28)
    patterns = ["".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 6))) for _ in range(400)]
    results = {}
    for which in ("go", "rust"):
        a = both(which)
        for rel, body in GREP_CORPUS.items():
            p = a.workdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode("latin-1") if rel == "invalid.bin.txt" else body.encode())
        results[which] = [a.request("POST", "/v1/grep", json.dumps({"pattern": p, "max_matches": 1000}).encode()) for p in patterns]
    diffs = [(p, g.status, g.body, r.status, r.body) for p, g, r in zip(patterns, results["go"], results["rust"]) if (g.status, g.body) != (r.status, r.body)]
    assert not diffs, diffs[:10]


def _battery(agent: Any) -> list[tuple[str, int, bytes]]:
    fixture_tree.make(agent.workdir)
    reqs: list[tuple[str, str, bytes | None]] = [
        ("GET", "/v1/manifest", None), ("GET", "/v1/list", None), ("GET", "/v1/list/src", None), ("GET", "/v1/list/src/app", None),
        ("GET", "/v1/list/nope", None), ("GET", "/v1/list/README.md", None), ("GET", "/v1/list/..%2Fx", None),
        ("POST", "/v1/grep", b'{"pattern": "a"}'), ("POST", "/v1/grep", b'{"pattern": "\\\\w+", "path": "src", "max_matches": 3}'),
        ("POST", "/v1/grep", b'{"pattern": "("}'), ("POST", "/v1/grep", b"{}"), ("POST", "/v1/grep", b'{"pattern": "x", "path": "/abs"}'),
        ("GET", "/v1/files/README.md", None), ("GET", "/v1/files/nope", None), ("GET", "/v1/files/src", None), ("PUT", "/v1/files/a%5Cb", b"x"),
        ("PUT", "/v1/files/new/dir/f.txt", b"hi"), ("GET", "/v1/files/new/dir/f.txt", None),
        ("POST", "/v1/exec", b"not json"), ("POST", "/v1/exec", b'{"argv": []}'), ("POST", "/v1/exec", b'{"argv": ["x"], "timeout_ms": 0}'),
        ("POST", "/v1/exec", b'{"argv": ["definitely-not-here"], "timeout_ms": 10}'),
        ("POST", "/v1/exec", b'{"argv": ["x"], "timeout_ms": 10, "env": {"AGENT_X": "1"}}'),
        ("POST", "/v1/exec/cancel", b"{}"), ("POST", "/v1/exec/cancel", b'{"exec_id": "z"}'),
        ("GET", "/v1/archive", None), ("GET", "/v1/archive?max_bytes=10", None), ("PUT", "/v1/archive?max_bytes=10", b""),
        ("GET", "/healthz", None), ("HEAD", "/healthz", None), ("GET", "/nope", None), ("GET", "/v1/files", None), ("PUT", "/v1/files", b"x"),
        ("DELETE", "/v1/files/x", None), ("GET", "/v1/exec", None), ("POST", "/v1/files/a/./b", b"x"), ("GET", "/v1/files/a//b?q=1", None),
        ("GET", "/v1/list/x/../y", None), ("GET", "/v1/files/%2E%2E/x", None), ("GET", "/v1/files/a%2Fb", None), ("GET", "/v1/archive/", None),
        ("POST", "/v1/exec/cancel/", b"{}"), ("GET", "/v1/manifest?x=1", None), ("PUT", "/healthz", b""),
    ]
    out = []
    for method, path, body in reqs:
        res = agent.request(method, path, body)
        keep = {k: v for k, v in res.headers.items() if k in ("content-type", "location", "allow", "x-content-type-options", "content-length")}
        out.append((f"{method} {path}", res.status, res.body, keep))
    return out


@linux_only
def test_responses_match_go_byte_for_byte(both) -> None:  # type: ignore[no-untyped-def]
    go, rust = _battery(both("go")), _battery(both("rust"))
    diffs = [(g[0], g[1:], r[1:]) for g, r in zip(go, rust) if g != r]
    assert not diffs, "\n".join(f"{name}: go={g!r}\n    rust={r!r}" for name, g, r in diffs)


def test_exec_responses_have_the_same_shape(both) -> None:  # type: ignore[no-untyped-def]
    shapes = []
    for which in ("go", "rust"):
        a = both(which)
        out = a.run("print", "x").json()
        shapes.append(list(out.keys()))
        res = a.run("sleep", "5", timeout_ms=200)
        shapes.append(re.sub(rb'"cpu_ms":\d+', b'"cpu_ms":N', res.body))
    assert shapes[0] == shapes[2] and shapes[1] == shapes[3], shapes

