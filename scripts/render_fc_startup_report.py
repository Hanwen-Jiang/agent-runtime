#!/usr/bin/env python3
"""Render the November 7 Firecracker report from immutable measurement evidence.

uv run --no-project --with matplotlib python scripts/render_fc_startup_report.py
Use --font /path/to/a/CJK-font.ttf when no supported Chinese font is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, ticker
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/plan/evidence/fc-pin-20261107"
OUTPUT = ROOT / "docs/plan/figures/fc-startup-20261107"
GROUPS = [
    ("cold", "first/cold", "冷启动", "#3469A5"),
    ("warm", "first/warm", "预热池", "#16836A"),
    ("snapshot", "first/snapshot", "内存快照", "#B96B29"),
    ("snapshot_repeat", "snapshot-repeat/snapshot", "内存快照复核", "#7E6F9C"),
]


def measurements():
    comparison = json.loads((EVIDENCE / "comparison.json").read_text())
    result = []
    for key, directory, label, color in GROUPS:
        relative = directory + "/samples.jsonl"
        raw = (EVIDENCE / relative).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == comparison["raw_file_sha256"][relative]
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        samples = [row for row in rows if not row.get("priming")]
        assert len(samples) == 20 and all(row["outcome"] == "completed" for row in samples)
        values = sorted(row["first_command_ms"] for row in samples)
        summary = next(iter(comparison["reports"][key]["summary"]["by_boot_source"].values()))
        # Match the experiment's nearest-rank definition, not an interpolated percentile.
        p50, p95 = (values[math.ceil(len(values) * q) - 1] for q in (.5, .95))
        assert math.isclose(p50, summary["p50_ms"]) and math.isclose(p95, summary["p95_ms"])
        result.append(dict(key=key, label=label, color=color, values=values, p50=p50, p95=p95))
    return result


def configure(font):
    if font:
        font_manager.fontManager.addfont(str(font))
        family = font_manager.FontProperties(fname=str(font)).get_name()
    else:
        family = None
        for candidate in ("PingFang SC", "Heiti SC", "Noto Sans CJK SC", "Microsoft YaHei", "WenQuanYi Zen Hei"):
            try:
                font_manager.findfont(candidate, fallback_to_default=False)
                family = candidate
                break
            except ValueError:
                pass
        if not family:
            raise SystemExit("No Chinese font found; supply --font.")
    plt.rcParams.update({"font.family": family, "font.size": 12, "axes.unicode_minus": False,
                         "text.color": "#233247", "axes.labelcolor": "#526175",
                         "xtick.color": "#526175", "ytick.color": "#233247",
                         "figure.facecolor": "white", "axes.facecolor": "white",
                         "savefig.facecolor": "white", "axes.spines.top": False,
                         "axes.spines.right": False, "axes.spines.left": False})


def finish(fig, name):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT / name, dpi=180, bbox_inches="tight", pad_inches=.22)
    plt.close(fig)


def warm_comparison(rows):
    cold, warm = rows[:2]
    decrease = (1 - warm["p95"] / cold["p95"]) * 100
    fig, ax = plt.subplots(figsize=(10.8, 5.2))
    fig.subplots_adjust(left=.15, right=.94, top=.72, bottom=.22)
    fig.text(.04, .94, "预热池明显缩短首条命令等待时间", fontsize=23, weight="bold")
    fig.text(.04, .855, f'P95：{cold["p95"]:,.2f} → {warm["p95"]:,.2f} ms   ·   时延下降 {decrease:.1f}%', fontsize=16, color=warm["color"])
    positions = [3.2, 2.2, .9, -.1]
    for row, ys in zip((cold, warm), (positions[:2], positions[2:])):
        for metric, y, alpha in zip(("p50", "p95"), ys, (.45, 1)):
            ax.barh(y, row[metric], height=.65, color=row["color"], alpha=alpha, zorder=3)
            ax.text(row[metric] + 20, y, f'{row[metric]:,.2f} ms', va="center", fontsize=13, weight="bold")
    ax.set_yticks(positions, ["冷启动 · P50", "冷启动 · P95", "预热池 · P50", "预热池 · P95"])
    ax.tick_params(axis="y", length=0, pad=12)
    ax.set_xlim(0, 1450)
    ax.set_ylim(-.8, 3.9)
    ax.set_xticks([0, 250, 500, 750, 1000, 1250])
    ax.grid(axis="x", color="#E5EAF0", linewidth=.8, zorder=0)
    ax.set_xlabel("创建请求发出 → 首条 shell 命令完成（毫秒，越低越好）", labelpad=10)
    ax.spines["bottom"].set_color("#D6DDE6")
    fig.text(.04, .055, "同规格 1 vCPU / 256 MiB 内存 / 256 MiB scratch；各 20 次，失败 0 次。", fontsize=11, color="#607085")
    finish(fig, "warm-pool-latency.png")


def distributions(rows):
    fig, ax = plt.subplots(figsize=(10.8, 6.0))
    fig.subplots_adjust(left=.17, right=.96, top=.73, bottom=.22)
    fig.text(.04, .94, "80 次启动测量：预热获益，快照恢复未获益", fontsize=22, weight="bold")
    fig.text(.04, .865, "每个圆点代表一次测量；四组各 20 次，未剔除慢样本。", fontsize=13, color="#526175")
    for y, row in zip(range(3, -1, -1), rows):
        # Deterministic vertical offsets separate overlapping points; they do not encode data.
        jitter = [((i * 7) % 11 - 5) * .035 for i in range(20)]
        ax.scatter(row["values"], [y + v for v in jitter], s=26, color=row["color"], alpha=.65, zorder=3, linewidths=0)
        ax.plot([row["p50"], row["p95"]], [y, y], color=row["color"], lw=2, zorder=4)
        ax.scatter([row["p50"]], [y], color="#192B40", marker="|", s=300, zorder=5)
        ax.scatter([row["p95"]], [y], color=row["color"], marker="D", s=55, edgecolors="white", zorder=5)
        ax.text(.01, y + .30, f'P50 {row["p50"]:,.2f} ms    P95 {row["p95"]:,.2f} ms',
                transform=ax.get_yaxis_transform(), fontsize=10.5, color=row["color"])
    ax.set_xscale("log")
    ax.set_xlim(10, 20000)
    ax.set_ylim(-.55, 3.65)
    ax.set_yticks([3, 2, 1, 0], [r["label"] for r in rows])
    ax.tick_params(axis="y", length=0, pad=12)
    ax.set_xticks([10, 100, 1000, 10000], ["10", "100", "1,000", "10,000"])
    ax.xaxis.set_minor_formatter(ticker.NullFormatter())
    ax.grid(axis="x", which="major", color="#DDE5ED", zorder=0)
    ax.spines["bottom"].set_color("#D6DDE6")
    ax.set_xlabel("创建请求 → 首条命令完成（毫秒，对数刻度；每格相差 10 倍）", labelpad=10)
    ax.legend(handles=[Line2D([], [], marker="|", markersize=14, linestyle="none", color="#192B40", label="P50"),
                       Line2D([], [], marker="D", markersize=6, linestyle="none", color="#526175", label="P95")],
              loc="upper right", ncol=2, frameon=False)
    fig.text(.04, .06, "快照捕获预填充不计入恢复分位数；共享宿主有背景负载，结果仅代表本实现与规格。", fontsize=11, color="#607085")
    finish(fig, "startup-distribution.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    configure(args.font)
    rows = measurements()
    warm_comparison(rows)
    distributions(rows)
    print(json.dumps({"output": str(OUTPUT), "groups": [{k: r[k] for k in ("key", "p50", "p95")} for r in rows]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
