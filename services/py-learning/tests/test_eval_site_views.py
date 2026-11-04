"""评测网站 /datasets 按题型画题（SITE-DATA，docs/plan/eval-site.md 第 7 节）。

site.json 的数据集写 layout（题型），site_catalog 把每道题整理成页面要画的几块（view.blocks）与一行摘要（view.summary）。
用户 2026-11-04 发现的问题：成对比较的题只显示了题目与 ``A>B``，两个候选回答藏在 extra 里，看着像判断题配错了答案。守住的：

- 成对比较并排显示两个回答，标出更好的那个；多选一的回答标出好回答与差回答；单选题标出正确选项；
  工具调用显示对话、可用工具与期望的调用；多轮智能体显示用户场景与期望的动作；补丁、终端任务、简答、交付与检索各有自己的块。
- 页面上不少字段：一行里的每个值（包括 extra 里的、写成 JSON 或 Python 字面量字符串的）都出现在 view 里。
- 有 view 的题不再带原样的 detail（内容都在 view 里，不重复发一遍）；没写 layout、layout 不认识的数据集照旧。
- 链接只给 https 的。留出集的题照旧只计数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentruntime_learning.evals.site_catalog import build_catalog
from agentruntime_learning.evals.site_views import LAYOUTS

REPO_EVALSETS = Path(__file__).resolve().parents[1] / "evalsets"


def write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jsonl":
        path.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in doc), "utf-8")
    else:
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), "utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "evalsets"
    write(r / "site.json", {"frameworks": [], "datasets": []})
    return r


def add(root: Path, name: str, layout: str | None, rows: list[dict[str, Any]], *, source_type: str = "original",
        question: str = "question", answer: str = "answer", **spec: Any) -> list[dict[str, Any]]:
    write(root / "public" / f"{name}.jsonl", rows)
    site = json.loads((root / "site.json").read_text("utf-8"))
    entry = {"name": name, "path": "public", "items": "jsonl", "file": f"{name}.jsonl", "question": question, "answer": answer,
             "source_type": source_type, "framework": "official", "eval_category": "judge", "work_item": "S-08",
             "source": {"dataset": name, "url": "https://example.org/data", "revision": "abc1234", "license": "MIT"}, **spec}
    if layout is not None:
        entry["layout"] = layout
    site["datasets"].append(entry)
    write(root / "site.json", site)
    return build_catalog(root).dataset(name)["items"]


def blocks(item: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [b for b in item["view"]["blocks"] if b["type"] == kind]


def field_values(item: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for b in blocks(item, "fields"):
        for f in b["items"]:
            key = f["key"]["en"] if isinstance(f["key"], dict) else f["key"]
            out[key] = f
    return out


def test_pairwise_shows_both_responses_side_by_side_and_which_is_better(root: Path) -> None:
    [item] = add(root, "judgebench", "pairwise", [{
        "id": "gpt:1", "question": "Speed of 4G?\n(A) 10 gbps\n(B) 100 mbps", "answer": "B>A",
        "extra": {"split": "gpt", "source": "mmlu-pro-engineering", "response_A": "I think (A). Answer: AAAAA", "response_B": "It is (B). Answer: BBBBB"},
        "selection": {"n": 40, "seed": 0, "eval": "judgebench"},
    }])
    view = item["view"]
    assert view["layout"] == "pairwise"
    [question] = [b for b in blocks(item, "text") if b["text"].startswith("Speed of 4G?")]
    [cands] = blocks(item, "candidates")
    assert cands["columns"] is True, "两个回答并排"
    assert [(c["label"], c["text"]) for c in cands["items"]] == [("A", "I think (A). Answer: AAAAA"), ("B", "It is (B). Answer: BBBBB")]
    assert [c.get("mark") for c in cands["items"]] == [None, "better"]
    assert view["summary"]["answer"] == {"zh": "更好的是 B", "en": "B is better"}, "摘要不再是看不懂的 B>A"
    assert field_values(item)["Source subset"]["value"] == "mmlu-pro-engineering"
    assert "detail" not in item, "内容都在 view 里，不再重复发原样的 detail"
    assert item["question"].startswith("Speed of 4G?") and item["answer"] == "B>A", "搜索与原有的字段不变"


def test_best_of_n_marks_the_chosen_and_the_rejected(root: Path) -> None:
    [item] = add(root, "rewardbench2", "best_of_n", [{
        "id": "Focus:1", "question": "Film ratings in India?", "answer": ["CBFC: U, U/A, A, S"],
        "extra": {"subset": "Focus", "rejected": ["MPAA ratings", "There are none", "Ask a lawyer"]},
    }])
    [cands] = blocks(item, "candidates")
    assert [c["text"] for c in cands["items"]] == ["CBFC: U, U/A, A, S", "MPAA ratings", "There are none", "Ask a lawyer"]
    assert [c["mark"] for c in cands["items"]] == ["chosen", "rejected", "rejected", "rejected"]
    assert cands["items"][0]["label"] == {"zh": "好回答", "en": "Chosen"}
    assert item["view"]["summary"]["answer"] == {"zh": "1 个好回答、3 个差回答", "en": "1 chosen, 3 rejected"}
    assert field_values(item)["Subset"]["value"] == "Focus"


def test_multiple_choice_marks_the_correct_option_and_says_the_context_is_not_shown(root: Path) -> None:
    [item] = add(root, "longbench", "multiple_choice", [{
        "id": "66fa", "question": "What problem does this code base solve?", "answer": "B",
        "extra": {"_id": "66fa", "context_bytes": 299000, "difficulty": "hard", "domain": "Code", "length": "short", "sub_domain": "Code repo QA",
                  "choice_A": "Delay", "choice_B": "Task heterogeneity", "choice_C": "Distributed", "choice_D": "Autoencoder"},
    }])
    [cands] = blocks(item, "candidates")
    assert [(c["label"], c["text"], c.get("mark")) for c in cands["items"]] == [
        ("A", "Delay", None), ("B", "Task heterogeneity", "correct"), ("C", "Distributed", None), ("D", "Autoencoder", None)]
    assert item["view"]["summary"]["answer"] == "B. Task heterogeneity"
    values = field_values(item)
    assert values["Long context (not shown)"]["value"] == "299000"
    assert values["Difficulty"]["value"] == "hard" and values["Sub-domain"]["value"] == "Code repo QA"


def test_tool_call_shows_the_conversation_the_tools_and_the_expected_calls(root: Path) -> None:
    question = json.dumps([[{"role": "user", "content": "Find the lyrics to 'Bohemian Rhapsody' by Queen."}]])
    tools = [{"name": "get_song_lyrics", "description": "Lyrics of a song", "parameters": {"type": "dict", "properties": {"song_title": {"type": "string"}}}},
             {"name": "get_stock_price", "description": "Stock price", "parameters": {"type": "dict", "properties": {}}}]
    [item] = add(root, "bfcl", "tool_call", [{
        "id": "multiple_1", "question": question, "answer": [{"get_song_lyrics": {"song_title": ["Bohemian Rhapsody"], "lang": ["English", ""]}}],
        "extra": {"category": "multiple", "function": tools},
    }])
    [messages] = blocks(item, "messages")
    assert messages["items"] == [{"role": "user", "content": "Find the lyrics to 'Bohemian Rhapsody' by Queen."}]
    [offered] = blocks(item, "tools")
    assert [t["name"] for t in offered["items"]] == ["get_song_lyrics", "get_stock_price"]
    assert offered["items"][0]["parameters"] == tools[0]["parameters"]
    [calls] = blocks(item, "calls")
    assert calls["items"] == [{"name": "get_song_lyrics", "arguments": {"song_title": ["Bohemian Rhapsody"], "lang": ["English", ""]}}]
    assert "empty" in calls["label"]["en"], "说明空字符串表示可以不传"
    assert item["view"]["summary"] == {"question": "Find the lyrics to 'Bohemian Rhapsody' by Queen.", "answer": "get_song_lyrics"}


def test_agent_scenario_shows_the_user_scenario_and_the_expected_actions(root: Path) -> None:
    scenario = {"instructions": {"domain": "airline", "reason_for_call": "Cancel IFOYYZ and upgrade M20IZO.",
                                 "known_info": "You are Aarav Ahmed.", "unknown_info": "Your booking date.", "task_instructions": "Pay with card 7334."}}
    answer = {"actions": [{"action_id": "37_1", "requestor": "assistant", "name": "get_reservation_details", "arguments": {"reservation_id": "M20IZO"}},
                          {"action_id": "37_2", "requestor": "user", "name": "toggle_roaming", "arguments": {}}],
              "communicate_info": ["The refund is $120"], "nl_assertions": ["Agent refuses to cancel IFOYYZ"],
              "reward_basis": ["DB", "COMMUNICATE"], "env_assertions": [{"env_type": "user", "func_name": "assert_roaming", "arguments": {}, "assert_value": True}]}
    [item] = add(root, "tau2", "agent_scenario", [{
        "id": "airline:37", "question": json.dumps(scenario), "answer": answer,
        "extra": {"domain": "airline", "task_id": "37", "description": {"purpose": "Two cancellations, none allowed."}},
    }])
    texts = {b["label"]["en"]: b["text"] for b in blocks(item, "text")}
    assert texts["Reason for the call"] == "Cancel IFOYYZ and upgrade M20IZO."
    assert texts["What the user knows"] == "You are Aarav Ahmed." and texts["What the user does not know"] == "Your booking date."
    [calls] = blocks(item, "calls")
    assert [c["name"] for c in calls["items"]] == ["get_reservation_details", "toggle_roaming"]
    assert [c["by"] for c in calls["items"]] == ["assistant", "user"], "发起方照原样给出，页面只给用户一侧的标出来"
    assert calls["items"][0]["id"] == "37_1"
    lists = {b["label"]["en"]: b["items"] for b in blocks(item, "list")}
    assert lists["Must tell the user"] == ["The refund is $120"]
    assert lists["Natural-language checks"] == ["Agent refuses to cancel IFOYYZ"]
    assert field_values(item)["Purpose"]["value"] == "Two cancellations, none allowed."
    assert item["view"]["summary"]["question"] == "Cancel IFOYYZ and upgrade M20IZO."
    assert item["view"]["summary"]["answer"]["en"].startswith("2 expected actions")


def test_code_patch_shows_the_issue_the_reference_patch_and_the_tests(root: Path) -> None:
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@\n-a\n+b\ndiff --git a/y.py b/y.py\n"
    [item] = add(root, "swebench-verified", "code_patch", [{
        "id": "astropy__astropy-1", "repo": "astropy/astropy", "base_commit": "298ccb4", "difficulty": "15 min - 1 hour",
        "created_at": "2022-03-31T23:28:27Z", "version": "4.3", "question": "TimeSeries: misleading exception\nmore", "hints_text": "",
        "answer": {"patch": patch, "test_patch": "diff --git a/t.py b/t.py\n", "FAIL_TO_PASS": ["t.py::test_a"], "PASS_TO_PASS": ["t.py::test_b"]},
    }])
    values = field_values(item)
    assert values["Repository"]["value"] == "astropy/astropy" and values["Repository"]["href"] == "https://github.com/astropy/astropy"
    code = {b["label"]["en"]: b for b in blocks(item, "text") if b.get("mono")}
    assert code["Reference patch"]["text"] == patch and code["Test patch"]["text"].startswith("diff --git a/t.py")
    lists = {b["label"]["en"]: b["items"] for b in blocks(item, "list")}
    assert lists["Tests that must start passing"] == ["t.py::test_a"] and lists["Tests that must keep passing"] == ["t.py::test_b"]
    assert not [b for b in blocks(item, "text") if b["label"]["en"] == "Hints"], "空的提示不画"
    assert item["view"]["summary"] == {"question": "TimeSeries: misleading exception",
                                       "answer": {"zh": "参考补丁改 2 个文件；1 条测试要转为通过", "en": "Reference patch touches 2 files; 1 test must start passing"}}


def test_terminal_task_shows_the_instructions_and_each_test_file(root: Path) -> None:
    [item] = add(root, "terminal-bench-2", "terminal_task", [{
        "id": "sampler", "question": "Implement an adaptive-rejection sampler.", "portable": True, "docker_image": "alexgshaw/sampler:20251031",
        "difficulty": "medium", "category": "scientific-computing", "expert_time_estimate_min": "180.0", "description": "Evaluates R skills.",
        "answer": {"graded_by": "hidden tests (tests/test.sh) run in the task image", "tests": {"test.sh": "#!/bin/bash\npytest", "test_outputs.py": "def test(): pass"}},
        "extra": {"redacted": "4 个形似访问令牌的字符串换成了占位符"},
    }])
    code = {b["label"]: b["text"] for b in blocks(item, "text") if b.get("mono")}
    assert code == {"tests/test.sh": "#!/bin/bash\npytest", "tests/test_outputs.py": "def test(): pass"}
    values = field_values(item)
    assert values["Graded by"]["value"] == "hidden tests (tests/test.sh) run in the task image"
    assert values["Expert time (minutes)"]["value"] == "180.0" and values["Image"]["value"] == "alexgshaw/sampler:20251031"
    assert values["Portable work directory"]["value"] == "true"
    assert item["view"]["summary"]["answer"] == "hidden tests (tests/test.sh) run in the task image"


def test_short_answer_parses_metadata_and_offers_only_https_links(root: Path) -> None:
    metadata = "{'topic': 'History', 'answer_type': 'Date', 'urls': ['https://www.heart.org/history', 'javascript:alert(1)']}"
    [item] = add(root, "simpleqa", "short_answer", [{"id": "simpleqa-1", "question": "In what year?", "answer": "1991", "extra": {"metadata": metadata}}])
    texts = {b["label"]["en"]: b["text"] for b in blocks(item, "text")}
    assert texts["Question"] == "In what year?" and texts["Reference answer"] == "1991"
    values = field_values(item)
    assert values["topic"]["value"] == "History" and values["answer_type"]["value"] == "Date"
    [urls] = [b for b in blocks(item, "list") if b["label"] == "urls"]
    assert urls["items"] == ["https://www.heart.org/history", "javascript:alert(1)"], "值照原样给出"
    assert not any("href" in f and not f["href"].startswith("https://") for b in blocks(item, "fields") for f in b["items"])
    [frames] = add(root, "frames", "short_answer", [{"id": "frames-1", "question": "How old?", "answer": "46",
                                                    "extra": {"reasoning_types": "Numerical reasoning | Temporal reasoning",
                                                              "wiki_links": "['https://en.wikipedia.org/wiki/Miss_Venezuela_1970']"}}])
    assert field_values(frames)["reasoning_types"]["value"] == "Numerical reasoning | Temporal reasoning"
    [links] = [b for b in blocks(frames, "list") if b["label"] == "wiki_links"]
    assert links["items"] == ["https://en.wikipedia.org/wiki/Miss_Venezuela_1970"]


def test_deliverable_and_retrieval_layouts_for_authored_cases(root: Path) -> None:
    [deliverable] = add(root, "deliverables", "deliverable", [{"id": "structure", "message": "What is the layout?", "expected_output": "README, src/",
                                                               "metrics": ["correctness", "honesty"], "confirmed": False}],
                        source_type="authored", question="message", answer="expected_output")
    texts = {b["label"]["en"]: b["text"] for b in blocks(deliverable, "text")}
    assert texts == {"Request": "What is the layout?", "Expected answer": "README, src/"}
    assert {b["label"]["en"]: b["items"] for b in blocks(deliverable, "list")} == {"Metrics": ["correctness", "honesty"]}

    write(root / "public" / "corpus.jsonl", [{"id": "m01", "kind": "recipe", "text": "Use uv run pytest."}, {"id": "m02", "kind": "fact", "text": "Port 8443."}])
    [retrieval] = add(root, "memory-rag", "retrieval", [{"id": "q-tests", "question": "How do I run tests?", "reference": "uv run pytest",
                                                         "reference_ids": ["m01"], "confirmed": False}],
                      source_type="authored", answer="reference", context_file="corpus.jsonl", context_ids="reference_ids")
    [memories] = blocks(retrieval, "candidates")
    assert memories["items"] == [{"label": "m01 · recipe", "text": "Use uv run pytest."}], "只列这题引用的记忆"


def test_without_a_known_layout_items_keep_the_raw_detail(root: Path) -> None:
    [plain] = add(root, "plain", None, [{"id": "p1", "question": "q", "answer": "a", "extra": {"k": "v"}}])
    assert "view" not in plain and plain["detail"]["extra"] == {"k": "v"}
    [odd] = add(root, "odd", "no_such_layout", [{"id": "o1", "question": "q", "answer": "a"}])
    assert "view" not in odd and "detail" in odd


def test_holdout_rows_with_a_layout_are_still_only_counted(root: Path) -> None:
    items = add(root, "judgebench", "pairwise", [
        {"id": "shown", "question": "q", "answer": "A>B", "extra": {"response_A": "a", "response_B": "b"}},
        {"id": "hidden-77", "split": "holdout", "question": "HOLDOUT question 5d1", "answer": "B>A", "extra": {"response_A": "HOLDOUT A 5d1", "response_B": "x"}},
    ])
    text = json.dumps([build_catalog(root).index(), items], ensure_ascii=False)
    assert "5d1" not in text and "hidden-77" not in text


# --- 对着仓库里真正的 evalsets ---


def _leaves(value: Any, parse: bool = True) -> list[Any]:
    """一个值里所有的叶子。直接是字段值的字符串若写成 JSON 或 Python 字面量（bfcl、tau2 的题目，simpleqa 的 metadata）先展开；
    更深处的字符串（例如测试文件的内容）原样算，页面也是原样画的。"""
    from agentruntime_learning.evals.site_views import parse_literal

    if isinstance(value, str):
        parsed = parse_literal(value) if parse else None
        return _leaves(parsed, False) if parsed is not None else [value]
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v, False)]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _leaves(v, False)]
    return [value]


def _row_leaves(row: dict[str, Any]) -> list[Any]:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else None
    out = [leaf for k, v in row.items() if not (k == "extra" and extra is not None) for leaf in _leaves(v)]
    return out + [leaf for v in (extra or {}).values() for leaf in _leaves(v)]


def _repo_jsonl_datasets() -> list[dict[str, Any]]:
    site = json.loads((REPO_EVALSETS / "site.json").read_text("utf-8"))
    return [d for d in site["datasets"] if d.get("items") == "jsonl"]


def test_every_jsonl_dataset_in_the_repository_has_a_known_layout() -> None:
    for spec in _repo_jsonl_datasets():
        assert spec.get("layout") in LAYOUTS, f"{spec['name']} 要在 site.json 写 layout"


@pytest.mark.parametrize("name", [d["name"] for d in _repo_jsonl_datasets()])
def test_no_value_of_a_repository_row_is_lost_on_the_page(name: str) -> None:
    catalog = build_catalog(REPO_EVALSETS)
    spec = next(d for d in _repo_jsonl_datasets() if d["name"] == name)
    rows = {r["id"]: r for r in (json.loads(line) for line in (REPO_EVALSETS / spec["path"] / spec["file"]).read_text("utf-8").splitlines() if line.strip())}
    items = catalog.dataset(name)["items"]
    assert items
    for item in items:
        assert "view" in item and item["view"]["blocks"], item["id"]
        haystack = json.dumps(item["view"], ensure_ascii=False)
        row = {k: v for k, v in rows[item["id"]].items() if k not in ("id", "split", "confirmed")}
        for leaf in _row_leaves(row):
            if leaf in ("", None):
                continue
            needle = json.dumps(leaf, ensure_ascii=False)
            needle = needle[1:-1] if isinstance(leaf, str) else needle
            assert needle in haystack, f"{name}/{item['id']} 的 {needle[:80]!r} 没画在页面上"
        for key in ("question", "answer"):
            assert item["view"]["summary"][key], f"{name}/{item['id']} 缺摘要的 {key}"
