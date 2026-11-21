"""Run the two fixed Sphinx smoke instances through the pinned GPT-6.2 runtime.

This is a two-instance diagnostic run, separate from the public Mini-50 denominator.
Each task still passes the same read-only project-image prewarm gate as Mini-50.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.__main__ import _live
from agentruntime_learning.benchmarks.client import token_from_env

from run_paired_arm import PROFILE, PreparedRuntime, read

DATA_SHA = "2d336d3f60d2a6496b21cf04ab73ed6cdd016eb52f19e1e4bb0328a278145d52"
SPHINX_IDS = ("sphinx-doc__sphinx-8056", "sphinx-doc__sphinx-9367")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("a", "b"), default="b")
    parser.add_argument("--api", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ids", type=Path, default=Path(__file__).with_name("sphinx.ids"))
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--prewarm", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("/opt/swebench/data/verified.jsonl"))
    args = parser.parse_args()

    ids = tuple(line.strip() for line in args.ids.read_text().splitlines() if line.strip())
    if ids != SPHINX_IDS:
        raise ValueError(f"Sphinx IDs changed: {ids!r}")
    if file_sha(args.dataset) != DATA_SHA:
        raise ValueError("benchmark input changed")
    frozen = read(args.freeze)
    if frozen["model_profile"] != PROFILE or frozen["tier"] != "strong@medium":
        raise ValueError("model/tier changed")
    instances = sb.select(sb.load_instances(args.dataset), ids=ids)
    run_id = args.out.name
    model_label = "runtime-" + args.arm + "-gpt-6.2-sol-fixed"
    settings = sb.Settings(
        run_id=run_id,
        model_name=model_label,
        budget_usd=6.0,
        task_usd_limit=3.0,
        concurrency=1,
        task_timeout_s=3600.0,
        poll_s=10.0,
        hints=False,
        environment="official",
    )
    client = PreparedRuntime(
        args.api,
        token_from_env(),
        args.out,
        args.prewarm,
        {sb.workspace_id(run_id, iid): iid for iid in ids},
    )
    selection = {
        "dataset_name": "SWE-bench/SWE-bench_Verified",
        "dataset_split": "test",
        "population_count": 500,
        "instances_file": str(args.dataset),
        "instances_sha256": DATA_SHA,
        "ids_file": str(args.ids),
        "ids_sha256": file_sha(args.ids),
        "ids": list(ids),
    }
    protocol = {
        "run_kind": "sphinx-two-task-diagnostic",
        "arm": args.arm,
        "run_id": run_id,
        "model_label": model_label,
        "model_profile": PROFILE,
        "tier": frozen["tier"],
        "thinking_budget": frozen["thinking_budget"],
        "runtime_tree": frozen["baseline_tree" if args.arm == "a" else "candidate_tree"],
        "single_generation_attempt": True,
        "prewarm_required": True,
        "input": selection,
        "budget_usd": settings.budget_usd,
        "task_usd_limit": settings.task_usd_limit,
        "task_timeout_s": settings.task_timeout_s,
        "concurrency": settings.concurrency,
        "hints": False,
        "official_grader": True,
        "mini50_denominator": False,
    }
    args.out.mkdir(exist_ok=True, parents=True)
    protocol_path = args.out / "paired-protocol.json"
    if protocol_path.exists() and read(protocol_path) != protocol:
        raise RuntimeError("refusing to change frozen experiment")
    protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n")
    with _live(run_id, model_label, suite=sb.SUITE, split="verified") as live:
        result = sb.run(client, instances, args.out, settings, selection=selection, live=live)
    print("finished", result.finished, "errors", result.errors, "not run", result.not_run, flush=True)
    return int(bool(result.not_run or result.errors))


if __name__ == "__main__":
    raise SystemExit(main())
