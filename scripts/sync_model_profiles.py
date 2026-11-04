"""Model Profile 的单价与长度从 models.dev 取（docs/plan/feedback-2026-11-01.md N-07；2026-11-02 人定：不再按中转站去核实价格与长度）。

只用标准库。默认改 deploy/dev/model-profiles.json，并让 deploy/dev/registry-seed.json 里的 model_profile 与它一致（同一份 Profile，
Registry 与 Model Gateway 读的是同一组）。云上的 ConfigMap（Chart 的 modelGateway.profilesConfigMap）由运维从这个文件创建。

  uv run --no-sync python scripts/sync_model_profiles.py                 # 取 https://models.dev/api.json，改写两个文件
  … --check                                                              # 只比较，有出入时退出码 1（不改文件）
  … --catalog <api.json>                                                 # 用本地的 models.dev 数据（离线、测试）
  … --add openai/gpt-6.1-sol --class strong --tiers medium,high          # 加一个 Profile（id 为 a6api/<model>@<年-月>）

规则：
- 按 Profile 的 vendor 找 models.dev 里第一方的供应商（openai、anthropic、google、deepseek），按 model 找条目；找不到的不改并列出来。
- 单价：输入、输出、缓存读（美元每百万 token）取基础价；超过一定长度的加价（models.dev 的 tiers、context_over_200k）这里不记，见计划。
  要更新或加入的项缺任一项单价（没有这个键或为 null）时报错、两个文件都不改，不写 0、不静默跳过（R-06-B，
  docs/plan/delegated-review-2026-11-04.md 第 7 节第 3 条）；写明的 0 是免费，照收。嵌入模型没有缓存读的计价，不给 cache_read 时按 0。
- 长度：context_window 取 limit.input（有时）与 limit.context 中较小的；max_output 取 limit.output；嵌入模型的 max_output 按 schema 写 0。
- 发布日：仅从合法的 release_date 复制到 released_at；缺失或无效时保留已有数据，不从 price_at 或模型名字推断。
- 单价/长度改动才把 freshness.price_at 改成今天（或 --date）；只补发布日期时不动取价日期。能力/可靠性评测不由此脚本生成。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "deploy" / "dev" / "model-profiles.json"
SEED = REPO / "deploy" / "dev" / "registry-seed.json"
CATALOG_URL = "https://models.dev/api.json"
# Profile 的 vendor → models.dev 里第一方的供应商。
VENDORS = {"openai": "openai", "anthropic": "anthropic", "google": "google", "deepseek": "deepseek",
           # 2026-11-03 加：中转站也有这几家的新模型，作能力矩阵的候选（models.dev 里各家自己的供应商条目）。
           "xai": "xai", "moonshotai": "moonshotai", "alibaba": "alibaba", "zhipuai": "zhipuai"}
# 新加的强档 Profile 的 Tier（与已有的强档一致）。
TIER_PRESETS = {
    "low": {"name": "low", "effort": "", "thinking_budget": 0},
    "medium": {"name": "medium", "effort": "medium", "thinking_budget": 8192},
    "high": {"name": "high", "effort": "high", "thinking_budget": 32768},
    "default": {"name": "default", "effort": "", "thinking_budget": 0},
}


def load_catalog(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(urllib.request.Request(source, headers={"User-Agent": "agentruntime-sync"}), timeout=60) as resp:  # noqa: S310 — 固定的 models.dev 地址或人给的地址
            return json.loads(resp.read())
    return json.loads(Path(source).read_text("utf-8"))


def entry_for(catalog: dict[str, Any], vendor: str, model: str) -> dict[str, Any] | None:
    provider = VENDORS.get(vendor)
    if provider is None:
        return None
    return ((catalog.get(provider) or {}).get("models") or {}).get(model)


def _usd(value: Any) -> float:
    return round(float(value or 0), 6)


def price_gaps(klass: str, entry: dict[str, Any]) -> list[str]:
    """models.dev 的条目缺的单价（input、output、cache_read）；嵌入模型的 cache_read 不算缺。"""
    cost = entry.get("cost") or {}
    needed = ("input", "output") if klass == "embed" else ("input", "output", "cache_read")
    return [key for key in needed if cost.get(key) is None]


def price_errors(document: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    """要更新的 Profile 里，models.dev 缺单价的那些（每个一行说明）；models.dev 里找不到的不在这里（照旧不改并列出）。"""
    out = []
    for profile in document.get("profiles") or []:
        entry = entry_for(catalog, str(profile.get("vendor")), str(profile.get("model")))
        if entry is not None and (gaps := price_gaps(str(profile.get("class")), entry)):
            out.append(f"{profile.get('id')}：models.dev 没有 {'、'.join(gaps)} 的单价")
    return out


def synced(profile: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """按 models.dev 的条目改单价、长度与真实发布日期，其余不动。纯函数。缺单价时报错（调用方先用 price_gaps 查）。"""
    if gaps := price_gaps(str(profile.get("class")), entry):
        raise ValueError(f"{profile.get('id')}：models.dev 没有 {'、'.join(gaps)} 的单价")
    cost = entry.get("cost") or {}
    limit = entry.get("limit") or {}
    out = json.loads(json.dumps(profile))
    out["cost"] = {"in_per_m": _usd(cost.get("input")), "out_per_m": _usd(cost.get("output")), "cache_read_per_m": _usd(cost.get("cache_read"))}
    window = [int(v) for v in (limit.get("context"), limit.get("input")) if isinstance(v, int) and v > 0]
    constraints = dict(out.get("constraints") or {})
    if window:
        constraints["context_window"] = min(window)
    if profile.get("class") == "embed":
        constraints["max_output"] = 0
    elif isinstance(limit.get("output"), int) and limit["output"] > 0:
        constraints["max_output"] = int(limit["output"])
    out["constraints"] = constraints
    released = entry.get("release_date")
    if isinstance(released, str):
        try:
            parsed = dt.date.fromisoformat(released)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == released:  # 只接受完整 YYYY-MM-DD，不接受周日期或紧凑格式。
                out["released_at"] = released
    return out


def new_profile(vendor: str, model: str, klass: str, tiers: list[str], entry: dict[str, Any], today: str) -> dict[str, Any]:
    capabilities = ["generate"]
    if entry.get("structured_output"):
        capabilities.append("structured")
    if entry.get("tool_call"):
        capabilities.append("tools")
    capabilities.append("stream")
    if klass == "embed":
        capabilities = ["embed"]
    base = {
        "id": f"a6api/{model}@{today[:7]}",
        "vendor": vendor,
        "model": model,
        "class": klass,
        "tiers": [dict(TIER_PRESETS[t]) for t in tiers],
        "capabilities": capabilities,
        "cost": {},
        "constraints": {},
        "freshness": {"price_at": today},
    }
    return synced(base, entry)


def sync(document: dict[str, Any], catalog: dict[str, Any], today: str) -> tuple[dict[str, Any], list[str], list[str]]:
    """返回（改好的文件内容、改了的 Profile 及说明、找不到的 Profile）。缺单价的 Profile 原样留着（main 据 price_errors 报错、不写文件）。"""
    changed: list[str] = []
    missing: list[str] = []
    profiles = []
    for profile in document.get("profiles") or []:
        entry = entry_for(catalog, str(profile.get("vendor")), str(profile.get("model")))
        if entry is None:
            missing.append(str(profile.get("id")))
            profiles.append(profile)
            continue
        if price_gaps(str(profile.get("class")), entry):
            profiles.append(profile)
            continue
        updated = synced(profile, entry)
        if (updated["cost"], updated["constraints"]) != (profile.get("cost"), profile.get("constraints")):
            updated["freshness"] = {"price_at": today}
        if updated != profile:
            changed.append(f"{profile['id']}：{profile.get('cost')} {profile.get('constraints')} released_at={profile.get('released_at')} → {updated['cost']} {updated['constraints']} released_at={updated.get('released_at')}")
        profiles.append(updated)
    return {**document, "profiles": profiles}, changed, missing


def mirror_seed(seed: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """registry-seed.json 的 model_profile 换成与 Profile 文件同样的一组，按文件里的顺序放在原来第一个 model_profile 的位置。"""
    entries = list(seed.get("entries") or [])
    at = next((i for i, e in enumerate(entries) if e.get("kind") == "model_profile"), len(entries))
    others = [e for e in entries if e.get("kind") != "model_profile"]
    before = [e for e in entries[:at] if e.get("kind") != "model_profile"]
    after = others[len(before):]
    mirrored = [{"kind": "model_profile", "name": p["id"], "document": p} for p in profiles]
    return {**seed, "entries": [*before, *mirrored, *after]}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _inline(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_profiles(document: dict[str, Any]) -> str:
    """Profile 文件按原来的手写格式：每个 Profile 的键一行一个，值写在一行里；多个 Tier、多条能力分时一行一个。"""
    lines = ["{"]
    head = [f"  {_inline(k)}: {_inline(v)}," for k, v in document.items() if k != "profiles"]
    lines += head
    lines.append('  "profiles": [')
    blocks = []
    for profile in document.get("profiles") or []:
        fields = []
        for key, value in profile.items():
            if key in ("tiers", "capability_scores") and isinstance(value, list) and len(value) > 1:
                inner = ",\n".join(f"        {_inline(t)}" for t in value)
                fields.append(f"      {_inline(key)}: [\n{inner}\n      ]")
            else:
                fields.append(f"      {_inline(key)}: {_inline(value)}")
        blocks.append("    {\n" + ",\n".join(fields) + "\n    }")
    lines.append(",\n".join(blocks))
    lines += ["  ]", "}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default=CATALOG_URL, help="models.dev 的 api.json（地址或本地文件）")
    ap.add_argument("--profiles", type=Path, default=PROFILES)
    ap.add_argument("--seed", type=Path, default=SEED)
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="写进 freshness.price_at 的日期")
    ap.add_argument("--check", action="store_true", help="只比较，不改文件；有出入时退出码 1")
    ap.add_argument("--add", action="append", default=[], metavar="VENDOR/MODEL", help="加一个 Profile（要同时给 --class 与 --tiers）")
    ap.add_argument("--class", dest="klass", default="strong")
    ap.add_argument("--tiers", default="medium,high")
    args = ap.parse_args(argv)

    catalog = load_catalog(args.catalog)
    document = json.loads(args.profiles.read_text("utf-8"))
    for spec in args.add:
        vendor, _, model = spec.partition("/")
        entry = entry_for(catalog, vendor, model)
        if entry is None:
            print(f"models.dev 里没有 {spec}（第一方供应商 {VENDORS.get(vendor, '未知')}）", file=sys.stderr)
            return 2
        if any(p.get("vendor") == vendor and p.get("model") == model for p in document["profiles"]):
            print(f"已经有 {spec} 的 Profile，不重复加", file=sys.stderr)
            continue
        if gaps := price_gaps(args.klass, entry):
            print(f"models.dev 没有 {spec} 的 {'、'.join(gaps)} 单价：不加（不写 0）", file=sys.stderr)
            return 2
        document["profiles"].append(new_profile(vendor, model, args.klass, [t.strip() for t in args.tiers.split(",") if t.strip()], entry, args.date))
        print(f"加了 {document['profiles'][-1]['id']}")
    if errors := price_errors(document, catalog):
        for line in errors:
            print(f"缺单价：{line}", file=sys.stderr)
        print("有要更新的 Profile 缺单价：两个文件都没改（不写 0，也不跳过）；人核实价格后再跑", file=sys.stderr)
        return 1
    updated, changed, missing = sync(document, catalog, args.date)
    seed = json.loads(args.seed.read_text("utf-8"))
    seed_updated = mirror_seed(seed, updated["profiles"])
    for line in changed:
        print(f"改：{line}")
    for pid in missing:
        print(f"models.dev 里找不到，没改：{pid}")
    differs = dump_profiles(updated) != args.profiles.read_text("utf-8") or _dump(seed_updated) != args.seed.read_text("utf-8")
    if args.check:
        if differs:
            print("Profile 与 models.dev（或种子与 Profile 文件）不一致：跑一次 scripts/sync_model_profiles.py", file=sys.stderr)
            return 1
        return 0
    args.profiles.write_text(dump_profiles(updated), "utf-8", newline="\n")
    args.seed.write_text(_dump(seed_updated), "utf-8", newline="\n")
    print("没有改动" if not differs else f"写了 {args.profiles.relative_to(REPO) if args.profiles.is_relative_to(REPO) else args.profiles} 与种子")
    return 0


if __name__ == "__main__":
    sys.exit(main())
