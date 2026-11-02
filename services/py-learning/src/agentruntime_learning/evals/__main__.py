"""一条命令跑评测（计划 S-08）：

    uv run --package agentruntime-learning python -m agentruntime_learning.evals run --subject baseline

参数：--eval-set 目录（默认仓库里的 task-loop）、--subject（baseline、oracle 或 <模块>:<工厂函数>）、
--split（dev 或 holdout，默认 dev）、--reports-dir、--compare（报告路径、latest 或 none，默认 latest）、
--require-confirmed（还有没确认的标注时报错）、--repeat N（跑 N 次，另写一份带中位数与范围的汇总）、
--gate s09|s10（按中位数判 S-09 或 S-10 的门槛，写出差距；见 gates.py）、--max-fallback-rate（回退率超过它的运行无效，
默认 0.05；TMP 计划第 8 节）。设了 OTEL_EXPORTER_OTLP_ENDPOINT 时每次运行经 OpenTelemetry 发 eval.run、eval.case span 与
runtime.eval.* 指标（按平台作用域 evals 绑定；TMP 计划第 9.2 节），没设时不发。

S-11 的压缩评测是另一个子命令：

    uv run --package agentruntime-learning python -m agentruntime_learning.evals context --subject lexical

被测对象是 lexical（离线基线）或 <模块>:<工厂函数>；真模型用 agentruntime_learning.evals.worker_subject:context_subject。
--repeat N 跑 N 次，指标取中位数，门槛按中位数判。设了 OTEL_EXPORTER_OTLP_ENDPOINT 时两个子命令都边跑边发进度（每条用例的结果
与花费，agentruntime_learning.evals.live），评测看板（evals.cgent.link）读它们（第 11.16 节）。

退出码：0 跑完（不论分数）；2 参数或评测集有问题；3 要求标注全部确认而还有草稿。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, EvalSetError, load_eval_set
from agentruntime_learning.evals.context import CONTEXT_DIR, load_context_samples, load_context_subject, run_context_eval
from agentruntime_learning.evals.gates import GATES
from agentruntime_learning.evals.repeat import RepeatedReport, run_repeated
from agentruntime_learning.evals.runner import (
    DEFAULT_MAX_FALLBACK_RATE,
    DEFAULT_REPORTS_DIR,
    LATEST,
    SPLITS,
    EvalConfigError,
    Report,
    UnconfirmedLabelsError,
    run_eval,
)
from agentruntime_learning.evals.subjects import load_subject

T = TypeVar("T")
ENV_OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
TELEMETRY_SERVICE = "learning"
TELEMETRY_PURPOSE = "evals"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.evals", description="评测集与评测 harness（计划 S-08）")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="跑一遍评测，输出成功率、成本与上一版的差异")
    run.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET, help="评测集目录")
    run.add_argument("--subject", required=True, help="baseline、oracle 或 <模块>:<工厂函数>")
    run.add_argument("--split", choices=SPLITS, default="dev", help="dev（默认）或 holdout；留出集只给汇总")
    run.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR, help="报告写在这里")
    run.add_argument("--compare", default=LATEST, help="和哪份报告比较：报告路径、latest（默认）或 none")
    run.add_argument("--require-confirmed", action="store_true", help="还有没经人确认的标注时报错（S-09、S-10 的门槛用）")
    run.add_argument("--repeat", type=_positive, default=1, help="跑几次（默认 1）；多于 1 次时另写一份带中位数与范围的汇总")
    run.add_argument("--gate", choices=sorted(GATES), default=None, help="按中位数判 S-09 或 S-10 的门槛，写出差距")
    run.add_argument(
        "--max-fallback-rate", type=_fraction, default=DEFAULT_MAX_FALLBACK_RATE, help="Planner、Reviewer 的回退率超过它的运行无效（默认 0.05）"
    )
    context = commands.add_parser("context", help="S-11 压缩评测：长任务样本上比较 full 与 mask_only 的 Probe 通过率与重复操作率")
    context.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET, help="评测集目录（读其中的 context_samples）")
    context.add_argument("--subject", required=True, help="lexical（离线基线）或 <模块>:<工厂函数>")
    context.add_argument("--split", choices=SPLITS, default="dev", help="dev（默认）或 holdout；留出集只给汇总")
    context.add_argument("--work-dir", type=Path, default=None, help="压缩时外置内容的临时目录（默认系统临时目录）")
    context.add_argument("--reports-dir", type=Path, default=None, help="给了就把报告（JSON）写在这里")
    context.add_argument("--require-confirmed", action="store_true", help="还有没经人确认的样本标注时报错")
    context.add_argument("--repeat", type=_positive, default=1, help="跑几次（默认 1）；指标取中位数，门槛按中位数判")

    de = commands.add_parser("deepeval", help="交付质量（N-12）：经 Task API 跑 evalsets/deliverables，再用 DeepEval 打分")
    dsub = de.add_subparsers(dest="action", required=True)
    drun = dsub.add_parser("run", help="经 Task API 跑用例，记下交付")
    drun.add_argument("--eval-set", type=Path, default=None, help="默认 evalsets/deliverables")
    drun.add_argument("--out", type=Path, required=True, help="输出目录；同一个目录再跑是续跑")
    drun.add_argument("--ids", default="", help="只跑这些用例（逗号分隔）")
    drun.add_argument("--run-id")
    drun.add_argument("--task-api", required=True)
    drun.add_argument("--budget-usd", type=float, required=True)
    drun.add_argument("--task-usd-limit", type=float, required=True)
    drun.add_argument("--concurrency", type=int, default=2)
    drun.add_argument("--task-timeout-min", type=float, default=30.0)
    drun.add_argument("--model-name", default="agent-runtime", help="被测的系统的标签（看板与报告用）")
    dscore = dsub.add_parser("score", help="用 DeepEval 的桥给交付打分（要装了 eval-requirements.txt 的 Python）")
    dscore.add_argument("--eval-set", type=Path, default=None)
    dscore.add_argument("--out", type=Path, required=True)
    dscore.add_argument("--framework-python", required=True, help="装了 DeepEval 的 Python")
    dscore.add_argument("--judge-model", required=True, help="判分的模型（与被测的不是同一家）")
    drep = dsub.add_parser("report", help="写 report.json 与 report.md")
    drep.add_argument("--out", type=Path, required=True)

    rg = commands.add_parser("ragas", help="记忆检索（N-13）：检索器取回 top-k，自己算 hit、recall、MRR，再用 Ragas 打分")
    rg.add_argument("--eval-set", type=Path, default=None, help="默认 evalsets/memory-rag")
    rg.add_argument("--retriever", default="lexical", help="lexical（离线基线）或 <模块>:<工厂函数>")
    rg.add_argument("--top-k", type=_positive, default=3)
    rg.add_argument("--out", type=Path, required=True)
    rg.add_argument("--framework-python", default="", help="装了 Ragas 的 Python；不给时只算自己的指标")
    rg.add_argument("--judge-model", default="", help="判分的模型；不给时 Ragas 只算不要模型的指标")
    rg.add_argument("--embedding-model", default="")
    return parser


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"要是正整数，得到 {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"要是正整数，得到 {value}")
    return value


def _fraction(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"要是 0 到 1 之间的数，得到 {text!r}") from None
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"要是 0 到 1 之间的数，得到 {value}")
    return value


async def _with_telemetry(run: Callable[[Any], Awaitable[T]]) -> T:
    """设了 OTEL_EXPORTER_OTLP_ENDPOINT 时给 run 一个 OpenTelemetry 的 Telemetry（平台作用域 evals），跑完导出；没设时给 None。
    导出失败只在标准错误里提一句：报告已经写好，评测不因此失败。"""
    if not os.environ.get(ENV_OTLP_ENDPOINT, "").strip():
        return await run(None)
    from agentruntime_core.adapters.otel import telemetry_from_env

    provider = telemetry_from_env(TELEMETRY_SERVICE)
    telemetry = provider.bind_platform(TELEMETRY_PURPOSE)
    try:
        return await run(telemetry)
    finally:
        try:
            await telemetry.flush()
        except Exception as exc:  # noqa: BLE001
            print(f"评测的 span 与指标没导出到 Collector：{exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            await provider.aclose()


def _context(args: argparse.Namespace) -> int:
    try:
        samples = load_context_samples(args.eval_set)
    except EvalSetError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not samples:
        print(f"{args.eval_set} 里没有长任务样本（{CONTEXT_DIR}/）", file=sys.stderr)
        return 2
    drafts = sorted(s.id for s in samples if not s.confirmed)
    if args.require_confirmed and drafts:
        print(f"这些样本的标注还没经人确认：{', '.join(drafts)}", file=sys.stderr)
        return 3
    try:
        subject = load_context_subject(args.subject)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="context-evals-") as scratch:
        work_dir = args.work_dir or Path(scratch)
        report = asyncio.run(_with_telemetry(lambda tel: run_context_eval(samples, subject, split=args.split, work_dir=work_dir, repeat=args.repeat, telemetry=tel)))
    path = None
    if args.reports_dir is not None:  # 先落盘再打印：真模型跑一遍要花钱，打印出问题也不丢报告
        args.reports_dir.mkdir(parents=True, exist_ok=True)
        path = args.reports_dir / f"context-{report.subject}-{report.split}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), "utf-8")
    print(report.summary())
    if path is not None:
        print(f"报告：{path}")
    return 0


def _deepeval(args: argparse.Namespace) -> int:
    from agentruntime_learning.benchmarks import swebench as sb
    from agentruntime_learning.benchmarks.__main__ import TOKEN_ENV, _live
    from agentruntime_learning.benchmarks.client import RuntimeClient
    from agentruntime_learning.evals import deliverables as dv
    from agentruntime_learning.evals import frameworks

    root = args.eval_set or dv.DEFAULT_SET
    try:
        if args.action == "report":
            dv.report(args.out)
            print((args.out / "report.md").read_text("utf-8"))
            return 0
        if args.action == "score":
            judge = frameworks.judge_config(args.judge_model)
            run_doc = json.loads((args.out / "run.json").read_text("utf-8"))
            with _live(str(run_doc["run_id"]), str(run_doc.get("model_name") or ""), suite=dv.SUITE, split="deliverables") as live:
                summary = dv.score(args.out, python=args.framework_python, judge=judge, root=root, live=live)
            print(f"打分完成：全部指标都通过 {summary['passed_all']}/{summary['selected']}；报告用 … deepeval report --out {args.out}")
            return 0
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            print(f"要设环境变量 {TOKEN_ENV}（Task API 的令牌）", file=sys.stderr)
            return 2
        cases = dv.load_cases(root)
        wanted = [x for x in args.ids.split(",") if x]
        if wanted:
            cases = [c for c in cases if c.instance_id in wanted]
        run_id = args.run_id or re.sub(r"[^A-Za-z0-9._-]+", "-", args.out.absolute().name).strip("-.") or "run"
        settings = sb.Settings(run_id=run_id, budget_usd=args.budget_usd, task_usd_limit=args.task_usd_limit, model_name=args.model_name,
                               concurrency=args.concurrency, task_timeout_s=args.task_timeout_min * 60, environment="none")
        with _live(run_id, args.model_name, suite=dv.SUITE, split="deliverables") as live:
            summary = dv.run(RuntimeClient(args.task_api, token), cases, args.out, settings, root=root, live=live)
        print(f"跑完 {summary.finished} 条，出错 {summary.errors} 条，已花 ${summary.spent_usd:.4f}；打分：… deepeval score --out {args.out} …")
        return 1 if summary.errors else 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _ragas(args: argparse.Namespace) -> int:
    from agentruntime_learning.evals import frameworks, rag
    from agentruntime_learning.evals.live import LiveRun

    try:
        corpus, questions = rag.load_set(args.eval_set or rag.DEFAULT_SET)
        retriever = rag.load_retriever(args.retriever, corpus)
        judge = frameworks.judge_config(args.judge_model, embedding_model=args.embedding_model) if args.judge_model else None
        if judge and not args.framework_python:
            print("给了 --judge-model 就要给 --framework-python（装了 Ragas 的 Python）", file=sys.stderr)
            return 2

        async def go(tel: Any) -> dict[str, Any]:
            live = LiveRun(tel, suite=rag.SUITE, subject=args.retriever, split="memory-rag") if tel is not None else None
            return await rag.run(retriever, corpus, questions, args.out, top_k=args.top_k, python=args.framework_python or None, judge=judge, live=live)

        summary = asyncio.run(_with_telemetry(go))
    except (ValueError, OSError, RuntimeError, ImportError, AttributeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary["metrics"], ensure_ascii=False))
    print(f"结果：{args.out / 'summary.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 输出重定向到文件时 Windows 用本地代码页（例如 cp1252），中文摘要编码不了：换成转义，不让打印中断评测。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command == "context":
        return _context(args)
    if args.command == "deepeval":
        return _deepeval(args)
    if args.command == "ragas":
        return _ragas(args)
    try:
        eval_set = load_eval_set(args.eval_set)
    except EvalSetError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        subject = load_subject(args.subject, eval_set)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    compare = {"none": None, LATEST: LATEST}.get(args.compare, Path(args.compare))
    common = {
        "split": args.split,
        "reports_dir": args.reports_dir,
        "compare": compare,
        "require_confirmed": args.require_confirmed,
        "max_fallback_rate": args.max_fallback_rate,
    }
    try:
        if args.repeat > 1 or args.gate:
            report: Report | RepeatedReport = asyncio.run(
                _with_telemetry(lambda tel: run_repeated(eval_set, subject, repeat=args.repeat, gate=args.gate, telemetry=tel, **common))
            )
        else:
            report = asyncio.run(_with_telemetry(lambda tel: run_eval(eval_set, subject, telemetry=tel, **common)))
    except UnconfirmedLabelsError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (EvalConfigError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(report.summary())
    if report.path is not None:
        print(f"报告：{report.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
