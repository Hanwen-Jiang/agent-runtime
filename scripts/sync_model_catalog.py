"""模型目录自动同步（docs/plan/model-catalog-sync.md，F1；N-07 的延续）：按中转站的 /models 与 models.dev 维护 Model Profile 与白名单。

只用标准库，取价与写 Profile 文件复用 scripts/sync_model_profiles.py。每次都从头算，不记状态：

  uv run --no-sync python scripts/sync_model_catalog.py --report <目录>     # 拉中转站与 models.dev，改 Profile、种子、默认策略与 values.yaml
  … --check                                                                # 只比较，有出入时退出码 1（不改文件）
  … --relay-models <models.json>                                           # 用本地的 /models 结果（离线、测试）
  … --policies <租户策略文件> …                                            # 另外重算这些文件里的白名单（例如线上 ConfigMap 的副本）

规则（deploy/dev/model-catalog-rules.json）：
- 产品线：模型名去掉版本号（gpt-6.1-sol → gpt-*-sol，claude-opus-5-5 → claude-opus-*），带上 vendor；aliases 把改了名的线接起来。
- 候选：中转站上有、models.dev 里有 vendor 自己（一方）的条目且只输出文字，不是快照、预览或 ignore 里的变体；没有一方价格的要 price_overrides。
- 一条线只有当前版本（版本号最新，相同时看发布日）可用，其余写 availability: false；中转站上没有了的也是；disabled 里的总是。
- 在跟的线（已有 Profile 的线与 track 里的线）出了新版本就加 Profile：档位与 Tier 继承同一条线上的旧版本（track 的新线按规则），
  能力、单价、长度、发布日取 models.dev；capability_scores、reliability 不继承（属于具体模型的测量）。
- 白名单按 allowlists 重算：default 是可用的 small、mid、embed 与单价不超过上限的 strong；租户是全部可用的（exclude_vendors 去掉几家），
  track 里标了 candidate 的线只给 candidates 为 true 的租户。其余字段原样，文件里没有的租户不新建。
- 拉不到 /models，或现有可用 Profile 里一半以上不在列表上：不改文件，退出码非 0。中转站的密钥不进输出、报告与错误信息。
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "deploy" / "dev" / "model-catalog-rules.json"
POLICIES = REPO / "deploy" / "dev" / "default-tenant-policies.json"
VALUES = REPO / "deploy" / "helm" / "agent-runtime" / "values.yaml"
PROVIDER_ENV = Path.home() / ".agentruntime" / "model-provider.env"


def _sibling(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profiles_sync = _sibling("sync_model_profiles")

_TOKEN = re.compile(r"^([a-z]*)(\d+(?:\.\d+)*)([a-z]*)$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)*$")
_SNAPSHOT = re.compile(r"(-\d{4}|-\d{8}|-expires-on-\d+)$")
_PREVIEW = re.compile(r"(^|-)(preview|latest)(-|$)")


class RelayListSuspicious(Exception):
    """中转站的列表看着不对（空的，或现有可用的 Profile 一半以上不在上面）：这一轮什么都不改。"""


def line_of(name: str) -> tuple[str, tuple[int, ...]] | None:
    """模型名 → （去掉版本号的线、版本号）。版本号是第一个带数字的段；Claude 那样用 - 分开的连续数字段算一个版本。"""
    tokens = name.lower().split("-")
    for i, token in enumerate(tokens):
        match = _TOKEN.match(token)
        if not match:
            continue
        prefix, digits, suffix = match.groups()
        parts, end = [digits], i + 1
        if not prefix and not suffix:
            while end < len(tokens) and _NUMBER.match(tokens[end]):
                parts.append(tokens[end])
                end += 1
        version = tuple(int(x) for part in parts for x in part.split("."))
        return "-".join([*tokens[:i], f"{prefix}*{suffix}", *tokens[end:]]), version
    return None


def is_snapshot(name: str) -> bool:
    """带日期或到期时间的快照（-1017、-20251001、-expires-on-0910）：只认不带后缀的名字。"""
    return bool(_SNAPSHOT.search(name.lower()))


def vendor_of(name: str, rules: dict[str, Any]) -> str | None:
    lowered, best = name.lower(), (0, None)
    for vendor, spec in (rules.get("vendors") or {}).items():
        for prefix in spec.get("prefixes") or []:
            if lowered.startswith(prefix.lower()) and len(prefix) > best[0]:
                best = (len(prefix), vendor)
    return best[1]


def _resolve(line: str, aliases: dict[str, str]) -> str:
    seen = {line}
    while line in aliases and aliases[line] not in seen:
        line = aliases[line]
        seen.add(line)
    return line


def _line(vendor: str, model: str, rules: dict[str, Any]) -> str | None:
    parsed = line_of(model)
    return None if parsed is None else _resolve(f"{vendor}:{parsed[0]}", rules.get("aliases") or {})


def _date(value: Any) -> str:
    if isinstance(value, str):
        try:
            if dt.date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    return ""


def _text_only(entry: dict[str, Any]) -> bool:
    output = (entry.get("modalities") or {}).get("output")
    return output is None or list(output) == ["text"]


def _cost(entry: dict[str, Any]) -> tuple[float, float]:
    cost = entry.get("cost") or {}
    return float(cost.get("input") or 0), float(cost.get("output") or 0)


@dataclass
class _Candidate:
    model: str
    vendor: str
    line: str
    version: tuple[int, ...]
    date: str
    entry: dict[str, Any] | None  # 一方的 models.dev 条目，或按 price_overrides 拼出的条目；None 表示没有价格
    hint: dict[str, float] | None = None


@dataclass
class Plan:
    document: dict[str, Any]
    added: list[str] = field(default_factory=list)
    retired: dict[str, str] = field(default_factory=dict)
    restored: list[str] = field(default_factory=list)
    unpriced: list[dict[str, Any]] = field(default_factory=list)
    new_lines: list[dict[str, Any]] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)


def _candidates(relay: list[str], catalog: dict[str, Any], rules: dict[str, Any]) -> list[_Candidate]:
    ignore = [re.compile(p) for p in rules.get("ignore") or []]
    overrides = rules.get("price_overrides") or {}
    out = []
    for model in sorted(set(relay)):
        lowered = model.lower()
        if is_snapshot(model) or _PREVIEW.search(lowered) or any(p.search(lowered) for p in ignore):
            continue
        vendor = vendor_of(model, rules)
        if vendor is None or line_of(model) is None:
            continue
        provider = rules["vendors"][vendor].get("provider", vendor)
        entry = ((catalog.get(provider) or {}).get("models") or {}).get(model)
        others = [m for p, data in sorted(catalog.items()) if p != provider
                  for m in [((data or {}).get("models") or {}).get(model)] if m and _text_only(m) and any(_cost(m))]
        if entry is not None and not _text_only(entry):
            continue
        date = _date((entry or {}).get("release_date")) or _date((overrides.get(model) or {}).get("released_at")) \
            or min((d for d in (_date(m.get("release_date")) for m in others) if d), default="")
        hint = None
        if others:
            hint = {"in_per_m": round(statistics.median(_cost(m)[0] for m in others), 6),
                    "out_per_m": round(statistics.median(_cost(m)[1] for m in others), 6)}
        if entry is None and model in overrides:
            o = overrides[model]
            base = others[0] if others else {}
            entry = {**base, "release_date": date,
                     "cost": {"input": o.get("in_per_m"), "output": o.get("out_per_m"), "cache_read": o.get("cache_read_per_m")}}
        line = _line(vendor, model, rules)
        assert line is not None
        out.append(_Candidate(model, vendor, line, line_of(model)[1], date, entry, hint))  # type: ignore[index]
    return out


def _guess_class(model: str, entry: dict[str, Any]) -> str:
    lowered = model.lower()
    if any(w in lowered for w in ("pro", "max", "opus", "ultra", "astra")):
        return "strong"
    if any(w in lowered for w in ("lite", "nano", "mini", "luna", "haiku")):
        return "small"
    out = _cost(entry)[1]
    return "small" if out <= 1 else "mid" if out <= 5 else "strong"


def plan(document: dict[str, Any], relay: list[str], catalog: dict[str, Any], rules: dict[str, Any], today: str) -> Plan:
    """算出新的 Profile 文件与报告的各项。纯函数（不读写文件）。"""
    relay_set = set(relay)
    profiles = [copy.deepcopy(p) for p in document.get("profiles") or []]
    available_before = [p for p in profiles if p.get("availability", True)]
    missing = [p for p in available_before if p.get("model") not in relay_set]
    if not relay_set or (available_before and 2 * len(missing) > len(available_before)):
        raise RelayListSuspicious(f"中转站的列表里有 {len(relay_set)} 个模型，现有可用的 {len(available_before)} 个 Profile 里 {len(missing)} 个不在上面")

    aliases = rules.get("aliases") or {}
    track = {_resolve(t["line"], aliases): t for t in rules.get("track") or []}
    lines = {p["id"]: _line(str(p.get("vendor")), str(p.get("model")), rules) for p in profiles}
    tracked = {ln for ln in lines.values() if ln} | set(track)
    candidates = _candidates(relay, catalog, rules)
    result = Plan(document={})

    # 每条在跟的线的当前版本：中转站上有、有价格的候选，加上中转站上有的已有 Profile。
    current: dict[str, str] = {}
    for line in sorted(tracked):
        members = {c.model: (c.version, c.date) for c in candidates if c.line == line and c.entry is not None}
        for p in profiles:
            if lines[p["id"]] == line and p.get("model") in relay_set and p["model"] not in members:
                members[p["model"]] = (line_of(p["model"])[1], _date(p.get("released_at")))  # type: ignore[index]
        if not members:
            continue
        by_version = max(members, key=lambda m: (members[m], m))
        by_date = max(members, key=lambda m: (members[m][1], members[m][0], m))
        current[line] = by_version
        if by_date != by_version and members[by_date][1] and members[by_version][1] and members[by_date][1] > members[by_version][1]:
            result.disagreements.append({"line": line, "by_version": by_version, "by_date": by_date})
        newest = members[by_version][0]
        for c in candidates:
            if c.line == line and c.entry is None and c.version > newest:
                result.unpriced.append({"model": c.model, "line": line, "hint": c.hint})

    # 新版本：当前版本还没有 Profile 的，加上。
    known = {(p.get("vendor"), p.get("model")) for p in profiles}
    by_model = {c.model: c for c in candidates}
    additions = []
    for line, model in current.items():
        c = by_model.get(model)
        if c is None or c.entry is None or (c.vendor, model) in known:
            continue
        olds = [p for p in profiles if lines[p["id"]] == line]
        if olds:
            pred = max(olds, key=lambda p: (line_of(p["model"])[1], _date(p.get("released_at"))))  # type: ignore[index]
            klass, tiers = pred["class"], copy.deepcopy(pred["tiers"])
        else:
            klass = track[line]["class"]
            tiers = [dict(profiles_sync.TIER_PRESETS[t]) for t in track[line]["tiers"]]
        new = profiles_sync.new_profile(c.vendor, model, klass, [], c.entry, today)
        new["id"] = f"a6api/{model.lower()}@{today[:7]}"  # schema 的 profile_id 只许小写；model 保持中转站上的写法
        new["tiers"] = tiers
        additions.append(new)
    for new in sorted(additions, key=lambda p: p["model"]):
        profiles.append(new)
        lines[new["id"]] = _line(new["vendor"], new["model"], rules)
        result.added.append(new["id"])

    # 单价与长度照 models.dev 的一方条目同步（与 sync_model_profiles.py 同样的规则）。
    overrides = rules.get("price_overrides") or {}
    for i, p in enumerate(profiles):
        if p["id"] in result.added:
            continue
        provider = (rules.get("vendors") or {}).get(str(p.get("vendor")), {}).get("provider")
        entry = ((catalog.get(provider) or {}).get("models") or {}).get(p.get("model")) if provider else None
        if entry is None and p.get("model") in by_model and p["model"] in overrides:
            entry = by_model[p["model"]].entry
        if entry is None:
            continue
        updated = profiles_sync.synced(p, entry)
        if (updated["cost"], updated["constraints"]) != (p.get("cost"), p.get("constraints")):
            updated["freshness"] = {"price_at": today}
        profiles[i] = updated

    # 可用性：每次从头算。
    disabled = set(rules.get("disabled") or [])
    was_off = {p["id"] for p in profiles if p.get("availability") is False}
    for p in profiles:
        line = lines[p["id"]]
        reason = None
        if p["id"] in disabled:
            reason = "规则里停用（disabled）"
        elif p.get("model") not in relay_set:
            reason = "中转站上没有了"
        elif line in current and current[line] != p["model"]:
            reason = f"同一条线（{line}）有更新的 {current[line]}"
        if reason is None:
            p.pop("availability", None)
            if p["id"] in was_off:
                result.restored.append(p["id"])
        else:
            p["availability"] = False
            if p["id"] not in was_off:
                result.retired[p["id"]] = reason

    # 新线：不在跟的、有一方价格的，只进报告。
    oldest = (dt.date.fromisoformat(today) - dt.timedelta(days=int(rules.get("new_line_max_age_days") or 3650))).isoformat()
    fresh: dict[str, _Candidate] = {}
    for c in candidates:
        if c.line in tracked or c.entry is None or (c.date and c.date < oldest) or not c.date:
            continue
        if c.line not in fresh or (c.version, c.date) > (fresh[c.line].version, fresh[c.line].date):
            fresh[c.line] = c
    result.new_lines = [{"line": c.line, "model": c.model, "released_at": c.date, "class_guess": _guess_class(c.model, c.entry or {}),
                         "cost": {"in_per_m": _cost(c.entry or {})[0], "out_per_m": _cost(c.entry or {})[1]}}
                        for c in sorted(fresh.values(), key=lambda c: (c.date, c.line), reverse=True)]
    result.document = {**document, "profiles": profiles}
    return result


def allowlists(policies: dict[str, Any], document: dict[str, Any], rules: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按规则重算租户策略文件里的 model_allowlist；返回（新的内容、说明）。顺序跟 Profile 文件一致。"""
    aliases = rules.get("aliases") or {}
    candidate_lines = {_resolve(t["line"], aliases) for t in rules.get("track") or [] if t.get("candidate")}
    spec = rules.get("allowlists") or {}
    out = copy.deepcopy(policies)
    notes: list[str] = []
    available = [p for p in document.get("profiles") or [] if p.get("availability", True)]

    def ids(*, candidates: bool, exclude: list[str], keep: Any) -> list[str]:
        return [p["id"] for p in available
                if p.get("vendor") not in exclude and keep(p)
                and (candidates or _line(str(p.get("vendor")), str(p.get("model")), rules) not in candidate_lines)]

    if "default" in spec and isinstance(out.get("default"), dict):
        rule = spec["default"]
        classes = set(rule.get("classes") or [])
        cap = rule.get("strong_max") or {}

        def free(p: dict[str, Any]) -> bool:
            if p.get("class") in classes:
                return True
            cost = p.get("cost") or {}
            return p.get("class") == "strong" and bool(cap) and float(cost.get("in_per_m", 0)) <= float(cap.get("in_per_m", 0)) \
                and float(cost.get("out_per_m", 0)) <= float(cap.get("out_per_m", 0))

        out["default"]["model_allowlist"] = ids(candidates=bool(rule.get("candidates")), exclude=list(rule.get("exclude_vendors") or []), keep=free)
    tenants = out.get("tenants") if isinstance(out.get("tenants"), dict) else {}
    for name, rule in (spec.get("tenants") or {}).items():
        if name not in tenants:
            notes.append(f"规则里有租户 {name}，这个文件里没有，没有新建")
            continue
        tenants[name]["model_allowlist"] = ids(candidates=bool(rule.get("candidates")), exclude=list(rule.get("exclude_vendors") or []), keep=lambda _p: True)
    return out, notes


