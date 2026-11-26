"""Gate the native SWE runner on the repair smoke, a frozen cohort, and real project-image readiness."""
import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks.__main__ import _live
from agentruntime_learning.benchmarks.client import RuntimeClient, token_from_env


PROFILE = "a6api/gpt-6.2-sol@2026-11"
SMOKE_IDS = {"sphinx-doc__sphinx-8056", "sphinx-doc__sphinx-9367"}
LIST_SHA = "594827f9dcc5c06dded01fc66872ceedb4821e30b8cd8e6484f1ace5cea65331"
DATA_SHA = "2d336d3f60d2a6496b21cf04ab73ed6cdd016eb52f19e1e4bb0328a278145d52"


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def smoke_graded(path):
    report = read(path / "report.json")
    if {r["instance_id"] for r in report["instances"]} != SMOKE_IDS or not report["graded"]:
        return False
    grading = list((path / "grading").glob("*.json"))
    for filename in grading:
        raw = read(filename)
        if set(raw.get("completed_ids", [])) == SMOKE_IDS:
            return all(read(path/"records"/(i+".json")).get("task_id")
                       and read(path/"records"/(i+".json")).get("patch") for i in SMOKE_IDS)
    return False


class PreparedRuntime(RuntimeClient):
    def __init__(self, api, token, out, helper, identities, diff_api=None):
        super().__init__(api, token)
        self.out = out
        self.helper = helper
        self.identities = identities
        self.diff_api = diff_api.rstrip("/") if diff_api else None

    def diff(self, workspace_id, *, base):
        if self.diff_api:
            return RuntimeClient(self.diff_api, self._token, timeout=self.timeout).diff(workspace_id, base=base)
        return super().diff(workspace_id, base=base)

    def submit(self, *, workspace_id, message, client_request_id, network=None):
        iid = self.identities[workspace_id]
        evidence = self.out / "prewarm" / (iid + ".json")
        evidence.parent.mkdir(exist_ok=True)
        # A clean first attempt is only submitted after a real authorized read-only
        # lease used the exact ready OCI image and was released. No model is called here.
        args = ["/usr/bin/python3", str(self.helper), "--tenant", "swebench", "--user", "mini50-eval",
                "--workspace", workspace_id, "--instance-id", iid, "--timeout", "1800", "--output", str(evidence)]
        last = None
        for attempt in range(3):
            try:
                subprocess.run(args, check=True, timeout=1920, stdout=subprocess.DEVNULL)
                last = None
                break
            except subprocess.CalledProcessError as exc:
                last = exc
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
        if last is not None:
            raise sb.PreSubmissionError("project image prewarm remained unavailable")
        fact = read(evidence)
        if not (fact.get("ready") is True and fact.get("instance_id") == iid and fact.get("released") is True):
            raise sb.PreSubmissionError("project image readiness is not verified")
        return super().submit(workspace_id=workspace_id, message=message, client_request_id=client_request_id,
                              network=network)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("a", "b"), required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--smoke", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--prewarm", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("/opt/swebench/data/verified.jsonl"))
    parser.add_argument("--diff-api", help="Workspace diff API. Keep task submission on the isolated arm and recover diffs through a stable shared API.")
    args = parser.parse_args()
    if not args.prewarm.is_file():
        raise RuntimeError("project-image preflight helper missing")
    if sha(args.ids) != LIST_SHA or sha(args.dataset) != DATA_SHA:
        raise ValueError("benchmark input changed")
    frozen = read(args.freeze)
    if frozen["model_profile"] != PROFILE or frozen["tier"] != "strong@medium":
        raise ValueError("model/tier changed")
    ids = args.ids.read_text().splitlines()
    if len(set(ids)) != len(ids) or len(ids) != 50:
        raise ValueError("expected 50 unique public Mini IDs")
    instances = sb.select(sb.load_instances(args.dataset), ids=ids)
    run_id = args.out.name
    model_label = "runtime-" + args.arm + "-gpt-6.2-sol-fixed"
    settings = sb.Settings(run_id=run_id, model_name=model_label, budget_usd=150.0, task_usd_limit=3.0,
                           concurrency=3, task_timeout_s=3600.0, poll_s=10.0, hints=False, environment="official")
    client = PreparedRuntime(args.api, token_from_env(), args.out, args.prewarm,
                             {sb.workspace_id(run_id, iid): iid for iid in ids}, diff_api=args.diff_api)
    selection = {"dataset_name": "SWE-bench/SWE-bench_Verified", "dataset_split": "test", "population_count": 500,
                 "instances_file": str(args.dataset), "instances_sha256": DATA_SHA, "ids_file": str(args.ids),
                 "public_cohort": "MariusHobbhahn/swe-bench-verified-mini", "source_commit": "1ce432065d9ed226017ef1ac88b5f91818383c64",
                 "cohort_file_sha256": LIST_SHA}
    protocol = {"arm": args.arm, "run_id": run_id, "model_label": model_label, "model_profile": PROFILE,
                "tier": frozen["tier"], "thinking_budget": frozen["thinking_budget"],
                "runtime_tree": frozen["baseline_tree" if args.arm=="a" else "candidate_tree"],
                "single_generation_attempt": True, "prewarm_required": True, "input": selection,
                "smoke_run_id": args.smoke.name, "smoke_reference": "historical smoke is not a formal prerequisite; every selected task is prewarmed before submit",
                "budget_usd": settings.budget_usd,
                "task_usd_limit": settings.task_usd_limit, "task_timeout_s": settings.task_timeout_s,
                "concurrency": settings.concurrency, "hints": False, "official_grader": True}
    path = args.out / "paired-protocol.json"
    if path.exists() and read(path) != protocol:
        raise RuntimeError("refusing to change frozen experiment")
    args.out.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(protocol, indent=2)+"\n")
    with _live(run_id, model_label, suite=sb.SUITE, split="verified") as live:
        result = sb.run(client, instances, args.out, settings, selection=selection, live=live)
    print("finished", result.finished, "errors", result.errors, "not run", result.not_run, flush=True)
    return int(bool(result.not_run or result.errors))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except sb.ProviderQuotaPaused:
        raise SystemExit(75) from None
