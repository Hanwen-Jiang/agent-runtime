"""Public cohort facts. Do not expose examples, labels, or storage paths."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def sha(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) else None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def facts(dataset: str, method: str, source: str, zh: str, en: str, **details: Any) -> dict[str, Any]:
    return {"dataset": dataset, "method": method, "source": source, "method_detail_zh": zh,
            "method_detail_en": en, **{k: v for k, v in details.items() if v is not None}}


# Public 40-item cohort published by scripts/export_public_items.py and used by
# the 2026-11-10 baseline. Only the audited cohort hash identifies this selection.
PUBLIC_LONGBENCH_40 = "a50aeaac8abfd5aa69d00b4f31080ee11e15b5ac0d2b3c164bcca0456be83415"


def longbench_selection(report: dict, split: str | None = None, excluded: int | None = None) -> dict:
    cohort = sha(report.get("cohort_sha256"))
    size = count(report.get("sample_count"))
    # Validation reports without an exclusion manifest cannot borrow numbers from
    # another run of the same protocol. Historical counts bind to exact cohorts.
    if split == "validation" and excluded is None:
        excluded = {
            "6fdf8b1f9b91524f55678f5c327226b0cedf6d6ca99c1881b8a84c811dc7751c": 40,
            "644ae1327462b8fcd82dd8e8519f4a39c6404eb1d16a4bc946c3afda683c6f83": 60,
            "18e4023b2fe42d35025b4f458493fb8b3901ba9b6a85ba217c155fe8ef2df229": 83,
        }.get(cohort)
    public40 = size == 40 and cohort == PUBLIC_LONGBENCH_40
    if public40:
        method, zh, en = ("seeded_hash_prefix", "使用已发布的 40 题清单：按 seed 与题号的 SHA-256 排序取前 40 题，各策略复用同一清单。",
                          "All variants reuse the published first 40 items ranked by SHA-256 of the seed and item ID.")
    elif excluded is not None:
        method, zh, en = ("frozen_cohort", f"调优前冻结验收清单，排除此前使用或预览的 {excluded} 道题；两臂按同一清单运行。",
                          f"The validation list was frozen before tuning, excluding {excluded} earlier or previewed items. Both arms use it.")
    else:
        method, zh, en = ("frozen_cohort", "两臂使用同一份固定题单；报告未记录从原始数据集抽取这份清单的算法。",
                          "Both arms use the same fixed item list. The report does not record how it was drawn from the original dataset.")
    version = report.get("dataset_version")
    version = version if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", version) else None
    return facts("LongBench v2", method, "public-summary.json", zh, en, selected_count=size,
                 split=split or ("development" if public40 else None), cohort_sha256=cohort,
                 version=version, seed=0 if public40 else None, excluded_count=excluded,
                 reference_url="https://evals.cgent.link/api/datasets/longbench.json" if public40 else None)