_ALLOWLIST_BLOCK = re.compile(r"^(?P<head>(?P<indent>[ \t]*)modelAllowlist:[ \t]*\n)(?P<items>(?:[ \t]*-[ \t].*\n)*)", re.MULTILINE)


def rewrite_values(text: str, allowlist: list[str]) -> str:
    """values.yaml 里 defaultTenantPolicies.modelAllowlist 那一段换成新的列表，其余一个字节不动。"""
    match = _ALLOWLIST_BLOCK.search(text, text.find("defaultTenantPolicies:"))
    if match is None:
        raise ValueError("values.yaml 里找不到 defaultTenantPolicies.modelAllowlist")
    items = "".join(f"{match['indent']}  - {i}\n" for i in allowlist)
    return text[:match.start("items")] + items + text[match.end("items"):]


def _env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def relay_models(base: str, key: str) -> list[str]:
    request = urllib.request.Request(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}", "User-Agent": "agentruntime-catalog-sync"})
    with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310 — 人给的中转站地址
        return [str(m["id"]) for m in json.loads(resp.read())["data"]]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _report(result: Plan, diffs: dict[str, dict[str, dict[str, list[str]]]], notes: list[str], today: str) -> tuple[str, str]:
    data = {"date": today, "added": result.added, "retired": result.retired, "restored": result.restored, "unpriced": result.unpriced,
            "new_lines": result.new_lines, "disagreements": result.disagreements, "allowlists": diffs, "notes": notes}
    md = [f"# 模型目录同步 {today}", ""]

    def section(title: str, rows: list[str]) -> None:
        md.extend([f"## {title}", "", *(rows or ["（没有）"]), ""])

    section("加了", [f"- `{i}`" for i in result.added])
    section("停用", [f"- `{i}`：{why}" for i, why in result.retired.items()])
    section("恢复", [f"- `{i}`" for i in result.restored])
    section("有更新的版本但没有一方价格（要用就在规则文件的 price_overrides 里给价格）",
            [f"- `{u['model']}`（{u['line']}）" + (f"：第三方价格中位数 输入 {u['hint']['in_per_m']}、输出 {u['hint']['out_per_m']}" if u["hint"] else "") for u in result.unpriced])
    section("新线（只列出来；要跟就加进规则文件的 track）",
            [f"- `{n['model']}`（{n['line']}，{n['released_at']}，猜 {n['class_guess']}，输入 {n['cost']['in_per_m']}、输出 {n['cost']['out_per_m']}）" for n in result.new_lines])
    section("版本号与发布日先后不一致（按版本号）", [f"- {d['line']}：版本号最新 `{d['by_version']}`，发布日最新 `{d['by_date']}`" for d in result.disagreements])
    rows = []
    for path, names in diffs.items():
        for name, change in names.items():
            if change["added"] or change["removed"]:
                rows.append(f"- {path} · {name}：加 {', '.join(change['added']) or '无'}；去掉 {', '.join(change['removed']) or '无'}")
    section("白名单", rows)
    if notes:
        section("说明", [f"- {n}" for n in notes])
    return "\n".join(md), _dump(data)


