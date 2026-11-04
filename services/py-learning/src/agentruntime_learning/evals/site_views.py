"""评测网站 /datasets 按题型画题（SITE-DATA，docs/plan/eval-site.md 第 7 节）。只用标准库。

site.json 的 jsonl 数据集写 ``layout``（题型），build_view 把一行题整理成页面要画的几块（blocks）与收起时的一行摘要（summary），
页面照着画；没写 layout 或不认识的，页面照旧画原样的 detail。每种题型把这一行的值都放进某一块：认识的进专门的块，其余的按值的
形状进“其他字段”，所以页面上不少字段（测试逐个核对）。

块（标签是 {"zh", "en"}，或原样的字段名）：

- ``text``：一段文字；mono 为真时按代码排；
- ``candidates``：几个候选（回答、选项、记忆），每个有 label、text，可选 mark（better、chosen、rejected、correct）；columns 为真时并排；
- ``fields``：键值对，值是文字，可带 href（只给 https 的）；
- ``messages``：对话（role、content）；``tools``：可用的工具（name、description、parameters）；
- ``calls``：期望的调用（name、arguments，可选 id 与 by（发起方）；上游的其余键原样跟着）；
- ``list``：一列文字（页面只把整条是 https 地址的画成链接）；``json``：结构化的值，原样给出。
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from typing import Any

SKIP = frozenset({"id", "split", "confirmed"})
LONG_TEXT = 160
_LITERAL_MAX = 200_000


def L(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


# 认识的字段名给双语的标签；不认识的用原样的字段名。
KEYS = {
    "source": L("来源子集", "Source subset"), "split": L("回答来自", "Responses from"), "subset": L("子集", "Subset"),
    "category": L("类别", "Category"), "difficulty": L("难度", "Difficulty"), "domain": L("领域", "Domain"),
    "sub_domain": L("子领域", "Sub-domain"), "length": L("长度档", "Length bucket"),
    "context_bytes": L("长篇上下文（不公开）", "Long context (not shown)"), "task_id": L("任务号", "Task id"),
    "purpose": L("目的", "Purpose"), "repo": L("仓库", "Repository"), "base_commit": L("基准提交", "Base commit"),
    "version": L("版本", "Version"), "created_at": L("提出时间", "Created at"), "docker_image": L("镜像", "Image"),
    "portable": L("工作目录能取出（没有二进制或过大的文件）", "Portable work directory"),
    "expert_time_estimate_min": L("专家用时（分钟）", "Expert time (minutes)"), "redacted": L("处理", "Redaction"),
    "selection": L("选题", "Selection"),
}


def parse_literal(value: str) -> Any:
    """写成 JSON 或 Python 字面量的字符串（形如 [...] 或 {...}）展开成值；不是的返回 None。只认字面量，不执行代码。"""
    s = value.strip()
    if len(s) < 2 or len(s) > _LITERAL_MAX or (s[0], s[-1]) not in (("[", "]"), ("{", "}")):
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return parsed if isinstance(parsed, (list, dict)) else None


def _str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _key(name: str) -> Any:
    return KEYS.get(name, name)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def text(label: Any, value: Any, *, mono: bool = False) -> dict[str, Any]:
    return {"type": "text", "label": label, "text": _str(value), "mono": mono}


def field(key: Any, value: Any, href: str | None = None) -> dict[str, Any]:
    out = {"key": key, "value": _str(value)}
    if href and href.startswith("https://"):
        out["href"] = href
    return out


def fields(label: Any, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    items = [f for f in items if f["value"] != ""]
    return {"type": "fields", "label": label, "items": items} if items else None


def listing(label: Any, items: list[Any]) -> dict[str, Any] | None:
    return {"type": "list", "label": label, "items": [_str(i) for i in items]} if items else None


def candidate(label: Any, value: Any, mark: str | None = None) -> dict[str, Any]:
    out = {"label": label, "text": _str(value)}
    if mark:
        out["mark"] = mark
    return out


class _Row:
    """一行题；记下哪些值已经放进了专门的块，其余的由 rest 放进“其他字段”。"""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.extra = row["extra"] if isinstance(row.get("extra"), dict) else {}
        self.used = set(SKIP) | ({"extra"} if isinstance(row.get("extra"), dict) else set())
        self.used_extra: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.used.add(key)
        return self.row.get(key, default)

    def x(self, key: str, default: Any = None) -> Any:
        self.used_extra.add(key)
        return self.extra.get(key, default)

    def rest(self) -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self.row.items() if k not in self.used] + [(k, v) for k, v in self.extra.items() if k not in self.used_extra]


def expand(pairs: list[tuple[str, Any]], label: Any = None) -> list[dict[str, Any]]:
    """按值的形状画：短的标量进一张键值表，长文字单独一段，一列标量是列表，字典展开，别的原样给 JSON。字符串形式的字面量先展开。"""
    short: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for key, value in pairs:
        if value is None or value == "":
            continue
        if isinstance(value, str):
            parsed = parse_literal(value)
            value = parsed if parsed is not None else value
        if key == "selection" and isinstance(value, dict):
            short.append(field(_key(key), " · ".join(f"{k}={v}" for k, v in value.items())))
        elif isinstance(value, dict):
            out.extend(expand(list(value.items()), _key(key)))
        elif isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
            if value:
                out.append(listing(_key(key), value))
        elif isinstance(value, (dict, list)):
            out.append({"type": "json", "label": _key(key), "value": value})
        elif isinstance(value, str) and (len(value) > LONG_TEXT or "\n" in value):
            out.append(text(_key(key), value))
        else:
            short.append(field(_key(key), value))
    head = fields(label or L("其他字段", "Other fields"), short)
    return ([head] if head else []) + out


def _done(r: _Row, blocks: list[dict[str, Any] | None], question: Any, answer: Any) -> dict[str, Any]:
    blocks = [b for b in blocks if b] + expand(r.rest())
    return {"blocks": blocks, "summary": {"question": question, "answer": answer}}


def pairwise(r: _Row, **_: Any) -> dict[str, Any]:
    question, label = _str(r.get("question", "")), _str(r.get("answer", ""))
    better = {"A>B": "A", "B>A": "B"}.get(label)
    items = [candidate(side, r.x(f"response_{side}", ""), "better" if better == side else None) for side in ("A", "B")]
    meta = fields(L("标注", "Labels"), [field(L("更好的是", "Better"), better or ""), field(L("原样的标注", "Label as given"), label),
                                       field(_key("source"), r.x("source", "")), field(_key("split"), r.x("split", ""))])
    blocks = [text(L("题目", "Question"), question), {"type": "candidates", "label": L("两个候选回答", "Two candidate responses"), "columns": True, "items": items}, meta]
    return _done(r, blocks, _first_line(question), L(f"更好的是 {better}", f"{better} is better") if better else label)


def best_of_n(r: _Row, **_: Any) -> dict[str, Any]:
    question = _str(r.get("question", ""))
    chosen, rejected = r.get("answer") or [], r.x("rejected") or []
    chosen = chosen if isinstance(chosen, list) else [chosen]
    rejected = rejected if isinstance(rejected, list) else [rejected]
    one = len(chosen) == 1
    items = [candidate(L("好回答", "Chosen") if one else L(f"好回答 {i}", f"Chosen {i}"), c, "chosen") for i, c in enumerate(chosen, 1)]
    items += [candidate(L(f"差回答 {i}", f"Rejected {i}"), c, "rejected") for i, c in enumerate(rejected, 1)]
    blocks = [text(L("题目", "Prompt"), question), {"type": "candidates", "label": L("候选回答", "Candidate responses"), "columns": False, "items": items},
              fields(L("标注", "Labels"), [field(_key("subset"), r.x("subset", ""))])]
    return _done(r, blocks, _first_line(question), L(f"{len(chosen)} 个好回答、{len(rejected)} 个差回答", f"{len(chosen)} chosen, {len(rejected)} rejected"))


def multiple_choice(r: _Row, **_: Any) -> dict[str, Any]:
    question, answer = _str(r.get("question", "")), _str(r.get("answer", ""))
    letters = sorted(k[len("choice_"):] for k in r.extra if k.startswith("choice_"))
    items = [candidate(x, r.x(f"choice_{x}", ""), "correct" if x == answer else None) for x in letters]
    meta = [field(_key(k), r.x(k, "")) for k in ("domain", "sub_domain", "difficulty", "length")]
    if "context_bytes" in r.extra:
        meta.append(field(_key("context_bytes"), r.x("context_bytes")))
    blocks = [text(L("题目", "Question"), question), {"type": "candidates", "label": L("选项", "Options"), "columns": False, "items": items},
              fields(L("标注", "Labels"), meta)]
    chosen = next((c["text"] for c in items if c.get("mark")), "")
    return _done(r, blocks, _first_line(question), f"{answer}. {chosen}" if chosen else answer)


def _messages(value: Any) -> list[dict[str, str]] | None:
    turns = parse_literal(value) if isinstance(value, str) else value
    if not isinstance(turns, list):
        return None
    flat = [m for turn in turns for m in (turn if isinstance(turn, list) else [turn])]
    if not all(isinstance(m, dict) and "content" in m for m in flat):
        return None
    return [{"role": _str(m.get("role", "")), "content": _str(m["content"])} for m in flat]


def tool_call(r: _Row, **_: Any) -> dict[str, Any]:
    raw = r.get("question", "")
    messages = _messages(raw)
    blocks: list[dict[str, Any] | None] = [{"type": "messages", "label": L("对话", "Conversation"), "items": messages} if messages else text(L("题目", "Question"), raw)]
    tools = r.x("function") or []
    blocks.append({"type": "tools", "label": L("可用的工具", "Available tools"),
                   "items": [{"name": _str(t.get("name", "")), "description": _str(t.get("description", "")), "parameters": t.get("parameters", {})}
                             for t in tools if isinstance(t, dict)]})
    expected = r.get("answer") or []
    calls = [{"name": name, "arguments": args} for call in expected if isinstance(call, dict) for name, args in call.items()]
    blocks.append({"type": "calls", "label": L("期望的调用（每个参数列出可接受的值，空字符串表示可以不传）",
                                               "Expected calls (acceptable values per parameter; an empty string means it may be omitted)"), "items": calls})
    blocks.append(fields(L("标注", "Labels"), [field(_key("category"), r.x("category", ""))]))
    users = [m["content"] for m in messages or [] if m["role"] == "user"]
    return _done(r, blocks, users[-1] if users else _first_line(_str(raw)), ", ".join(c["name"] for c in calls))


SCENARIO = {
    "reason_for_call": L("来电原因", "Reason for the call"), "known_info": L("用户知道的", "What the user knows"),
    "unknown_info": L("用户不知道的", "What the user does not know"), "task_instructions": L("给模拟用户的要求", "Instructions to the simulated user"),
    "persona": L("用户画像", "Persona"),
}


def agent_scenario(r: _Row, **_: Any) -> dict[str, Any]:
    raw = r.get("question", "")
    scenario = parse_literal(raw) if isinstance(raw, str) else raw
    blocks: list[dict[str, Any] | None] = []
    reason = ""
    if isinstance(scenario, dict):
        inner = scenario.get("instructions") if isinstance(scenario.get("instructions"), dict) else {}
        reason = _str(inner.get("reason_for_call", ""))
        blocks.append(fields(L("用户场景", "User scenario"), [field(_key("domain"), inner.get("domain", ""))]))
        blocks += [text(SCENARIO.get(k, k), v) for k, v in inner.items() if k != "domain" and v not in (None, "")]
        others = [(k, v) for k, v in scenario.items() if k != "instructions"]
        blocks += expand(others, L("用户场景的其他部分", "Rest of the scenario")) if others else []
        if not isinstance(scenario.get("instructions"), dict) and "instructions" in scenario:
            blocks += expand([("instructions", scenario["instructions"])])
    else:
        blocks.append(text(L("用户场景", "User scenario"), raw))
    raw_answer = r.get("answer")
    answer = raw_answer if isinstance(raw_answer, dict) else {}
    if not answer and raw_answer not in (None, "", {}):
        blocks.append({"type": "json", "label": L("判分依据", "Scoring criteria"), "value": raw_answer})
    actions = []
    for a in answer.get("actions") or []:
        call = {"name": _str(a.get("name", "")), "arguments": a.get("arguments", {})}
        if a.get("action_id"):
            call["id"] = _str(a["action_id"])
        if a.get("requestor"):
            call["by"] = _str(a["requestor"])
        call.update({k: v for k, v in a.items() if k not in ("name", "arguments", "action_id", "requestor")})
        actions.append(call)
    blocks.append({"type": "calls", "label": L("期望的动作（按顺序）", "Expected actions (in order)"), "items": actions})
    blocks.append(listing(L("要告诉用户的信息", "Must tell the user"), answer.get("communicate_info") or []))
    blocks.append(listing(L("自然语言的判定条件", "Natural-language checks"), answer.get("nl_assertions") or []))
    if answer.get("env_assertions"):
        blocks.append({"type": "json", "label": L("环境断言", "Environment assertions"), "value": answer["env_assertions"]})
    blocks.append(fields(L("计分", "Scoring"), [field(L("计分依据", "Reward basis"), ", ".join(map(_str, answer.get("reward_basis") or [])))]))
    known = {"actions", "communicate_info", "nl_assertions", "env_assertions", "reward_basis"}
    blocks += expand([(k, v) for k, v in answer.items() if k not in known])
    description = r.x("description")
    blocks += expand(list(description.items()) if isinstance(description, dict) else [("description", description)], L("说明", "Notes"))
    n = len(actions)
    names = "、".join(a["name"] for a in actions[:3]) + ("…" if n > 3 else "")
    names_en = ", ".join(a["name"] for a in actions[:3]) + ("…" if n > 3 else "")
    return _done(r, blocks, reason or _first_line(_str(raw)), L(f"期望 {n} 个动作：{names}" if n else "不需要动作", f"{n} expected actions: {names_en}" if n else "No actions expected"))


def code_patch(r: _Row, **_: Any) -> dict[str, Any]:
    repo = _str(r.get("repo", ""))
    meta = [field(_key("repo"), repo, f"https://github.com/{repo}" if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else None)]
    meta += [field(_key(k), r.get(k, "")) for k in ("base_commit", "version", "difficulty", "created_at")]
    issue = _str(r.get("question", ""))
    answer = r.get("answer") if isinstance(r.row.get("answer"), dict) else {}
    patch = _str(answer.get("patch", ""))
    blocks = [fields(L("题目信息", "Instance"), meta), text(L("Issue", "Issue"), issue)]
    if r.get("hints_text"):
        blocks.append(text(L("提示（Issue 下的讨论）", "Hints"), r.row["hints_text"]))
    blocks.append(text(L("参考补丁", "Reference patch"), patch, mono=True))
    if answer.get("test_patch"):
        blocks.append(text(L("测试补丁", "Test patch"), answer["test_patch"], mono=True))
    fail, keep = answer.get("FAIL_TO_PASS") or [], answer.get("PASS_TO_PASS") or []
    blocks += [listing(L("修好后要转为通过的测试", "Tests that must start passing"), fail), listing(L("要继续通过的测试", "Tests that must keep passing"), keep)]
    blocks += expand([(k, v) for k, v in answer.items() if k not in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS")])
    files = len(re.findall(r"^diff --git ", patch, flags=re.M))
    return _done(r, blocks, _first_line(issue), L(f"参考补丁改 {files} 个文件；{len(fail)} 条测试要转为通过",
                                                  f"Reference patch touches {files} files; {len(fail)} test{'s' if len(fail) != 1 else ''} must start passing"))


def terminal_task(r: _Row, **_: Any) -> dict[str, Any]:
    meta = [field(_key(k), r.get(k, "")) for k in ("category", "difficulty", "expert_time_estimate_min", "docker_image", "portable")]
    instructions = _str(r.get("question", ""))
    answer = r.get("answer") if isinstance(r.row.get("answer"), dict) else {}
    blocks: list[dict[str, Any] | None] = [fields(L("题目信息", "Task"), meta)]
    if r.get("description"):
        blocks.append(text(L("考什么", "What it tests"), r.row["description"]))
    blocks.append(text(L("题目说明", "Task instructions"), instructions))
    blocks.append(fields(L("判分", "Grading"), [field(L("判分方式", "Graded by"), answer.get("graded_by", ""))]))
    tests = answer.get("tests") if isinstance(answer.get("tests"), dict) else {}
    blocks += [text(f"tests/{name}", body, mono=True) for name, body in tests.items()]
    blocks += expand([(k, v) for k, v in answer.items() if k not in ("graded_by", "tests")])
    return _done(r, blocks, _first_line(instructions), _str(answer.get("graded_by", "")))


def short_answer(r: _Row, **_: Any) -> dict[str, Any]:
    question, answer = _str(r.get("question", "")), r.get("answer", "")
    return _done(r, [text(L("题目", "Question"), question), text(L("标准答案", "Reference answer"), answer)], _first_line(question), _str(answer))


def deliverable(r: _Row, *, question_key: str = "message", answer_key: str = "expected_output", **_: Any) -> dict[str, Any]:
    question, answer = _str(r.get(question_key, "")), _str(r.get(answer_key, ""))
    blocks = [text(L("请求", "Request"), question), text(L("期望的回答", "Expected answer"), answer),
              listing(L("判分维度", "Metrics"), r.get("metrics") or [])]
    return _done(r, blocks, _first_line(question), _first_line(answer))


def retrieval(r: _Row, *, question_key: str = "question", answer_key: str = "reference", context: list[dict[str, Any]] | None = None,
              context_ids: str = "", **_: Any) -> dict[str, Any]:
    question, answer = _str(r.get(question_key, "")), _str(r.get(answer_key, ""))
    ids = [_str(i) for i in (r.get(context_ids) or [])] if context_ids else []
    memories = [candidate(" · ".join(_str(m[k]) for k in ("id", "kind") if m.get(k)), m.get("text", "")) for m in context or []]
    blocks = [text(L("问题", "Question"), question), text(L("参考答案", "Reference answer"), answer)]
    if memories:
        blocks.append({"type": "candidates", "label": L("这题引用的记忆", "Memories this question refers to"), "columns": False, "items": memories})
    found = {_str(m.get("id")) for m in context or []}
    blocks.append(listing(L("引用的记忆（语料里没有）", "Referenced memories missing from the corpus"), [i for i in ids if i not in found]))
    return _done(r, blocks, _first_line(question), _first_line(answer))


LAYOUTS: dict[str, Callable[..., dict[str, Any]]] = {
    "pairwise": pairwise, "best_of_n": best_of_n, "multiple_choice": multiple_choice, "tool_call": tool_call,
    "agent_scenario": agent_scenario, "code_patch": code_patch, "terminal_task": terminal_task, "short_answer": short_answer,
    "deliverable": deliverable, "retrieval": retrieval,
}


def build_view(layout: str, row: dict[str, Any], **options: Any) -> dict[str, Any] | None:
    """一行题的 view；layout 不认识时为 None（页面照旧画 detail）。options 给 deliverable、retrieval 的键名与引用的记忆。"""
    make = LAYOUTS.get(layout)
    if make is None:
        return None
    view = make(_Row(row), **options)
    return {"layout": layout, **view}
