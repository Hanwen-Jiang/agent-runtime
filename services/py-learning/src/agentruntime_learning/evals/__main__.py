"""一条命令跑评测（计划 S-08）：

    uv run --package agentruntime-learning python -m agentruntime_learning.evals run --subject baseline

参数：--eval-set 目录（默认仓库里的 task-loop）、--subject（baseline、oracle 或 <模块>:<工厂函数>）、
--split（dev 或 holdout，默认 dev）、--reports-dir、--compare（报告路径、latest 或 none，默认 latest）、
--require-confirmed（还有没确认的标注时报错）。

退出码：0 跑完（不论分数）；2 参数或评测集有问题；3 要求标注全部确认而还有草稿。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, EvalSetError, load_eval_set
from agentruntime_learning.evals.runner import DEFAULT_REPORTS_DIR, LATEST, SPLITS, EvalConfigError, UnconfirmedLabelsError, run_eval
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
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
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
    try:
        report = asyncio.run(
            run_eval(eval_set, subject, split=args.split, reports_dir=args.reports_dir, compare=compare, require_confirmed=args.require_confirmed)
        )
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
