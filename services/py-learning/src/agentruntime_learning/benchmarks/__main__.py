"""python -m agentruntime_learning.benchmarks swebench {fetch,run,grade,report}（TMP 计划 eval-observability-and-runtime-fixes 11.10）。

- fetch：从 Hugging Face 取题目（默认 SWE-bench/SWE-bench_Verified 的 test，500 题），写成 JSONL。
- run：经 Task API 跑选中的题（--limit 与 --seed 抽，或 --ids 文件按行给）；Task API 的令牌取环境变量 AR_TASK_API_TOKEN，
  不写进输出目录。同一个 --out 再跑是续跑。花钱：--budget-usd 是总预算，--task-usd-limit 是单题上限。
- grade：调官方判分工具（--python 指一个装了 swebench 的 Python；要 Docker，或 --modal 在 Modal 上跑）。
- report：写 report.json 与 report.md（判过分时并进判分结果）。

terminalbench {fetch,prepare,run,grade,report}（docs/plan/feedback-2026-11-01.md N-11）：fetch 克隆 Terminal-Bench 2.0；prepare 用
Docker 从官方镜像取出各任务的工作目录（只收文本文件，带不走的记为不可移植）；run 经 Task API 跑（默认只选可移植的）；grade 用 Docker
在官方镜像里跑隐藏的测试；report 写汇总。令牌、预算、并发、续跑同 swebench。

设了 OTEL_EXPORTER_OTLP_ENDPOINT 时 run 边跑边发进度、grade 判完发分数（agentruntime_learning.evals.live），评测看板读它们
（docs/plan/eval-observability-and-runtime-fixes.md 第 11.16 节）；看板上的运行 id 是 run.json 的 run_id，续跑与判分都对得上。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agentruntime_learning.benchmarks import suite
from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks import tbench_harbor
from agentruntime_learning.benchmarks import terminalbench as tb
from agentruntime_learning.benchmarks.client import ApiError, RuntimeClient
from agentruntime_learning.evals import deepeval_metrics as dm
from agentruntime_learning.evals import ragas as rg

TOKEN_ENV = "AR_TASK_API_TOKEN"
OTLP_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
TELEMETRY_SERVICE = "learning"
TELEMETRY_PURPOSE = "evals"
# 看板上 SWE-bench 的“划分”：官方的 Verified 集。
SPLIT = "verified"


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

    tbp = benches.add_parser("terminalbench", help="Terminal-Bench 2.0（N-11）")
    tsub = tbp.add_subparsers(dest="command", required=True)
    tf = tsub.add_parser("fetch", help="克隆 Terminal-Bench 2.0 的任务")
    tf.add_argument("--out", type=Path, required=True, help="克隆到这里")
    tf.add_argument("--repo", default=tb.DEFAULT_REPO)
    tf.add_argument("--ref", default="", help="提交或标签；不给时取默认分支的最新")
    tp = tsub.add_parser("prepare", help="用 Docker 从官方镜像取出各任务的工作目录")
    tp.add_argument("--tasks", type=Path, required=True, help="fetch 克隆下来的目录")
    tp.add_argument("--out", type=Path, required=True, help="运行的输出目录（prepared/ 写在里面）")
    tp.add_argument("--ids", type=Path, help="只准备这些任务（按行给名字）")
    tp.add_argument("--remove-images", action="store_true", help="取完就删镜像（省磁盘；判分时会再拉）")
    tr = tsub.add_parser("run", help="经 Task API 跑选中的任务")
    tr.add_argument("--tasks", type=Path, required=True)
    tr.add_argument("--out", type=Path, required=True, help="输出目录；同一个目录再跑是续跑")
    tpick = tr.add_mutually_exclusive_group(required=True)
    tpick.add_argument("--limit", type=int, help="按种子抽这么多个")
    tpick.add_argument("--ids", type=Path, help="按行给任务名的文件")
    tr.add_argument("--seed", type=int, default=0)
    tr.add_argument("--difficulty", action="append", default=[], choices=tb.DIFFICULTIES)
    tr.add_argument("--category", action="append", default=[])
    tr.add_argument("--include-unportable", action="store_true", help="也选不可移植的任务（工作目录里有二进制或太大的文件，带不全）")
    tr.add_argument("--run-id")
    tr.add_argument("--task-api", required=True)
    tr.add_argument("--budget-usd", type=float, required=True)
    tr.add_argument("--task-usd-limit", type=float, required=True)
    tr.add_argument("--concurrency", type=int, default=1)
    tr.add_argument("--task-timeout-min", type=float, default=60.0)
    tr.add_argument("--poll-s", type=float, default=30.0)
    tr.add_argument("--model-name", default="agent-runtime")
    tr.add_argument("--environment", choices=("official", "none"), default="official")
    tr.add_argument("--workspace-path", default="/workspace")
    tr.add_argument("--keep-workspaces", action="store_true")
    tg = tsub.add_parser("grade", help="用 Docker 在官方镜像里跑隐藏的测试")
    tg.add_argument("--tasks", type=Path, required=True)
    tg.add_argument("--out", type=Path, required=True)
    tg.add_argument("--max-workers", type=int, default=4)
    trep = tsub.add_parser("report", help="写 report.json 与 report.md")
    trep.add_argument("--out", type=Path, required=True)
    th = tsub.add_parser("harbor", help="经 Harbor（官方框架）跑：在任务的容器里跑 Runtime 的桥命令、在同一个容器里判分；fixture、oracle 只检查链路")
    th.add_argument("--tasks", type=Path, help="任务 manifest JSON；不填时使用内置 5 题 smoke manifest")
    th.add_argument("--out", type=Path, required=True)
    th.add_argument("--budget-usd", type=float, required=True)
    th.add_argument("--mode", choices=("fixture", "oracle", "harbor"), default="fixture")

    suite_parser = benches.add_parser("suite", help="并发运行多个评测适配器")
    suite_sub = suite_parser.add_subparsers(dest="suite_command", required=True)
    suite_run = suite_sub.add_parser("run", help="按计划运行评测")
    suite_run.add_argument("--plan", type=Path, required=True)
    suite_run.add_argument("--out", type=Path, required=True)
    suite_run.add_argument("--dry-run", action="store_true")

    deepeval = benches.add_parser("deepeval", help="DeepEval 首轮适配器")
    deepeval_sub = deepeval.add_subparsers(dest="deepeval_command", required=True)
    deepeval_run = deepeval_sub.add_parser("run", help="运行 Task 轨迹 JSON cases")
    deepeval_run.add_argument("--cases", type=Path, required=True)
    deepeval_run.add_argument("--out", type=Path, required=True)
    deepeval_run.add_argument("--budget-usd", type=float, required=True)
    deepeval_run.add_argument("--mode", choices=("fixture", "gateway"), default="fixture")
    deepeval_run.add_argument("--tenant-id", default="evals")
    deepeval_run.add_argument("--gateway-factory", default=os.environ.get("AR_EVAL_GATEWAY_FACTORY", ""))
    deepeval_run.add_argument("--judge-tier", default="strong@medium")

    ragas = benches.add_parser("ragas", help="Ragas 首轮适配器")
    ragas_sub = ragas.add_subparsers(dest="ragas_command", required=True)
    ragas_run = ragas_sub.add_parser("run", help="运行检索 JSON cases")
    ragas_run.add_argument("--cases", type=Path, required=True)
    ragas_run.add_argument("--out", type=Path, required=True)
    ragas_run.add_argument("--budget-usd", type=float, required=True)
    ragas_run.add_argument("--mode", choices=("fixture", "gateway"), default="fixture")
    ragas_run.add_argument("--tenant-id", default="evals")
    ragas_run.add_argument("--gateway-factory", default=os.environ.get("AR_EVAL_GATEWAY_FACTORY", ""))
    ragas_run.add_argument("--embedding-version")
    return parser


@contextlib.contextmanager
def _live(run_id: str, model: str, *, suite: str = sb.SUITE, split: str = SPLIT) -> Iterator[Any]:
    """设了 OTEL_EXPORTER_OTLP_ENDPOINT 时边跑边发进度（评测看板，第 11.16 节），结束时导出；没设时为 None。"""
    if not os.environ.get(OTLP_ENV, "").strip():
        yield None
        return
    from agentruntime_core.adapters.otel import telemetry_from_env

    from agentruntime_learning.evals.live import BlockingLive, LiveRun

    provider = telemetry_from_env(TELEMETRY_SERVICE)
    live = BlockingLive(LiveRun(provider.bind_platform(TELEMETRY_PURPOSE), suite=suite, subject=model or "agent-runtime", split=split, run_id=run_id, model=model))
    try:
        yield live
    finally:
        live.flush()
        live._call(provider.aclose())
        live.close()


def _lines(path: Path | None) -> list[str]:
    return [line.strip() for line in path.read_text("utf-8").splitlines() if line.strip()] if path else []


def _terminalbench(args: argparse.Namespace) -> int:
    if args.command == "harbor":
        return _terminalbench_harbor(args)
    if args.command == "fetch":
        print(f"Terminal-Bench 2.0：{args.out}（提交 {tb.fetch(args.out, repo=args.repo, ref=args.ref)}）")
        return 0
    if args.command == "prepare":
        tasks = tb.load_tasks(args.tasks)
        ids = _lines(args.ids)
        print(tb.prepare(tb.select(tasks, ids=ids) if ids else tasks, args.out, remove_images=args.remove_images))
        return 0
    if args.command == "grade":
        manifest = json.loads((args.out / "run.json").read_text("utf-8"))
        with _live(str(manifest["run_id"]), str((manifest.get("settings") or {}).get("model_name") or ""), suite=tb.SUITE, split="2.0") as live:
            summary = tb.grade(args.out, args.tasks, max_workers=args.max_workers, live=live)
        print(f"判分完成：判了 {summary['graded']} 个，解决 {len(summary['resolved_ids'])} 个；报告用 … terminalbench report --out {args.out}")
        return 0
    if args.command == "report":
        rep = tb.report(args.out)
        print((args.out / "report.md").read_text("utf-8"))
        return 0 if rep["selected"] else 1
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"要设环境变量 {TOKEN_ENV}（Task API 的令牌）", file=sys.stderr)
        return 2
    tasks = tb.load_tasks(args.tasks)
    only = None if args.include_unportable else tb.portable_ids(args.out)
    if only is not None and not only:
        print(f"{args.out}/prepared 里没有可移植的任务：先跑 terminalbench prepare（要 Docker）", file=sys.stderr)
        return 2
    chosen = tb.select(tasks, limit=args.limit, seed=args.seed, ids=_lines(args.ids), difficulties=args.difficulty, categories=args.category, only=only)
    run_id = args.run_id or re.sub(r"[^A-Za-z0-9._-]+", "-", args.out.absolute().name).strip("-.") or "run"
    settings = sb.Settings(
        run_id=run_id, budget_usd=args.budget_usd, task_usd_limit=args.task_usd_limit, model_name=args.model_name, concurrency=args.concurrency,
        task_timeout_s=args.task_timeout_min * 60, environment=args.environment, workspace_path=args.workspace_path, keep_workspaces=args.keep_workspaces,
        poll_s=args.poll_s,
    )
    selection = {"tasks_dir": str(args.tasks), **({"ids_file": str(args.ids)} if args.ids else {"limit": args.limit, "seed": args.seed}),
                 "difficulties": sorted(args.difficulty), "categories": sorted(args.category), "portable_only": only is not None}
    with _live(run_id, args.model_name, suite=tb.SUITE, split="2.0") as live:
        summary = tb.run(RuntimeClient(args.task_api, token), chosen, args.out, settings, selection=selection, live=live)
    print(f"跑完 {summary.finished} 个（这次提交 {summary.submitted} 个 Task），没跑 {summary.not_run} 个，出错 {summary.errors} 个；已花 ${summary.spent_usd:.4f}")
    print(f"判分：… terminalbench grade --tasks {args.tasks} --out {args.out}（要 Docker）")
    return 1 if summary.errors else 0


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
    with _live(run_id, args.model_name) as live:
        summary = sb.run(RuntimeClient(args.task_api, token), chosen, args.out, settings, selection=selection, live=live)
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


def _write_json_report(out: Path, report: dict[str, object], *, eval_name: str, budget_usd: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"eval": eval_name, "mode": "fixture", "budget_usd": budget_usd, "estimated": True, "measured": False,
                "sample_count": len(report.get("cases") or []) if isinstance(report.get("cases"), list) else 0}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _terminalbench_harbor(args: argparse.Namespace) -> int:
    manifest = tbench_harbor.load_manifest(args.tasks) if args.tasks else tbench_harbor.smoke_manifest()
    results = tbench_harbor.run(manifest, args.out, budget_usd=args.budget_usd, mode=args.mode)
    return 0 if all(r.status == "completed" and r.verifier_status in {"passed", "resolved", "success"} for r in results) else 1


def _load_gateway(spec: str):
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("gateway mode requires --gateway-factory <module>:<factory> or AR_EVAL_GATEWAY_FACTORY")
    return getattr(importlib.import_module(module_name), attr)()


def _deepeval_run(args: argparse.Namespace) -> int:
    if args.mode == "gateway":
        report = asyncio.run(dm.evaluate_gateway_cases(dm.load_cases(args.cases), _load_gateway(args.gateway_factory), tenant_id=args.tenant_id, budget_usd=args.budget_usd, tier=args.judge_tier))
    else:
        report = dm.evaluate_cases(dm.load_cases(args.cases), source="fixture")
    _write_json_report(args.out, report, eval_name="deepeval", budget_usd=args.budget_usd)
    if args.mode == "gateway":
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "manifest.json").write_text(json.dumps({"eval": "deepeval", "mode": "gateway", "budget_usd": args.budget_usd, "estimated": False, "measured": True, "sample_count": len(report.get("cases") or []), "cost_usd": report.get("cost_usd", 0.0)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row.get("status") == "ok" for row in report["cases"]) else 1


def _ragas_run(args: argparse.Namespace) -> int:
    if args.mode == "gateway":
        report = rg.run_gateway(args.cases, gateway=_load_gateway(args.gateway_factory), tenant_id=args.tenant_id, budget_usd=args.budget_usd, embedding_version=args.embedding_version, out=args.out)
    else:
        report = rg.run_fixture(args.cases, out=args.out)
    report["budget_usd"] = args.budget_usd
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


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
        if args.benchmark == "terminalbench":
            return _terminalbench(args)
        if args.benchmark == "deepeval":
            return _deepeval_run(args)
        if args.benchmark == "ragas":
            return _ragas_run(args)
        if args.command == "fetch":
            count = sb.fetch_instances(args.out, dataset=args.dataset, split=args.split)
            print(f"取到 {count} 题：{args.out}")
            return 0
        if args.command == "run":
            return _run(args)
        if args.command == "grade":
            manifest = json.loads((args.out / "run.json").read_text("utf-8"))
            with _live(str(manifest["run_id"]), str((manifest.get("settings") or {}).get("model_name") or "")) as live:
                graded = sb.grade(args.out, python=args.python, max_workers=args.max_workers, modal=args.modal, live=live)
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
