"""一条命令跑评测（计划 S-08）：

    uv run --package agentruntime-learning python -m agentruntime_learning.evals run --subject baseline

参数：--eval-set 目录（默认仓库里的 task-loop）、--subject（baseline、oracle 或 <模块>:<工厂函数>）、
--split（dev 或 holdout，默认 dev）、--reports-dir、--compare（报告路径、latest 或 none，默认 latest）、
--require-confirmed（还有没确认的标注时报错）、--repeat N（跑 N 次，另写一份带中位数与范围的汇总）、
--gate s09|s10（按中位数判 S-09 或 S-10 的门槛，写出差距；见 gates.py）。

S-11 的压缩评测是另一个子命令：

    uv run --package agentruntime-learning python -m agentruntime_learning.evals context --subject lexical

被测对象是 lexical（离线基线）或 <模块>:<工厂函数>；真模型用 agentruntime_learning.evals.worker_subject:context_subject。
--repeat N 跑 N 次，指标取中位数，门槛按中位数判。

退出码：0 跑完（不论分数）；2 参数或评测集有问题；3 要求标注全部确认而还有草稿。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, EvalSetError, load_eval_set
from agentruntime_learning.evals.context import CONTEXT_DIR, load_context_samples, load_context_subject, run_context_eval
from agentruntime_learning.evals.gates import GATES
from agentruntime_learning.evals.repeat import RepeatedReport, run_repeated
from agentruntime_learning.evals.runner import DEFAULT_REPORTS_DIR, LATEST, SPLITS, EvalConfigError, Report, UnconfirmedLabelsError, run_eval
from agentruntime_learning.evals.subjects import load_subject


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
    context = commands.add_parser("context", help="S-11 压缩评测：长任务样本上比较 full 与 mask_only 的 Probe 通过率与重复操作率")
    context.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET, help="评测集目录（读其中的 context_samples）")
    context.add_argument("--subject", required=True, help="lexical（离线基线）或 <模块>:<工厂函数>")
    context.add_argument("--split", choices=SPLITS, default="dev", help="dev（默认）或 holdout；留出集只给汇总")
    context.add_argument("--work-dir", type=Path, default=None, help="压缩时外置内容的临时目录（默认系统临时目录）")
    context.add_argument("--reports-dir", type=Path, default=None, help="给了就把报告（JSON）写在这里")
    context.add_argument("--require-confirmed", action="store_true", help="还有没经人确认的样本标注时报错")
    context.add_argument("--repeat", type=_positive, default=1, help="跑几次（默认 1）；指标取中位数，门槛按中位数判")
    return parser


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"要是正整数，得到 {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"要是正整数，得到 {value}")
    return value


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
        report = asyncio.run(run_context_eval(samples, subject, split=args.split, work_dir=args.work_dir or Path(scratch), repeat=args.repeat))
    print(report.summary())
    if args.reports_dir is not None:
        args.reports_dir.mkdir(parents=True, exist_ok=True)
        path = args.reports_dir / f"context-{report.subject}-{report.split}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2), "utf-8")
        print(f"报告：{path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command == "context":
        return _context(args)
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
    common = {"split": args.split, "reports_dir": args.reports_dir, "compare": compare, "require_confirmed": args.require_confirmed}
    try:
        if args.repeat > 1 or args.gate:
            report: Report | RepeatedReport = asyncio.run(run_repeated(eval_set, subject, repeat=args.repeat, gate=args.gate, **common))
        else:
            report = asyncio.run(run_eval(eval_set, subject, **common))
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
