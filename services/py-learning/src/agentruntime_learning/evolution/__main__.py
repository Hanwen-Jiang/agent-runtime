"""CLI entrypoint: python -m agentruntime_learning.evolution {matrix,evolve,candidates}."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider

from agentruntime_learning.evolution.matrix import SUPPORTED_SUITES, SkillMatrixRunner, load_platform_skills_variants, write_matrix_reports
from agentruntime_learning.evolution.pipeline import run_evolution_cycle
from agentruntime_learning.evolution.store import EvolutionStore
from agentruntime_learning.pipeline import stream

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
    src = e.add_mutually_exclusive_group(required=True)
    src.add_argument("--traces", nargs="+", help="Trace files (otlp_json or console)")
    src.add_argument("--topic", help="Redpanda/Kafka trace topic (uses AR_EVENT_STREAM_BROKERS)")
    e.add_argument("--group", default="ar-self-evolution", help="Consumer group when --topic is set")
    e.add_argument("--format", default="console", choices=("otlp_json", "console"))
    e.add_argument("--skills-file", default=str(DEFAULT_SKILLS_FILE))
    e.add_argument("--out", required=True, help="Output report directory")
    e.add_argument("--data-dir", default="", help="Relational data dir for SQLite EvolutionStore")
    e.add_argument("--dsn", default="", help="Postgres DSN for platform EvolutionStore (or AR_EVOLUTION_DSN)")
    e.add_argument("--registry-addr", default="", help="Runtime Registry gRPC address (or AR_REGISTRY_ADDRESS)")
    e.add_argument("--publish-token", default="", help="Runtime Registry publish token (or AR_REGISTRY_PUBLISH_TOKEN)")
    e.add_argument("--models", default="strong@medium,mid@medium")
    e.add_argument("--mode", default="fixture", choices=("fixture", "gateway"))
    e.add_argument("--min-support", type=int, default=2)
    e.add_argument("--budget-usd", type=float, default=0.0)

    c = sub.add_parser("candidates", help="Inspect stored evolution candidates")
    c_sub = c.add_subparsers(dest="action", required=True)
    c_list = c_sub.add_parser("list", help="List candidates")
    c_list.add_argument("--data-dir", default="")
    c_list.add_argument("--dsn", default="")
    c_list.add_argument("--status", default="")
    c_show = c_sub.add_parser("show", help="Show single candidate JSON")
    c_show.add_argument("--data-dir", default="")
    c_show.add_argument("--dsn", default="")
    c_show.add_argument("candidate_id")
    return parser


async def _open_store(args: argparse.Namespace) -> tuple[EvolutionStore | None, Any]:
    dsn = (getattr(args, "dsn", "") or os.environ.get("AR_EVOLUTION_DSN", "")).strip()
    if dsn:
        from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider

        provider = await PostgresRelationalStoreProvider.connect(dsn, max_conns=2)
        return EvolutionStore(provider), provider
    data_dir = (getattr(args, "data_dir", "") or os.environ.get("AR_DATA_DIR", "")).strip()
    if data_dir:
        return EvolutionStore(SQLiteRelationalStoreProvider(Path(data_dir) / "relational")), None
    return None, None


def _resolve_registry_creds(args: argparse.Namespace) -> tuple[str, str]:
    addr = (getattr(args, "registry_addr", "") or os.environ.get("AR_REGISTRY_ADDRESS", "")).strip()
    tok = (getattr(args, "publish_token", "") or os.environ.get("AR_REGISTRY_PUBLISH_TOKEN", "")).strip()
    if addr and not tok:
        sec_path = os.environ.get("AR_SECRETS_FILE", "").strip()
        if sec_path:
            from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
            from agentruntime_core.errors import NotFoundError

            async def _read() -> str:
                try:
                    sec = await LocalFileSecretStoreProvider(Path(sec_path)).bind_platform("evolution").read("registry/publish-token")
                    val = sec.reveal()
                    return val.decode() if isinstance(val, bytes) else str(val)
                except NotFoundError:
                    return ""

            tok = asyncio.run(_read())
    return addr, tok


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

    store, closeable = await _open_store(args)
    registry: Any = None
    reader: Any = None
    telemetry: Any = None
    try:
        if args.command == "evolve":
            variants = load_platform_skills_variants(Path(args.skills_file))
            by_name = {v.skill_name: v for v in variants}
            lines: list[str] = []
            snap = None
            if args.traces:
                for path in args.traces:
                    lines.extend(Path(path).read_text("utf-8").splitlines())
            else:
                brokers = os.environ.get(stream.ENV_BROKERS, "").strip()
                if not brokers:
                    raise SystemExit(f"missing {stream.ENV_BROKERS} for --topic {args.topic}")
                reader = stream.TopicReader(brokers, args.topic, args.group)
                snap = reader.snapshot()
                lines = snap.lines()

            reg_addr, reg_tok = _resolve_registry_creds(args)
            if reg_addr and reg_tok:
                from agentruntime_worker.registry import GrpcRegistry

                registry = GrpcRegistry(reg_addr, publish_token=reg_tok)

            if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
                from agentruntime_core.adapters.otel import telemetry_from_env

                telemetry = telemetry_from_env("evolution", os.environ)

            tiers = [t.strip() for t in args.models.split(",") if t.strip()]
            res = await run_evolution_cycle(
                lines=lines,
                fmt=args.format,
                skills_by_name=by_name,
                out_dir=Path(args.out),
                store=store,
                registry=registry,
                telemetry=telemetry,
                model_tiers=tiers,
                mode=args.mode,
                min_support=args.min_support,
                budget_usd=args.budget_usd,
            )
            if reader is not None and snap is not None:
                reader.commit(snap)
            out = res.summary()
            if snap is not None:
                out["topic"] = snap.topic
                out["records"] = len(snap.records)
            return out

        if store is None:
            raise SystemExit("candidates requires --data-dir or --dsn / AR_EVOLUTION_DSN")
        await store.migrate()
        if args.action == "list":
            rows = await store.list_candidates(status=args.status)
            return {"candidates": [{"candidate_id": r.candidate_id, "target_name": r.target_name, "status": r.status} for r in rows]}
        rec = await store.get_candidate(args.candidate_id)
        return rec.to_json()
    finally:
        if reader is not None:
            reader.close()
        if registry is not None and hasattr(registry, "aclose"):
            await registry.aclose()
        if telemetry is not None and hasattr(telemetry, "aclose"):
            await telemetry.aclose()
        if closeable is not None and hasattr(closeable, "aclose"):
            await closeable.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out = asyncio.run(_run_async(args))
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
