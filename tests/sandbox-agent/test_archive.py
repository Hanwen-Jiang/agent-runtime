"""存档、清单、列目录与搜索（D-06a；D-28 黑盒，对应 Go 版 archive_test.go 与 D-28 验收 3 的恶意 tar 套件）。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest
import rawtar
from conftest import Agent, linux_only
from rawtar import Item, dir, file, symlink

MARKER = ".agentruntime"


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode() if isinstance(body, str) else body)


def manifest(agent: Agent) -> list[dict]:
    res = agent.request("GET", "/v1/manifest")
    assert res.status == 200, res.body
    assert res.headers["content-type"] == "application/json"
    return res.json()["entries"]


def export(agent: Agent, limit: str = "1048576") -> tuple[int, bytes]:
    res = agent.request("GET", f"/v1/archive?max_bytes={limit}")
    return res.status, res.body


def import_tar(agent: Agent, data: bytes, limit: str = "1048576") -> tuple[int, str | None]:
    res = agent.request("PUT", f"/v1/archive?max_bytes={limit}", data, headers={"Content-Type": "application/x-tar"})
    return res.status, res.code


def comparable(entries: list[dict]) -> list[dict]:
    if os.name == "nt":
        return [{**e, "mode": 0} for e in entries]
    return entries


def test_archive_round_trips_into_a_fresh_environment_and_the_manifest_matches(make_agent) -> None:  # type: ignore[no-untyped-def]
    src = make_agent()
    write_tree(src.workdir, {"README.md": "# 退款\n", "src/app/main.py": "print('hi')\n", "empty.txt": "", ".git/HEAD": "ref: refs/heads/main\n"})
    (src.workdir / MARKER).write_bytes(b"")
    if os.name != "nt":
        (src.workdir / "src/app/main.py").chmod(0o755)
    code, data = export(src)
    assert code == 200, data
    dst = make_agent()
    (dst.workdir / MARKER).write_bytes(b"")
    assert import_tar(dst, data) == (204, None)
    want, got = comparable(manifest(src)), comparable(manifest(dst))
    assert want == got
    by_path = {e["path"]: e for e in got}
    assert MARKER not in by_path, "标记文件不进清单"
    readme = by_path["README.md"]
    assert readme == {"path": "README.md", "type": "file", "mode": readme["mode"], "size": len("# 退款\n".encode()),
                      "sha256": hashlib.sha256("# 退款\n".encode()).hexdigest()}
    assert by_path["empty.txt"] == {"path": "empty.txt", "type": "file", "mode": by_path["empty.txt"]["mode"],
                                    "sha256": hashlib.sha256(b"").hexdigest()}, "大小为 0 时省略 size"
    assert by_path["src"] == {"path": "src", "type": "dir", "mode": by_path["src"]["mode"]}
    if os.name != "nt":
        assert by_path["src/app/main.py"]["mode"] == 0o755
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert MARKER not in names and "src" in names


def test_export_writes_a_tar_that_python_reads(agent: Agent) -> None:
    write_tree(agent.workdir, {"a/b/c.txt": "c", "z.txt": "z" * 1000})
    code, data = export(agent)
    assert code == 200
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert set(members) == {"a", "a/b", "a/b/c.txt", "z.txt"}
        assert members["a"].isdir() and members["z.txt"].size == 1000
        assert tar.extractfile("z.txt").read() == b"z" * 1000  # type: ignore[union-attr]


def test_export_order_is_depth_first_by_name(agent: Agent) -> None:
    write_tree(agent.workdir, {"a/x": "1", "a-b": "2", "a.c": "3", "B": "4"})
    code, data = export(agent)
    names = [m.name for m in tarfile.open(fileobj=io.BytesIO(data)).getmembers()]
    assert names == ["B", "a", "a/x", "a-b", "a.c"]
    assert [e["path"] for e in manifest(agent)] == ["B", "a", "a-b", "a.c", "a/x"], "清单按路径的字符串顺序"


def test_empty_workdir(agent: Agent) -> None:
    assert agent.request("GET", "/v1/manifest").body == b'{"entries":[]}\n'
    code, data = export(agent)
    assert code == 200 and tarfile.open(fileobj=io.BytesIO(data)).getnames() == []
    assert import_tar(agent, b"") == (204, None), "空的请求体是空存档"


def test_import_only_into_an_empty_workdir_and_within_the_limit(make_agent) -> None:  # type: ignore[no-untyped-def]
    h = make_agent()
    write_tree(h.workdir, {"already.txt": "x"})
    tarball = rawtar.build(file("a.txt", "a"))
    assert import_tar(h, tarball, "1024") == (409, "not_empty")
    empty = make_agent()
    assert import_tar(empty, rawtar.build(file("big.bin", "x" * 2048)), "1024")[0] == 413
    write_tree(h.workdir, {"big.bin": "y" * 4096})
    assert export(h, "1024")[0] == 413
    assert export(h, "4097")[0] == 200, "文件总大小正好不超过上限"
    for bad in ("", "0", "-5", "abc", "1.5"):
        res = h.request("GET", f"/v1/archive?max_bytes={bad}")
        assert res.status == 400 and res.code == "invalid_request", bad
        res = h.request("PUT", f"/v1/archive?max_bytes={bad}", b"")
        assert res.status == 400 and res.code == "invalid_request", bad
    assert h.request("GET", "/v1/archive").status == 400


def test_an_oversized_body_is_refused_with_413(agent: Agent) -> None:
    # 请求体的上限是 max_bytes + max_bytes/8 + 1 MiB；目录项不算进文件大小，只有请求体的上限拦得住。
    data = rawtar.build(*[dir(f"d{i}") for i in range(3000)])
    assert len(data) > 1024 + 128 + (1 << 20)
    assert import_tar(agent, data, "1024") == (413, "too_large")


def test_a_truncated_or_corrupt_archive_is_invalid(make_agent) -> None:  # type: ignore[no-untyped-def]
    good = rawtar.build(file("a.txt", b"x" * 1000))
    a = make_agent()
    assert import_tar(a, good[:700]) == (400, "invalid_archive")
    bad = bytearray(good)
    bad[0] ^= 0xFF  # 校验和不对
    b = make_agent()
    assert import_tar(b, bytes(bad)) == (400, "invalid_archive")
    c = make_agent()
    assert import_tar(c, rawtar.build(file("a.txt", "x"), end=False)) == (204, None), "没有结尾的两个零块也行"


HOSTILE: dict[str, list[Item]] = {
    "absolute": [file("/tmp/sandbox-agent-escape/pwned.txt")],
    "dotdot": [file("../escape.txt")],
    "nested-dotdot": [file("a/../../escape.txt")],
    "dotdot-only": [file("..")],
    "backslash": [file("a\\b.txt")],
    "backslash-dotdot": [file("..\\escape.txt")],
    "drive": [file("C:/escape.txt")],
    "hardlink": [file("a.txt"), Item("b.txt", b"1", link="a.txt")],
    "char-device": [Item("dev", b"3")],
    "block-device": [Item("dev", b"4")],
    "fifo": [Item("fifo", b"6")],
    "contiguous": [Item("c", b"7")],
    "escaping-link": [symlink("out", "../../etc")],
    "escaping-link-in-subdir": [dir("d"), symlink("d/ln", "../../escape.txt")],
    "absolute-link": [symlink("out", "/etc")],
    "backslash-link": [symlink("out", "..\\escape.txt")],
    "dotdot-link": [symlink("out", "sub/../..")],
    "empty-link": [symlink("out", "")],
    "duplicate-file": [file("a.txt"), file("a.txt", "y")],
    "file-then-dir": [file("a", "x"), file("a/b.txt", "y")],
    "link-then-file": [symlink("a.txt", "b.txt"), file("a.txt")],
    "gnu-longname-escape": [Item("innocent", b"0", b"x", gnu_longname="../escape.txt")],
    "pax-path-escape": [Item("innocent", b"0", b"x", pax={"path": "../escape.txt"})],
    "pax-linkpath-escape": [Item("innocent", b"2", link="ok", pax={"linkpath": "../../escape.txt"})],
    "prefix-escape": [Item("escape.txt", b"0", b"x", prefix="..")],
}
LINUX_ONLY = {
    # 先放一个指向工作目录里的链接，再经它写：上级目录不能是链接。
    "through-link": [dir("sub"), symlink("ln", "sub"), file("ln/x.txt")],
    "dir-over-link": [dir("sub"), symlink("d", "sub"), dir("d")],
    "through-link-deeper": [dir("sub"), dir("sub/inner"), symlink("ln", "sub"), file("ln/inner/x.txt")],
    "link-to-dot-then-escape": [symlink("here", "."), file("here/../../escape.txt")],
}


@pytest.mark.parametrize("name", sorted(HOSTILE) + sorted(LINUX_ONLY))
def test_hostile_archives_are_refused_and_write_nothing_outside(make_agent, tmp_path: Path, name: str) -> None:  # type: ignore[no-untyped-def]
    if name in LINUX_ONLY and sys.platform != "linux":
        pytest.skip("要符号链接")
    items = HOSTILE.get(name) or LINUX_ONLY[name]
    parent = tmp_path / "parent"
    work = parent / "work"
    a = make_agent(workdir=work)
    before = _snapshot(parent, skip=work)
    code, err = import_tar(a, rawtar.build(*items))
    assert code == 400, (name, code, err)
    assert err in {"invalid_path", "invalid_link", "unsupported_entry", "invalid_archive", "symlink_unsupported"}, err
    assert _snapshot(parent, skip=work) == before, "写到了工作目录外面"
    assert not Path("/tmp/sandbox-agent-escape").exists()
    assert not (tmp_path / "escape.txt").exists()


EXPECTED_CODES = {
    "absolute": "invalid_path", "dotdot": "invalid_path", "nested-dotdot": "invalid_path", "dotdot-only": "invalid_path",
    "backslash": "invalid_path", "drive": "invalid_path", "hardlink": "unsupported_entry", "char-device": "unsupported_entry",
    "fifo": "unsupported_entry", "escaping-link": "invalid_link", "absolute-link": "invalid_link", "duplicate-file": "invalid_path",
    "file-then-dir": "invalid_path", "pax-path-escape": "invalid_path", "gnu-longname-escape": "invalid_path",
}


@pytest.mark.parametrize("name", sorted(EXPECTED_CODES))
def test_hostile_archive_error_codes(make_agent, name: str) -> None:  # type: ignore[no-untyped-def]
    a = make_agent()
    assert import_tar(a, rawtar.build(*HOSTILE[name])) == (400, EXPECTED_CODES[name])


def _snapshot(root: Path, *, skip: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == skip:
            dirnames[:] = []
            continue
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            if p == skip:
                continue
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mode, st.st_size)
    return out


def test_benign_names_are_normalised(make_agent) -> None:  # type: ignore[no-untyped-def]
    a = make_agent()
    data = rawtar.build(dir("./"), dir("./d/"), file("./d/x.txt", "x"), file("d//y.txt", "y"), file("d/sub/../z.txt", "z"), file(MARKER, "m"))
    assert import_tar(a, data) == (204, None)
    assert [e["path"] for e in manifest(a)] == ["d", "d/x.txt", "d/y.txt", "d/z.txt"]
    assert not (a.workdir / MARKER).exists(), "存档里的标记文件跳过"


def test_long_and_unicode_names_round_trip(make_agent) -> None:  # type: ignore[no-untyped-def]
    long_dir = "d" * 120
    a = make_agent()
    write_tree(a.workdir, {f"{long_dir}/{'f' * 150}.txt": "long", "退款/说明.md": "中文", "sp ace/x": "s"})
    code, data = export(a)
    assert code == 200
    b = make_agent()
    assert import_tar(b, data) == (204, None)
    assert manifest(a) == manifest(b)


def test_python_tarfile_archives_import(make_agent) -> None:  # type: ignore[no-untyped-def]
    # Worker 从 ObjectStore 的清单与块组装的是 tarfile 的 PAX 存档（agentruntime_worker.workspace.archive）；GNU 与 ustar 也收。
    for fmt in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT, tarfile.USTAR_FORMAT):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=fmt) as tar:
            for name, body in (("退款/a.txt" if fmt != tarfile.USTAR_FORMAT else "r/a.txt", b"a"), ("x" * 90 + "/" + "y" * 90, b"long")):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(body), 0o640, 1700000000.5
                tar.addfile(info, io.BytesIO(body))
            info = tarfile.TarInfo("emptydir")
            info.type, info.mode = tarfile.DIRTYPE, 0o700
            tar.addfile(info)
            if sys.platform == "linux":
                info = tarfile.TarInfo("ln")
                info.type, info.linkname = tarfile.SYMTYPE, "emptydir"
                tar.addfile(info)
        a = make_agent()
        assert import_tar(a, buf.getvalue()) == (204, None), fmt
        entries = {e["path"]: e for e in manifest(a)}
        assert entries[("x" * 90) + "/" + ("y" * 90)]["size"] == 4
        if os.name != "nt":
            assert entries["emptydir"]["mode"] == 0o700 and entries[("x" * 90) + "/" + ("y" * 90)]["mode"] == 0o640
        if sys.platform == "linux":
            assert entries["ln"] == {"path": "ln", "type": "symlink", "mode": 0, "link": "emptydir"}


@linux_only
def test_modes_are_restored_and_type_bits_ignored(agent: Agent) -> None:
    data = rawtar.build(dir("ro", 0o555), Item("ro/x.sh", b"0", b"#!", mode=0o100755), Item("setuid", b"0", b"s", mode=0o4755), dir("private", 0o700))
    assert import_tar(agent, data) == (204, None)
    m = {e["path"]: e["mode"] for e in manifest(agent)}
    assert m["ro/x.sh"] == 0o755 and m["setuid"] == 0o755, "只留权限位"
    assert m["ro"] == 0o755 and m["private"] == 0o700, "目录至少 0700"
    assert not stat.S_ISUID & (agent.workdir / "setuid").stat().st_mode


@linux_only
def test_symlinks_inside_the_workdir_round_trip(make_agent) -> None:  # type: ignore[no-untyped-def]
    h = make_agent()
    data = rawtar.build(dir("real"), file("real/f.txt", "f"), symlink("alias", "real"), symlink("real/up", "../alias"))
    assert import_tar(h, data) == (204, None)
    entries = {e["path"]: e for e in manifest(h)}
    assert entries["alias"] == {"path": "alias", "type": "symlink", "mode": 0, "link": "real"}
    assert entries["real/up"]["link"] == "../alias"
    code, out = export(h)
    g = make_agent()
    assert import_tar(g, out) == (204, None)
    assert manifest(g) == manifest(h)


@linux_only
def test_links_pointing_out_and_special_files_are_left_out_of_exports(agent: Agent) -> None:
    write_tree(agent.workdir, {"keep.txt": "k"})
    os.symlink("/etc", agent.workdir / "out")
    os.symlink("../../x", agent.workdir / "out2")
    os.mkfifo(agent.workdir / "fifo")
    code, data = export(agent)
    assert code == 200
    assert tarfile.open(fileobj=io.BytesIO(data)).getnames() == ["keep.txt"]
    paths = [e["path"] for e in manifest(agent)]
    assert paths == ["keep.txt", "out", "out2"], "清单里有指出去的链接（只记录），没有 fifo"


def test_list_and_grep_run_inside_the_workdir(agent: Agent) -> None:
    write_tree(agent.workdir, {"a.py": "def refund():\n    return 1\n", "pkg/b.py": "x = 1\n# refund policy\n", "pkg/c.txt": "nothing\n",
                               ".git/config": "refund\n"})
    (agent.workdir / "bin.dat").write_bytes(b"refund\x00\x01")
    (agent.workdir / MARKER).write_bytes(b"")
    listed = agent.request("GET", "/v1/list").json()["entries"]
    assert [f'{e["name"]}:{e["type"]}' for e in listed] == [".git:dir", "a.py:file", "bin.dat:file", "pkg:dir"]
    assert agent.request("GET", "/v1/list/").json()["entries"] == listed
    sub = agent.request("GET", "/v1/list/pkg").json()["entries"]
    assert sub == [{"name": "b.py", "type": "file", "size": len("x = 1\n# refund policy\n")}, {"name": "c.txt", "type": "file", "size": 8}]
    res = agent.request("GET", "/v1/list/missing")
    assert res.status == 404 and res.code == "dir_not_found"
    assert agent.request("GET", "/v1/list/a.py").status == 404
    res = agent.request("GET", "/v1/list/..%2Fescape")
    assert res.status == 400 and res.code == "invalid_path"

    def grep(body: dict | bytes) -> tuple[int, dict]:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        res = agent.request("POST", "/v1/grep", raw)
        return res.status, res.json()

    code, out = grep({"pattern": "refund"})
    assert code == 200 and out["truncated"] is False and len(out["matches"]) == 2
    assert out["matches"][0] == {"path": "a.py", "line": 1, "text": "def refund():"}
    assert len(grep({"pattern": "refund", "path": "pkg"})[1]["matches"]) == 1
    assert len(grep({"pattern": "refund", "path": "pkg/b.py"})[1]["matches"]) == 1
    assert len(grep({"pattern": "refund", "path": "."})[1]["matches"]) == 2
    assert grep({"pattern": "refund", "max_matches": 1})[1]["truncated"] is True
    code, out = grep({"pattern": "("})
    assert code == 400 and out["code"] == "invalid_pattern"
    for bad in (b"{}", b'{"pattern": ""}', b"nope", b'{"pattern": 5}'):
        code, out = grep(bad)
        assert code == 400 and out["code"] == "invalid_request", bad
    code, out = grep({"pattern": "x", "path": "../x"})
    assert code == 400 and out["code"] == "invalid_path"
    assert grep({"pattern": "x", "path": "missing"}) == (200, {"matches": [], "truncated": False})


def test_list_does_not_follow_links_and_caps_entries(agent: Agent) -> None:
    for i in range(1005):
        (agent.workdir / f"f{i:04d}").write_bytes(b"")
    listed = agent.request("GET", "/v1/list").json()["entries"]
    assert len(listed) == 1000 and listed[0]["name"] == "f0000" and listed[-1]["name"] == "f0999"
    if sys.platform == "linux":
        (agent.workdir / "real").mkdir()
        os.symlink("real", agent.workdir / "ln")
        assert agent.request("GET", "/v1/list/ln").status == 404


def test_grep_skips_git_binary_and_big_files_and_trims_lines(agent: Agent) -> None:
    write_tree(agent.workdir, {
        "deep/.git/x": "needle", "deep/ok.txt": "needle\r\nno\r\nneedle at 3", "big.txt": "needle\n" + "x" * (1 << 20),
        "long.txt": "needle" + "é" * 600, "nonl.txt": "needle", "late-nul.txt": "needle\n" + "y" * 2000 + "\0",
    })
    (agent.workdir / "bin2.txt").write_bytes(b"x" * 1000 + b"\0needle")
    out = agent.request("POST", "/v1/grep", b'{"pattern": "needle", "max_matches": 5000}').json()
    got = [(m["path"], m["line"]) for m in out["matches"]]
    assert got == [("deep/ok.txt", 1), ("deep/ok.txt", 3), ("late-nul.txt", 1), ("long.txt", 1), ("nonl.txt", 1)], got
    assert out["matches"][0]["text"] == "needle", "行尾的 \\r 去掉"
    by = {m["path"]: m for m in out["matches"]}
    assert len(by["long.txt"]["text"]) == 500, "按字符截到 500"


def test_grep_max_matches_is_clamped(agent: Agent) -> None:
    write_tree(agent.workdir, {"many.txt": "m\n" * 1500})
    out = agent.request("POST", "/v1/grep", b'{"pattern": "m", "max_matches": 100000}').json()
    assert len(out["matches"]) == 1000 and out["truncated"] is True
    out = agent.request("POST", "/v1/grep", b'{"pattern": "m"}').json()
    assert len(out["matches"]) == 100 and out["truncated"] is True, "默认 100"
    out = agent.request("POST", "/v1/grep", b'{"pattern": "m", "max_matches": -3}').json()
    assert len(out["matches"]) == 100


def test_archive_endpoints_need_the_token(agent: Agent) -> None:
    for path in ["/v1/manifest", "/v1/archive?max_bytes=1", "/v1/list"]:
        assert agent.request("GET", path, token="wrong-token-0123456789abcdef0123456789").status == 401
