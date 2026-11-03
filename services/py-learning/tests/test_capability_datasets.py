"""公开数据集评测的共用部分（capability.evals._datasets）：缓存目录、下载核对哈希、确定的选题、大 JSON 数组的逐个读取。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentruntime_learning.capability.evals import _datasets as ds


def test_cache_dir_comes_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ds.DATA_ENV, str(tmp_path))
    assert ds.data_dir() == tmp_path
    monkeypatch.delenv(ds.DATA_ENV)
    assert ds.data_dir() == Path.home() / ".cache" / "agentruntime-evals"


def test_download_checks_the_pinned_hash(tmp_path: Path) -> None:
    src = tmp_path / "src.csv"
    src.write_bytes(b"problem,answer\nq,a\n")
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    dest = tmp_path / "cache" / "x.csv"
    with pytest.raises(ValueError, match="sha256"):
        ds.download(src.as_uri(), dest, sha256="0" * 64)
    assert not dest.exists() and not list(dest.parent.glob("*.part")), "核对不过不留文件"
    assert ds.download(src.as_uri(), dest, sha256=good).read_bytes() == src.read_bytes()
    src.write_bytes(b"changed")
    assert ds.download(src.as_uri(), dest, sha256=good).read_bytes() != b"changed", "缓存对得上就不再下载"


def test_select_is_stable_nested_and_round_robin() -> None:
    ids = [f"q{i}" for i in range(30)]
    assert ds.select(ids, 10, 3) == ds.select(list(reversed(ids)), 10, 3)
    assert ds.select(ids, 20, 3)[:10] == ds.select(ids, 10, 3)
    assert ds.select(ids, 10, 3) != ds.select(ids, 10, 4)
    groups = {i: ("a" if int(i[1:]) < 25 else "b") for i in ids}
    picked = ds.select(ids, 8, 0, group=groups.__getitem__)
    assert sum(groups[i] == "b" for i in picked) == 4 and ds.select(ids, 12, 0, group=groups.__getitem__)[:8] == picked
    assert len(ds.select(ids, 100, 0, group=groups.__getitem__)) == 30
    with pytest.raises(ValueError):
        ds.select(["a", "a"], 1, 0)


def test_iter_json_array_reads_elements_one_by_one(tmp_path: Path) -> None:
    rows = [{"_id": str(i), "context": "长文本" * (i * 50), "n": i} for i in range(20)]
    path = tmp_path / "data.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
    assert list(ds.iter_json_array(path, chunk_size=64)) == rows
    (tmp_path / "empty.json").write_text("[ ]", "utf-8")
    assert list(ds.iter_json_array(tmp_path / "empty.json")) == []


def test_cached_jsonl_builds_once(tmp_path: Path) -> None:
    calls = []

    def build() -> list[dict]:
        calls.append(1)
        return [{"id": "a"}]

    assert ds.cached_jsonl(tmp_path / "c.jsonl", build) == ds.cached_jsonl(tmp_path / "c.jsonl", build) == [{"id": "a"}]
    assert len(calls) == 1


def test_cached_jsonl_keeps_unicode_line_separators_inside_strings(tmp_path: Path) -> None:
    """RewardBench 2 的回答里有 U+2028 等：缓存按 ensure_ascii=False 写，读回来不能把它们当换行切断一行 JSON。"""
    rows = [{"id": "a", "text": "one twothree fourfive"}, {"id": "b", "text": "plain"}]
    assert ds.cached_jsonl(tmp_path / "u.jsonl", lambda: rows) == rows
    assert ds.read_jsonl(tmp_path / "u.jsonl") == rows
