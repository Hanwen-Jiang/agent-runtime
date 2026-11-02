"""python -m agentruntime_learning.benchmarks swebench {fetch,run,grade,report}（TMP 计划 eval-observability-and-runtime-fixes 11.10）。

- fetch：从 Hugging Face 取题目（默认 SWE-bench/SWE-bench_Verified 的 test，500 题），写成 JSONL。
- run：经 Task API 跑选中的题（--limit 与 --seed 抽，或 --ids 文件按行给）；Task API 的令牌取环境变量 AR_TASK_API_TOKEN，
  不写进输出目录。同一个 --out 再跑是续跑。花钱：--budget-usd 是总预算，--task-usd-limit 是单题上限。
- grade：调官方判分工具（--python 指一个装了 swebench 的 Python；要 Docker，或 --modal 在 Modal 上跑）。
- report：写 report.json 与 report.md（判过分时并进判分结果）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from agentruntime_learning.benchmarks import suite
from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.client import ApiError, RuntimeClient

TOKEN_ENV = "AR_TASK_API_TOKEN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.benchmarks", description="用 Runtime 跑主流 Benchmark（TMP 计划第 10 节）")
    benches = parser.add_subparsers(dest="benchmark", required=True)
    swe = benches.add_parser("swebench", help="SWE-bench（Verified）")
    sub = swe.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="从 Hugging Face 取题目，写成 JSONL")
    fetch.add_argument("--out", type=Path, required=True)
    fetch.add_argument("--dataset", default=sb.DEFAULT_DATASET)
    fetch.add_argument("--split", default="test")

    run = sub.add_parser("run", help="经 Task API 跑选中的题，写 predictions")
    run.add_argument("--instances", type=Path, required=True, help="题目文件（fetch 写的 .jsonl，或 .json 数组）")
    run.add_argument("--out", type=Path, required=True, help="输出目录；同一个目录再跑是续跑")
    pick = run.add_mutually_exclusive_group(required=True)
    pick.add_argument("--limit", type=int, help="按种子抽这么多题")
    pick.add_argument("--ids", type=Path, help="按行给 instance_id 的文件")
    run.add_argument("--stratify", action="store_true", help="与 --limit 一起用：按仓库与难度分层抽题")
    run.add_argument("--exclude-repo", action="append", default=[], help="分层抽题时不选这个仓库，可以给多个")
    run.add_argument("--exclude-ids", type=Path, help="分层抽题时不选这些题（按行给 instance_id 的文件）：验证用的题不和调优用的题重复")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--run-id", help="默认取输出目录的名字")
    run.add_argument("--task-api", required=True, help="Task API 的地址（与 Workspace API 同一个），例如 http://127.0.0.1:8080")
    run.add_argument("--budget-usd", type=float, required=True, help="总预算（美元）")
    run.add_argument("--task-usd-limit", type=float, required=True, help="单题上限（美元）；租户策略的 budget.usd 应不高于它")
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--task-timeout-min", type=float, default=60.0)
    run.add_argument("--poll-s", type=float, default=30.0, help="跟事件时至多隔这么久查一次花费")
    run.add_argument("--model-name", default="agent-runtime", help="predictions 的 model_name_or_path")
    run.add_argument("--hints", action="store_true", help="消息里带上 hints_text（榜单默认不带）")
    run.add_argument("--environment", choices=("official", "none"), default="official", help="official：用题目的官方镜像（D-17 构建）")
    run.add_argument("--repo-url-template", default="https://github.com/{repo}.git")
    run.add_argument("--workspace-path", default="/workspace", help="沙箱里工作区的挂载点（/testbed 指向它）")
    run.add_argument("--keep-workspaces", action="store_true")

    grade = sub.add_parser("grade", help="调官方判分工具")
    grade.add_argument("--out", type=Path, required=True)
    grade.add_argument("--python", required=True, help="装了 swebench（v5）的 Python")
    grade.add_argument("--max-workers", type=int, default=4)
    grade.add_argument("--modal", action="store_true", help="在 Modal 上判分（不要本机 Docker，花钱）")

    rep = sub.add_parser("report", help="写 report.json 与 report.md")
    rep.add_argument("--out", type=Path, required=True)

    suite_parser = benches.add_parser("suite", help="并发运行多个评测适配器")
    suite_sub = suite_parser.add_subparsers(dest="suite_command", required=True)
    suite_run = suite_sub.add_parser("run", help="按计划运行评测")
    suite_run.add_argument("--plan", type=Path, required=True)
    suite_run.add_argument("--out", type=Path, required=True)
    suite_run.add_argument("--dry-run", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> int:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"要设环境变量 {TOKEN_ENV}（Task API 的令牌，例如 scripts/dev_stack.py token --tenant <租户> --user <用户> 的输出）", file=sys.stderr)
        return 2
    instances = sb.load_instances(args.instances)
    ids = [line.strip() for line in args.ids.read_text("utf-8").splitlines() if line.strip()] if args.ids else None
    if (args.stratify or args.exclude_repo) and args.limit is None:
        print("--stratify、--exclude-repo 要与 --limit 一起用", file=sys.stderr)
        return 2
    if (args.exclude_repo or args.exclude_ids) and not args.stratify:
        print("--exclude-repo、--exclude-ids 要与 --stratify 一起用", file=sys.stderr)
        return 2
    exclude_ids = [line.strip() for line in args.exclude_ids.read_text("utf-8").splitlines() if line.strip()] if args.exclude_ids else []
    if args.stratify:
        chosen = sb.stratified(instances, limit=args.limit, seed=args.seed, exclude_repos=args.exclude_repo, exclude_ids=exclude_ids)
    else:
        chosen = sb.select(instances, limit=args.limit, seed=args.seed, ids=ids)
    run_id = args.run_id or re.sub(r"[^A-Za-z0-9._-]+", "-", args.out.absolute().name).strip("-.") or "run"
    settings = sb.Settings(
        run_id=run_id, budget_usd=args.budget_usd, task_usd_limit=args.task_usd_limit, model_name=args.model_name,
        concurrency=args.concurrency, task_timeout_s=args.task_timeout_min * 60, hints=args.hints, environment=args.environment,
        repo_url_template=args.repo_url_template, workspace_path=args.workspace_path, keep_workspaces=args.keep_workspaces, poll_s=args.poll_s,
    )
    selection = {
        "instances_file": str(args.instances), "instances_sha256": hashlib.sha256(args.instances.read_bytes()).hexdigest(),
        **({"ids_file": str(args.ids), "ids": True} if args.ids else {"limit": args.limit, "seed": args.seed}),
        **({"stratified": True, "exclude_repos": sorted(args.exclude_repo), "excluded_ids": len(exclude_ids)} if args.stratify else {}),
    }
    summary = sb.run(RuntimeClient(args.task_api, token), chosen, args.out, settings, selection=selection)
    print(
        f"跑完 {summary.finished} 题（这次提交 {summary.submitted} 个 Task），没跑 {summary.not_run} 题，出错 {summary.errors} 题；"
        f"已花 ${summary.spent_usd:.4f}（预算 ${args.budget_usd:.2f}）"
    )
    print(f"predictions：{args.out / 'predictions.jsonl'}；判分：… swebench grade --out {args.out} --python <装了 swebench 的 Python>")
    return 1 if summary.errors else 0


def _suite_run(args: argparse.Namespace) -> int:
    try:
        plan = suite.parse_plan(json.loads(args.plan.read_text("utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"读取评测计划失败：{exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        for argv in suite.dry_run(plan, args.out):
            print(" ".join(argv))
        return 0
    summary = suite.run_suite(plan, args.out)
    print((args.out / "suite-report.md").read_text("utf-8"))
    return 0 if summary.status == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    # 输出重定向到文件时 Windows 用本地代码页（例如 cp1252），中文摘要编码不了：换成转义，不让打印中断。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        if args.benchmark == "suite":
            return _suite_run(args)
        if args.command == "fetch":
            count = sb.fetch_instances(args.out, dataset=args.dataset, split=args.split)
            print(f"取到 {count} 题：{args.out}")
            return 0
        if args.command == "run":
            return _run(args)
        if args.command == "grade":
            graded = sb.grade(args.out, python=args.python, max_workers=args.max_workers, modal=args.modal)
            print(f"判分完成：解决 {len(graded.get('resolved_ids') or [])} 题；报告用 … swebench report --out {args.out}")
            return 0
        rep = sb.report(args.out)
        print((args.out / "report.md").read_text("utf-8"))
        return 0 if rep["selected"] else 1
    except sb.ResumeMismatch as exc:
        print(exc, file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError, ApiError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
