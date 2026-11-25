"""CLI entrypoint: python -m agentruntime_learning.evolution {matrix,evolve,candidates}."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider

from agentruntime_learning.evolution.matrix import SUPPORTED_SUITES, SkillMatrixRunner, load_platform_skills_variants, write_matrix_reports
from agentruntime_learning.evolution.pipeline import run_evolution_cycle
from agentruntime_learning.evolution.store import EvolutionStore

DEFAULT_SKILLS_FILE = Path(__file__).resolve().parents[5] / "deploy" / "dev" / "platform-skills.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_learning.evolution")
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("matrix", help="Run Model x Skill x Benchmark evaluation matrix")
    m.add_argument("--out", required=True, help="Output report directory")
    m.add_argument("--skills-file", default=str(DEFAULT_SKILLS_FILE))
    m.add_argument("--skills", default="", help="Comma-separated skill names (empty = all)")
    m.add_argument("--models", default="strong@medium,mid@medium", help="Comma-separated model tiers")
    m.add_argument("--suites", default=",".join(SUPPORTED_SUITES), help="Comma-separated suites")
    m.add_argument("--mode", default="fixture", choices=("fixture", "gateway"))
    m.add_argument("--budget-usd", type=float, default=0.0)

    e = sub.add_parser("evolve", help="Cluster OTel/console failure traces and propose+evaluate skill candidate")
    e.add_argument("--traces", nargs="+", required=True, help="Trace files (otlp_json or console)")
    e.add_argument("--format", default="console", choices=("otlp_json", "console"))
    e.add_argument("--skills-file", default=str(DEFAULT_SKILLS_FILE))
    e.add_argument("--out", required=True, help="Output report directory")
    e.add_argument("--data-dir", default="", help="Relational data dir for SQLite EvolutionStore")
    e.add_argument("--models", default="strong@medium,mid@medium")
    e.add_argument("--mode", default="fixture", choices=("fixture", "gateway"))
    e.add_argument("--min-support", type=int, default=2)
    e.add_argument("--budget-usd", type=float, default=0.0)

    c = sub.add_parser("candidates", help="Inspect stored evolution candidates")
    c_sub = c.add_subparsers(dest="action", required=True)
    c_list = c_sub.add_parser("list", help="List candidates")
    c_list.add_argument("--data-dir", required=True)
    c_list.add_argument("--status", default="")
    c_show = c_sub.add_parser("show", help="Show single candidate JSON")
    c_show.add_argument("--data-dir", required=True)
    c_show.add_argument("candidate_id")
    return parser


async def _run_async(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "matrix":
        wanted = [s.strip() for s in args.skills.split(",") if s.strip()] or None
        variants = load_platform_skills_variants(Path(args.skills_file), skill_names=wanted)
        tiers = [t.strip() for t in args.models.split(",") if t.strip()]
        suites = [s.strip() for s in args.suites.split(",") if s.strip()]
        runner = SkillMatrixRunner(budget_usd=args.budget_usd)
        report = await runner.run(variants=variants, model_tiers=tiers, suites=suites, mode=args.mode)
        jp, mp = write_matrix_reports(report, Path(args.out))
        return {"run_id": report.run_id, "cells": len(report.cells), "json": str(jp), "markdown": str(mp)}

    if args.command == "evolve":
        variants = load_platform_skills_variants(Path(args.skills_file))
        by_name = {v.skill_name: v for v in variants}
        lines: list[str] = []
        for path in args.traces:
            lines.extend(Path(path).read_text("utf-8").splitlines())
        store = EvolutionStore(SQLiteRelationalStoreProvider(Path(args.data_dir) / "relational")) if args.data_dir else None
        tiers = [t.strip() for t in args.models.split(",") if t.strip()]
        res = await run_evolution_cycle(
            lines=lines,
            fmt=args.format,
            skills_by_name=by_name,
            out_dir=Path(args.out),
            store=store,
            model_tiers=tiers,
            mode=args.mode,
            min_support=args.min_support,
            budget_usd=args.budget_usd,
        )
        return res.summary()

    store = EvolutionStore(SQLiteRelationalStoreProvider(Path(args.data_dir) / "relational"))
    await store.migrate()
    if args.action == "list":
        rows = await store.list_candidates(status=args.status)
        return {"candidates": [{"candidate_id": r.candidate_id, "target_name": r.target_name, "status": r.status} for r in rows]}
    rec = await store.get_candidate(args.candidate_id)
    return rec.to_json()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out = asyncio.run(_run_async(args))
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
