"""核对 deploy/data/values 的键名与第三方 Chart 在固定版本上的默认 values.yaml 对得上（docs/plan/cloud-deploy.md D-03）。

    uv run --no-sync python scripts/check_chart_values.py

版本取自 deploy/cluster/versions.conf，Chart 的源码经 `gh api` 从 GitHub 读（要联网、gh 已登录；不下载 Chart 包）。
我们写了、Chart 的默认值里没有的键都列出来；原样交给应用自己配置的键（Qdrant 的 config、Grafana 的 grafana.ini 等）与
Kubernetes 的 resources 不查。升级 versions.conf 里的 Chart 版本之后跑一遍。有不对的键时退出码 1。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALUES = ROOT / "deploy" / "data" / "values"

# values 文件名 → (GitHub 仓库, 标签的模板, values.yaml 的路径, versions.conf 里的变量)
CHARTS = {
    "temporal": ("temporalio/helm-charts", "temporal-{v}", "charts/temporal/values.yaml", "TEMPORAL_CHART_VERSION"),
    "qdrant": ("qdrant/qdrant-helm", "qdrant-{v}", "charts/qdrant/values.yaml", "QDRANT_CHART_VERSION"),
    "redpanda": ("redpanda-data/redpanda-operator", "charts/redpanda/v{v}", "charts/redpanda/chart/values.yaml", "REDPANDA_CHART_VERSION"),
    "langfuse": ("langfuse/langfuse-k8s", "langfuse-{v}", "charts/langfuse/values.yaml", "LANGFUSE_CHART_VERSION"),
    "tempo": ("grafana-community/helm-charts", "tempo-{v}", "charts/tempo/values.yaml", "TEMPO_CHART_VERSION"),
    "grafana": ("grafana-community/helm-charts", "grafana-{v}", "charts/grafana/values.yaml", "GRAFANA_CHART_VERSION"),
}
# 顶层里原样交给应用的键；任何层级的 resources（Kubernetes 的资源请求与上限）也不查。
FREE_FORM = {"qdrant": {"config"}, "grafana": {"grafana.ini", "datasources"}, "redpanda": {"config"}}


def versions() -> dict[str, str]:
    out = {}
    for line in (ROOT / "deploy" / "cluster" / "versions.conf").read_text("utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def fetch(repo: str, ref: str, path: str) -> str:
    out = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{path}?ref={ref}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        raise SystemExit(f"读不到 {repo}@{ref}:{path}：{out.stderr.strip()}")
    return out.stdout


def unknown_keys(ours: object, theirs: object, path: str = "", free: frozenset[str] = frozenset()) -> list[str]:
    problems: list[str] = []
    if not isinstance(ours, dict):
        return problems
    for key, value in ours.items():
        here = f"{path}.{key}" if path else key
        if (not path and key in free) or key == "resources":
            continue
        if not isinstance(theirs, dict) or key not in theirs:
            problems.append(here)
            continue
        default = theirs[key]
        if isinstance(value, dict) and isinstance(default, dict) and default:
            problems += unknown_keys(value, default, here)
    return problems


def main() -> int:
    pinned = versions()
    failed = False
    for name, (repo, ref, path, var) in CHARTS.items():
        tag = ref.format(v=pinned[var])
        defaults = yaml.safe_load(fetch(repo, tag, path)) or {}
        ours = yaml.safe_load((VALUES / f"{name}.yaml").read_text("utf-8"))
        problems = unknown_keys(ours, defaults, free=frozenset(FREE_FORM.get(name, ())))
        failed |= bool(problems)
        print(f"{name}（{tag}）：{'对得上' if not problems else '这些键 Chart 里没有：' + '、'.join(problems)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
