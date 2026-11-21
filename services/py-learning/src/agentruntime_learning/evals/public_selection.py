"""Public fields describing the cohort actually selected for one evaluation run."""

from __future__ import annotations

import re
from typing import Any

_LABEL = re.compile(r"[\w][\w .()+:@-]{0,126}")
_HASH = re.compile(r"[0-9a-f]{64}")
_METHODS = frozenset({
    "stratified_repo_difficulty", "seeded_random", "explicit_ids", "full", "filtered_all", "unrecorded",
    "per_evaluator", "seeded_hash_prefix", "seeded_round_robin", "seeded_shuffle_prefix", "predefined_split",
    "frozen_cohort", "historical_unrecorded", "supplied_cases",
})
_COUNTS = ("population_count", "eligible_count", "selected_count", "excluded_count", "excluded_id_count",
           "requested_count", "observed_count", "limit")


def private_selection(row: dict[str, Any]) -> bool:
    return (row.get("ids_hidden") is True or row.get("holdout_hidden") is True or row.get("visibility") == "private"
            or any(isinstance(v, str) and re.search(r"h[oe]ld[- _]?out|private|hidden", v, re.I)
                   for v in (row.get("split"), row.get("dataset"), row.get("eval_set_name"))))


def clean_selection(value: Any, *, private: bool = False, group: bool = False, projected: bool = False,
                    benchmark: bool = False) -> dict[str, Any]:
    """A report can contain private inputs; only project bounded public metadata."""
    if not isinstance(value, dict):
        return {}
    info = {}
    for key in ("dataset", "split", "role", "tier", "version", "source"):
        raw = value.get(key)
        if isinstance(raw, str) and _LABEL.fullmatch(raw):
            info[key] = raw
    if isinstance(value.get("method"), str) and value["method"] in _METHODS:
        info["method"] = value["method"]
    for key in _COUNTS:
        if type(n := value.get(key)) is int and 0 <= n <= 2**53 - 1:
            info[key] = n
    if type(seed := value.get("seed")) is int and abs(seed) <= 2**53 - 1:
        info["seed"] = seed
    for key in ("dataset_sha256", "cohort_sha256"):
        if isinstance(v := value.get(key), str) and _HASH.fullmatch(v):
            info[key] = v
    private = private or private_selection(value)
    # Only the restricted per-suite projectors can supply explanatory prose, never a raw input file.
    if projected:
        for key in ("method_detail_zh", "method_detail_en"):
            if isinstance(v := value.get(key), str) and len(v) <= 1024:
                info[key] = v
        if isinstance(url := value.get("reference_url"), str) and url.startswith((
            "https://evals.cgent.link/api/datasets/", "https://github.com/Hanwen-Jiang/agent-runtime/blob/",
        )):
            info["reference_url"] = url
    if benchmark and not private and info.get("split") == "test":
        from agentruntime_learning.evals.public_benchmarks import PUBLIC_SWE_REPOS

        for key, pattern, maximum in (
            ("excluded_repos", r"[\w.-]+/[\w.-]+", 128),
            ("selection_ids", r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]{1,10}", 5000),
        ):
            vals = value.get(key)
            if isinstance(vals, list) and len(vals) <= maximum and all(isinstance(v, str) and re.fullmatch(pattern, v) for v in vals):
                repos = vals if key == "excluded_repos" else [v.rsplit("-", 1)[0].replace("__", "/") for v in vals]
                if all(r in PUBLIC_SWE_REPOS for r in repos) and (key != "selection_ids" or info.get("selected_count") == len(vals) == len(set(vals))):
                    info[key] = vals
        for key, labels in (("repo_counts", PUBLIC_SWE_REPOS),
                            ("difficulty_counts", {"<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours"})):
            if isinstance(counts := value.get(key), dict):
                info[key] = {k: n for k, n in counts.items() if k in labels and type(n) is int and 0 <= n <= 2**53 - 1}
        if value.get("reference_url") in (
            "https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified",
            "https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite",
            "https://huggingface.co/datasets/SWE-bench/SWE-bench",
        ):
            info["reference_url"] = value["reference_url"]
    if not group and isinstance(groups := value.get("groups"), list) and len(groups) <= 256:
        info["groups"] = [g for part in groups if (g := clean_selection(part, private=private, group=True, projected=projected))]
    return info
