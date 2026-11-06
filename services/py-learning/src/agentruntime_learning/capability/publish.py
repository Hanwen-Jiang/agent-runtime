"""把能力矩阵的分数写成 Profile 的 capability_scores（docs/plan/capability-matrix-evals.md 第 1、6、8 节）。

scores.json（matrix.score_results）里可发布的分数 → 每个候选 Profile 一条
`{task_family, role, tier, score, source, at}`，并进 deploy/dev/model-profiles.json 与 deploy/dev/registry-seed.json 的
model_profile（两者是同一组 Profile，格式与 scripts/sync_model_profiles.py 相同）。之后人审，再按现有流程经 Registry 发布。

- 同一维度（task_family × role × tier）的旧条目被新的替换；不可发布的（样本不足、污染过多、不领先）不写，也不删旧的。
- 刷新时删掉 at 超过 90 天的条目（所有 Profile 都查），回到 0.5 的先验。
- 写之前校验：model_profile.schema.json，加上 Go 那边（Registry 与 Model Gateway）的规则：score 在 [0,1]、维度不重复、
  role 是附录 A 的角色、task_family 合法、Tier 属于这个 Profile。
- source 形如 `capmatrix:s09+bfcl-v4@2026-11-10:n=60,r=3:run=round1`：评测、观测日期、题数、重复次数与计划名，
  能追溯到输出目录里的 scores.json 与 results.jsonl。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from agentruntime_learning.capability.base import ROLES
from agentruntime_learning.capability.publication import POLICY_VERSION
from agentruntime_learning.capability.matrix import REPO
from agentruntime_learning.evals import schemas

PROFILES = REPO / "deploy" / "dev" / "model-profiles.json"
SEED = REPO / "deploy" / "dev" / "registry-seed.json"
SYNC_SCRIPT = REPO / "scripts" / "sync_model_profiles.py"
MAX_AGE_DAYS = 90
PATCH = "capability-patch.json"
_FAMILY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class PublishError(ValueError):
    """写出来的 Profile 不合格（schema 或 Go 的规则）。"""


def source_of(cell: Mapping[str, Any], candidate: Mapping[str, Any], plan: str) -> str:
    evals = "+".join(e["name"] for e in cell["evals"])
    digest = cell["publication"]["evidence_sha256"]
    return f"capmatrix:{evals}@{cell['at']}:n={candidate['items']},r={candidate['repeats']}:run={plan}:evidence={digest}"


def patch_from_scores(report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """scores.json → {Profile id: [capability_scores 条目]}；只收可发布的。"""
    patch: dict[str, list[dict[str, Any]]] = {}
    for cell in report["cells"]:
        evidence = cell.get("publication") or {}
        if evidence.get("policy") != POLICY_VERSION or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("evidence_sha256", ""))):
            raise PublishError("报告缺少当前发布约束与证据摘要；先从原始 results.jsonl 重新 score，旧规划记录不能发布")
        if not evidence.get("coverage_ok"):
            continue
        if not cell.get("at"):
            continue
        for c in cell["candidates"]:
            if not c.get("publishable") or c.get("score") is None:
                continue
            patch.setdefault(c["candidate"], []).append(
                {
                    "task_family": cell["task_family"],
                    "role": cell["role"],
                    "tier": cell["tier"],
                    "score": round(float(c["score"]), 4),
                    "source": source_of(cell, c, str(report["plan"])),
                    "at": cell["at"],
                }
            )
    return patch


def _dims(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(entry.get("task_family")), str(entry.get("role")), str(entry.get("tier")))


def _too_old(entry: Mapping[str, Any], today: dt.date, max_age_days: int) -> bool:
    try:
        at = dt.date.fromisoformat(str(entry.get("at")))
    except ValueError:
        return False  # 格式不对的交给校验报错，不悄悄删掉
    return (today - at).days > max_age_days


def merge(profiles: Sequence[Mapping[str, Any]], patch: Mapping[str, Sequence[Mapping[str, Any]]], *, today: dt.date, max_age_days: int = MAX_AGE_DAYS) -> tuple[list[dict[str, Any]], list[str]]:
    """按补丁改 Profile（纯函数）：同维度替换、过期的删掉。返回（新的 Profile 列表、改动说明）。"""
    known = {str(p["id"]) for p in profiles}
    unknown = sorted(set(patch) - known)
    if unknown:
        raise PublishError(f"补丁里的 Profile 不在文件里：{'、'.join(unknown)}")
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for profile in profiles:
        doc = json.loads(json.dumps(profile))
        pid = str(doc["id"])
        old = list(doc.get("capability_scores") or [])
        kept = []
        for entry in old:
            if _too_old(entry, today, max_age_days):
                notes.append(f"{pid}：删掉过期的 {'/'.join(_dims(entry))}（{entry.get('at')}）")
            else:
                kept.append(entry)
        for entry in patch.get(pid, []):
            before = next((e for e in kept if _dims(e) == _dims(entry)), None)
            kept = [e for e in kept if _dims(e) != _dims(entry)]
            kept.append(dict(entry))
            was = f"{before['score']}（{before.get('at')}）" if before else "先验 0.5"
            notes.append(f"{pid}：{'/'.join(_dims(entry))} {was} → {entry['score']}（{entry['source']}）")
        kept.sort(key=_dims)
        if kept:
            doc["capability_scores"] = kept
        else:
            doc.pop("capability_scores", None)
        out.append(doc)
    return out, notes


def profile_problems(profile: Mapping[str, Any]) -> list[str]:
    """model_profile.schema.json，加上 Registry（validate.go）与 Model Gateway（profiles.go）对 capability_scores 的检查。"""
    pid = str(profile.get("id"))
    found = [f"{pid}：{p}" for p in schemas.problems(schemas.validator("model_profile"), profile)]
    tiers = [str(t.get("name")) for t in profile.get("tiers") or []]
    if len(set(tiers)) != len(tiers):
        found.append(f"{pid}：Tier 名重复")
    seen: set[tuple[str, str, str]] = set()
    for entry in profile.get("capability_scores") or []:
        dims = _dims(entry)
        if dims in seen:
            found.append(f"{pid}：capability_scores 的 task_family/role/tier 重复：{'/'.join(dims)}")
        seen.add(dims)
        if dims[2] not in tiers:
            found.append(f"{pid}：capability_scores 引用了不存在的 Tier {dims[2]!r}")
        if dims[1] not in ROLES:
            found.append(f"{pid}：capability_scores 的 role {dims[1]!r} 不是附录 A 的角色")
        if not _FAMILY.match(dims[0]):
            found.append(f"{pid}：capability_scores 的 task_family {dims[0]!r} 不合法")
        score = entry.get("score")
        if not isinstance(score, int | float) or isinstance(score, bool) or math.isnan(score) or not 0 <= score <= 1:
            found.append(f"{pid}：capability_scores 的 score 必填且在 [0,1] 内")
        if not str(entry.get("source") or "").strip():
            found.append(f"{pid}：capability_scores 的 source 必填")
        try:
            dt.date.fromisoformat(str(entry.get("at")))
        except ValueError:
            found.append(f"{pid}：capability_scores 的 at 要是 YYYY-MM-DD")
    return found


def _sync_module(path: Path = SYNC_SCRIPT) -> ModuleType:
    """Profile 文件的格式（手写的格式）与种子的镜像都用 scripts/sync_model_profiles.py 的，保证两边写出来的一样。"""
    if not path.exists():
        raise PublishError(f"找不到 {path}：发布要在仓库的检出里跑")
    spec = importlib.util.spec_from_file_location("sync_model_profiles", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish(
    report: Mapping[str, Any],
    *,
    profiles_path: Path = PROFILES,
    seed_path: Path = SEED,
    today: dt.date | None = None,
    max_age_days: int = MAX_AGE_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按 scores.json 改两个文件（dry_run 时不写）；返回 {patch, notes, changed}。不合格时抛 PublishError，什么都不写。"""
    today = today or dt.datetime.now(dt.UTC).date()
    sync = _sync_module()
    document = json.loads(Path(profiles_path).read_text("utf-8"))
    patch = patch_from_scores(report)
    updated, notes = merge(document.get("profiles") or [], patch, today=today, max_age_days=max_age_days)
    problems = [p for profile in updated for p in profile_problems(profile)]
    if problems:
        raise PublishError("写出来的 Profile 不合格：\n" + "\n".join(f"  {p}" for p in problems))
    new_doc = {**document, "profiles": updated}
    seed = json.loads(Path(seed_path).read_text("utf-8"))
    profiles_text = sync.dump_profiles(new_doc)
    seed_text = sync._dump(sync.mirror_seed(seed, updated))
    changed = profiles_text != Path(profiles_path).read_text("utf-8") or seed_text != Path(seed_path).read_text("utf-8")
    if changed and not dry_run:
        Path(profiles_path).write_text(profiles_text, "utf-8", newline="\n")
        Path(seed_path).write_text(seed_text, "utf-8", newline="\n")
    return {"patch": patch, "notes": notes, "changed": changed}


__all__ = ["MAX_AGE_DAYS", "PATCH", "PROFILES", "SEED", "PublishError", "merge", "patch_from_scores", "profile_problems", "publish", "source_of"]
