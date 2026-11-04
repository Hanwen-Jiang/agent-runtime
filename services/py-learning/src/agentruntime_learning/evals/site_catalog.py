"""评测网站的 /datasets 数据（docs/plan/eval-site.md）：从 evalsets 目录读出每个数据集的题、答案、来源与人审状态。只用标准库。

evalsets/site.json 说明哪些目录上页面、属于哪个评测类别、来源类型（authored 自编、adapted 改编、original 原题）、用哪套框架，
以及框架的介绍；没在里面说明的目录不上页面。每个数据集的 items 写题在哪里：

- ``task_cases``（默认）、``reviewer_samples``、``context_samples``：目录里每个 JSON 一道题（evals.cases 的格式）；
- ``jsonl``：``file`` 里一行一道题，``question``、``answer`` 给出题目与答案的键，可选 ``context_file`` + ``context_ids``
  （例如 memory-rag 的问题引用 corpus 里的记忆）。

守住的规则：

- 留出集（split 为 holdout）的题只计数：题目、答案、id、来源都不进输出。
- 每题要有来源：自编题来自 site.json 的 work_item；改编与原题来自同目录 sources.jsonl 里同 id（或 case_id）的一行。
  缺来源的题不进输出，记进 excluded。
- 人审状态：task 类的 review.status，jsonl 的 confirmed；已确认的带确认人与日期。原题（source_type 为 original）一律是 original：
  官方的题与答案，不需人审；来源写在 site.json 的数据集上（``source``），每题加上自己的 id。
- jsonl 数据集写了 ``layout``（题型，见 site_views）时每题带 ``view``（页面按题型画的几块与摘要），不再带原样的 ``detail``；
  没写或不认识的照旧带 detail。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentruntime_learning.evals.site_views import build_view

GROUPINGS = ("eval_category", "source_type", "framework", "review", "dataset")
DEFAULT_GROUPING = "eval_category"
SITE_FILE = "site.json"
SOURCES_FILE = "sources.jsonl"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DIR_ITEMS = ("task_cases", "reviewer_samples", "context_samples")
SOURCE_TYPES = ("authored", "adapted", "original")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _inside(root: Path, rel: str) -> Path | None:
    """rel 只能指向 root 里面（site.json 也来自仓库，照样不信）。"""
    path = (root / rel).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def _review(doc: dict[str, Any]) -> dict[str, Any]:
    review = doc.get("review")
    if isinstance(review, dict) and review.get("status") == "confirmed":
        return {k: review[k] for k in ("status", "confirmed_by", "confirmed_at") if review.get(k)}
    if isinstance(review, dict) and review.get("status"):
        return {"status": str(review["status"])}
    if doc.get("confirmed") is True:
        return {"status": "confirmed"}
    return {"status": "draft"}


def _source(spec: dict[str, Any], record: dict[str, Any] | None, item_id: str) -> dict[str, Any] | None:
    if spec["source_type"] == "authored":
        return {"kind": "authored", "work_item": spec.get("work_item", "")}
    if record is None and spec["source_type"] == "original" and isinstance(spec.get("source"), dict):
        # 原题：一个数据集一个来源（上游、版本、许可证），每题加上自己的 id
        record = {**spec["source"], "source_url": spec["source"].get("url", ""), "source_id": item_id}
    if record is None:
        return None
    url = record.get("source_url") or record.get("upstream_repository") or record.get("dataset_url")
    out = {"kind": "upstream", "dataset": record.get("dataset", ""), "url": url or "",
           "revision": record.get("revision", ""), "license": record.get("license", "")}
    for key in ("source_id", "instance_id", "source_split", "official_split", "row_index_zero_based", "derived_from",
                "selection", "round_origin", "authored_fields", "source_fields"):
        if key in record:
            out[key] = record[key]
    return out


def _task_question_answer(kind: str, doc: dict[str, Any]) -> tuple[str, Any]:
    if kind == "task_cases":
        planner = doc.get("planner", {})
        expect = planner.get("expect", {})
        return str(planner.get("input", {}).get("message", "")), {k: expect[k] for k in ("triage", "risk", "shape", "task_family") if k in expect}
    if kind == "reviewer_samples":
        expect = doc.get("checkpoint", {}).get("expect", {})
        return str(doc.get("description", "")), {k: expect[k] for k in ("verdict", "acceptable", "min_escalation") if k in expect}
    return str(doc.get("description", "")), [{"question": p.get("question"), "answer": p.get("answer")} for p in doc.get("probes", [])]


@dataclass
class Catalog:
    datasets: list[dict[str, Any]] = field(default_factory=list)
    items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    holdout: dict[str, int] = field(default_factory=dict)
    frameworks: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def index(self) -> dict[str, Any]:
        return {"datasets": self.datasets, "frameworks": self.frameworks, "groupings": list(GROUPINGS),
                "default_grouping": DEFAULT_GROUPING, "excluded": self.excluded, **self.notes}

    def dataset(self, name: str) -> dict[str, Any] | None:
        if not isinstance(name, str) or not NAME_RE.match(name) or name not in self.items:
            return None
        meta = next(d for d in self.datasets if d["name"] == name)
        return {**meta, "items": self.items[name], "holdout": {"count": self.holdout[name]}}


def build_catalog(root: Path) -> Catalog:
    root = Path(root)
    site_path = root / SITE_FILE
    site = _read_json(site_path) if site_path.is_file() else {}
    catalog = Catalog(frameworks=list(site.get("frameworks", [])),
                      notes={k: site[k] for k in ("categories", "source_types", "intro") if k in site})
    for spec in site.get("datasets", []):
        name = str(spec.get("name", ""))
        if not NAME_RE.match(name) or spec.get("source_type") not in SOURCE_TYPES or name in catalog.items:
            continue
        base = _inside(root, str(spec.get("path", "")))
        if base is None or not base.is_dir():
            continue
        _load_dataset(catalog, name, spec, base)
    return catalog


def _load_dataset(catalog: Catalog, name: str, spec: dict[str, Any], base: Path) -> None:
    sources: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(base / SOURCES_FILE):
        key = record.get("id") or record.get("case_id")
        if key:
            sources[str(key)] = record
    kind = spec.get("items", "task_cases")
    # id, split, question, answer, review, detail, 原样的一行（只有 jsonl 的；按题型画题用）
    raw: list[tuple[str, str, str, Any, dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    if kind in DIR_ITEMS:
        folder = base / kind
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            doc = _read_json(path)
            question, answer = _task_question_answer(kind, doc)
            detail = {k: v for k, v in doc.items() if k not in ("id", "split", "review", "kind")}
            raw.append((str(doc.get("id", path.stem)), str(doc.get("split", "dev")), question, answer, _review(doc), detail, None))
    elif kind == "jsonl":
        file = _inside(base, str(spec.get("file", "")))
        context = {}
        if spec.get("context_file"):
            ctx_path = _inside(base, str(spec["context_file"]))
            context = {str(r.get("id")): r for r in _read_jsonl(ctx_path)} if ctx_path else {}
        for row in _read_jsonl(file) if file else []:
            detail = {k: v for k, v in row.items() if k not in ("id", "split", "confirmed")}
            if spec.get("context_ids") and context:
                detail["context"] = [context[i] for i in row.get(spec["context_ids"], []) if i in context]
            raw.append((str(row.get("id", "")), str(row.get("split", "dev")), str(row.get(spec.get("question", "message"), "")),
                        row.get(spec.get("answer", "expected_output")), _review(row), detail, row))
    counts = {"total": len(raw), "shown": 0, "holdout_hidden": 0, "confirmed": 0, "draft": 0, "original": 0, "excluded": 0}
    items: list[dict[str, Any]] = []
    for item_id, split, question, answer, review, detail, row in raw:
        if spec["source_type"] == "original":
            review = {"status": "original"}  # 官方的题与答案，不需人审
        counts[review["status"] if review["status"] in ("confirmed", "original") else "draft"] += 1
        if split == "holdout":
            counts["holdout_hidden"] += 1
            continue
        record = sources.get(item_id)
        source = _source(spec, record, item_id)
        if source is None or not item_id:
            counts["excluded"] += 1
            catalog.excluded.append({"dataset": name, "id": item_id, "reason": "no_source"})
            continue
        group = (record or {}).get("group_id") or (record or {}).get("derived_from") or item_id
        item = {
            "id": item_id, "dataset": name, "source_type": spec["source_type"], "framework": spec.get("framework", ""),
            "eval_category": spec.get("eval_category", ""), "review": review, "question": question, "answer": answer,
            "source": source, "group_id": group, "split": split, "detail": detail,
        }
        view = build_view(str(spec["layout"]), row, question_key=spec.get("question", "message"), answer_key=spec.get("answer", "expected_output"),
                          context=detail.get("context"), context_ids=spec.get("context_ids", "")) if row is not None and spec.get("layout") else None
        if view is not None:
            item["view"] = view
            del item["detail"]  # 内容都在 view 里
        items.append(item)
        counts["shown"] += 1
    catalog.items[name] = items
    catalog.holdout[name] = counts["holdout_hidden"]
    catalog.datasets.append({
        "name": name, "title": spec.get("title", {}), "description": spec.get("description", {}),
        "source_type": spec["source_type"], "framework": spec.get("framework", ""), "eval_category": spec.get("eval_category", ""),
        "work_item": spec.get("work_item", ""), "counts": counts,
    })
