"""能力矩阵的命令行（docs/plan/capability-matrix-evals.md 第 8 节）：

    uv run --package agentruntime-learning python -m agentruntime_learning.capability estimate --plan <计划.json> [--out <目录>]
    … capability run --plan <计划.json> --out <目录> [--budget-usd X] [--concurrency N]   # 花钱；同一个 --out 再跑是续跑
    … capability score --out <目录>                                                      # 重新折算，写 scores.json
    … capability publish --out <目录> [--dry-run] [--date YYYY-MM-DD]                     # 写进 Profile 与 Registry 种子

run 先打印花费估算再跑；Model Gateway 取 AR_EVAL_GATEWAY_FACTORY（同 evals 的 worker_subject）。设了
OTEL_EXPORTER_OTLP_ENDPOINT 时边跑边发 runtime.eval.*（suite capability）。计划的格式见 capability.matrix。
publish 只写 scores.json 里可发布的分数（区间够窄、污染不超过 5%、不是淘汰赛里不领先的），补丁另存 capability-patch.json；
改完的两个文件要人审，再按现有流程经 Registry 发布。

退出码：0 完成；2 参数、计划或文件有问题。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from agentruntime_learning.capability import matrix, publish


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.capability", description="能力矩阵：给 Model Profile 的 capability_scores 出有来源的分数")
    commands = parser.add_subparsers(dest="command", required=True)
    est = commands.add_parser("estimate", help="按计划估算花费（做过的按实际花费）")
    est.add_argument("--plan", type=Path, required=True)
    est.add_argument("--out", type=Path, default=None, help="给了就按里面已跑的结果算剩下的")
    est.add_argument("--profiles", type=Path, default=matrix.DEFAULT_PROFILES)
    run = commands.add_parser("run", help="跑计划（花钱）；同一个 --out 再跑是续跑")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--profiles", type=Path, default=matrix.DEFAULT_PROFILES)
    run.add_argument("--budget-usd", type=float, default=None, help="总花费到了就不再开始新的题（默认不设上限）")
    run.add_argument("--concurrency", type=int, default=None, help="同时跑几道题（默认用计划里的）")
    sc = commands.add_parser("score", help="按结果重新折算分数，写 scores.json")
    sc.add_argument("--out", type=Path, required=True)
    sc.add_argument("--plan", type=Path, default=None, help="默认用输出目录里的 plan.json")
    pub = commands.add_parser("publish", help="把可发布的分数写进 deploy/dev/model-profiles.json 与 registry-seed.json")
    pub.add_argument("--out", type=Path, required=True)
    pub.add_argument("--profiles", type=Path, default=publish.PROFILES)
    pub.add_argument("--seed", type=Path, default=publish.SEED)
    pub.add_argument("--date", default=None, help="判断过期（90 天）用的日期，默认今天（UTC）")
    pub.add_argument("--dry-run", action="store_true", help="只打印要改的，不写文件")
    return parser


def _load(plan_path: Path, profiles_path: Path) -> tuple[matrix.MatrixPlan, dict[str, Any], dict[tuple[str, str], Any]]:
    plan = matrix.load_plan(plan_path)
    profiles = matrix.load_profiles(profiles_path)
    matrix.check_profiles(plan, profiles)
    return plan, profiles, matrix.load_evals(plan)


def _estimate(args: argparse.Namespace) -> int:
    plan, profiles, evals = _load(args.plan, args.profiles)
    rows = matrix.read_results(args.out) if args.out else []
    print(matrix.estimate(plan, profiles, evals, rows).summary())
    return 0


def _run(args: argparse.Namespace) -> int:
    from agentruntime_learning.evals.__main__ import _with_telemetry
    from agentruntime_learning.evals.judging import gateway_from_env

    plan, profiles, evals = _load(args.plan, args.profiles)
    gateway = gateway_from_env()

    async def go(telemetry: Any) -> dict[str, Any]:
        runner = matrix.MatrixRunner(plan, gateway, args.out, profiles=profiles, evals=evals, telemetry=telemetry, budget_usd=args.budget_usd, concurrency=args.concurrency)
        print(runner.estimate().summary(), flush=True)
        report = await runner.run()
        print(f"这次跑了 {runner.new_runs} 次，累计花费 ${runner.spent:.4f}" + (f"；预算用完，{runner.skipped_for_budget} 次没跑" if runner.skipped_for_budget else ""))
        return report

    report = asyncio.run(_with_telemetry(go))
    print(matrix.scores_summary(report))
    print(f"结果：{args.out / matrix.RESULTS}；分数：{args.out / matrix.SCORES}")
    return 0


def _score(args: argparse.Namespace) -> int:
    plan = matrix.load_plan(args.plan or args.out / matrix.PLAN_COPY)
    report = matrix.score_results(plan, matrix.read_results(args.out))
    path = matrix.write_scores(args.out, report)
    print(matrix.scores_summary(report))
    print(f"分数：{path}")
    return 0


def _publish(args: argparse.Namespace) -> int:
    report = json.loads((args.out / matrix.SCORES).read_text("utf-8"))
    today = dt.date.fromisoformat(args.date) if args.date else None
    result = publish.publish(report, profiles_path=args.profiles, seed_path=args.seed, today=today, dry_run=args.dry_run)
    (args.out / publish.PATCH).write_text(json.dumps(result["patch"], ensure_ascii=False, indent=2), "utf-8")
    for note in result["notes"]:
        print(note)
    if not result["changed"]:
        print("没有改动")
    elif args.dry_run:
        print("（--dry-run：没写文件）")
    else:
        print(f"写了 {args.profiles} 与 {args.seed}；人审后按现有流程经 Registry 发布")
    print(f"补丁：{args.out / publish.PATCH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    handlers = {"estimate": _estimate, "run": _run, "score": _score, "publish": _publish}
    try:
        return handlers[args.command](args)
    except (matrix.PlanError, matrix.ResumeMismatch, publish.PublishError, ValueError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