def _allowlist_of(policies: dict[str, Any]) -> dict[str, list[str]]:
    out = {}
    if isinstance(policies.get("default"), dict):
        out["default"] = list(policies["default"].get("model_allowlist") or [])
    for name, tenant in (policies.get("tenants") or {}).items():
        if isinstance(tenant, dict) and "model_allowlist" in tenant:
            out[name] = list(tenant["model_allowlist"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--relay", default="", help="中转站的基地址（含 /v1）；默认取 AR_MODEL_PROVIDER_BASE_URL 或 --provider-env 里的")
    ap.add_argument("--relay-models", type=Path, help="用本地的 /models 结果，不联网")
    ap.add_argument("--provider-env", type=Path, default=PROVIDER_ENV, help="中转站地址与密钥的文件（环境变量优先）")
    ap.add_argument("--catalog", default=profiles_sync.CATALOG_URL, help="models.dev 的 api.json（地址或本地文件）")
    ap.add_argument("--rules", type=Path, default=RULES)
    ap.add_argument("--profiles", type=Path, default=profiles_sync.PROFILES)
    ap.add_argument("--seed", type=Path, default=profiles_sync.SEED)
    ap.add_argument("--policies", type=Path, action="append", help="租户策略文件，可以给几个；第一个的 default 也写进 values.yaml（默认仓库的默认策略）")
    ap.add_argument("--values", type=Path, default=VALUES, help="Chart 的 values.yaml（defaultTenantPolicies.modelAllowlist）")
    ap.add_argument("--report", type=Path, help="报告写到这个目录（Markdown 与 JSON）")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--check", action="store_true", help="只比较，不改文件；有出入时退出码 1")
    args = ap.parse_args(argv)
    policy_paths = args.policies or [POLICIES]

    try:
        if args.relay_models:
            relay = [str(m["id"]) for m in json.loads(args.relay_models.read_text("utf-8"))["data"]]
        else:
            env = _env_file(args.provider_env)
            base = args.relay or os.environ.get("AR_MODEL_PROVIDER_BASE_URL") or env.get("AR_MODEL_PROVIDER_BASE_URL", "")
            key = os.environ.get("AR_MODEL_PROVIDER_API_KEY") or env.get("AR_MODEL_PROVIDER_API_KEY", "")
            if not base or not key:
                print("没有中转站的地址或密钥（AR_MODEL_PROVIDER_BASE_URL、AR_MODEL_PROVIDER_API_KEY）", file=sys.stderr)
                return 2
            relay = relay_models(base, key)
    except urllib.error.HTTPError as err:
        print(f"拉不到中转站的模型列表：HTTP {err.code}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f"拉不到中转站的模型列表：{type(err).__name__}", file=sys.stderr)
        return 2

    catalog = profiles_sync.load_catalog(args.catalog)
    rules = json.loads(args.rules.read_text("utf-8"))
    document = json.loads(args.profiles.read_text("utf-8"))
    try:
        result = plan(document, relay, catalog, rules, args.date)
    except RelayListSuspicious as err:
        print(f"{err}：这一轮不改任何文件", file=sys.stderr)
        return 3

    outputs: dict[Path, str] = {args.profiles: profiles_sync.dump_profiles(result.document)}
    seed = json.loads(args.seed.read_text("utf-8"))
    outputs[args.seed] = _dump(profiles_sync.mirror_seed(seed, result.document["profiles"]))
    diffs: dict[str, dict[str, dict[str, list[str]]]] = {}
    notes: list[str] = []
    first_default: list[str] | None = None
    for path in policy_paths:
        before = json.loads(path.read_text("utf-8"))
        after, file_notes = allowlists(before, result.document, rules)
        notes += [f"{path.name}：{n}" for n in file_notes]
        old, new = _allowlist_of(before), _allowlist_of(after)
        diffs[path.name] = {name: {"added": [i for i in ids if i not in old.get(name, [])], "removed": [i for i in old.get(name, []) if i not in ids]}
                            for name, ids in new.items()}
        outputs[path] = _dump(after)
        if first_default is None:
            first_default = new.get("default")
    if args.values and first_default is not None:
        outputs[args.values] = rewrite_values(args.values.read_text("utf-8"), first_default)

    changed = [path for path, text in outputs.items() if path.read_text("utf-8") != text]
    for pid in result.added:
        print(f"加：{pid}")
    for pid, why in result.retired.items():
        print(f"停用：{pid}（{why}）")
    for pid in result.restored:
        print(f"恢复：{pid}")
    for item in result.unpriced:
        print(f"没有一方价格，没加：{item['model']}（{item['line']}）")
    if args.check:
        if changed:
            print("与中转站、models.dev 或规则不一致：跑一次 scripts/sync_model_catalog.py（" + "、".join(p.name for p in changed) + "）", file=sys.stderr)
            return 1
        return 0
    for path in changed:
        # 换行符保持原样（values.yaml 是 CRLF）。
        path.write_text(outputs[path], "utf-8", newline="\r\n" if b"\r\n" in path.read_bytes() else "\n")
    if args.report:
        args.report.mkdir(parents=True, exist_ok=True)
        md, js = _report(result, diffs, notes, args.date)
        (args.report / f"model-catalog-{args.date}.md").write_text(md, "utf-8", newline="\n")
        (args.report / f"model-catalog-{args.date}.json").write_text(js, "utf-8", newline="\n")
    print("没有改动" if not changed else "改了：" + "、".join(p.name for p in changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
