"""Execute a predeclared missing matrix trial once with an independent billing identity."""
import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

DEPLOY = Path("/opt/evals/deployments/capability-resume-20261123")
NATIVE = Path("/opt/evals/mx")
SUPP = DEPLOY / "supplemental-attempts"
STOP = DEPLOY / "user-stop-20261123T075640Z"
NAMES = ("core", "transform", "judge-v2")
BILLING_ID = ContextVar("billing_id")
PREFIX = "capability-resume-20261123-"


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(key):
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


def key(row):
    return ("/".join([row["task_family"],row["role"],row["tier"]]), row["eval"],
            row["candidate"], row["item"], row["repeat"])


def job_key(job):
    return (job.cell.key, job.ev.name, job.candidate, job.item.id, job.repeat)


def rows(path):
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = [json.loads(line) for line in f if line.endswith(b"\n")]
    by_slot = {digest(key(row)): row for row in data}
    if len(by_slot) != len(data):
        raise ValueError("Duplicate observation identity")
    return by_slot


def atomic(path, value, freeze=False):
    encoded = (json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+"\n").encode()
    if freeze and path.exists():
        if path.read_bytes() != encoded:
            raise ValueError("Attempt registration changed: " + str(path))
        return
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
    Path(out.name).replace(path)


def scheduled(lock):
    keys = {}
    for e in lock["evaluations"]:
        cell = next(c for c in lock["plan"]["cells"]
                    if "/".join([c.get("task_family",lock["plan"].get("task_family","general.unclassified")),c["role"],c["tier"]])==e["cell"])
        spec = next(s if isinstance(s,dict) else {"eval":s} for s in cell["evals"] if (s["eval"] if isinstance(s,dict) else s)==e["spec"])
        repeat=spec.get("r",cell.get("r",lock["plan"]["r"]))
        for candidate in cell["candidates"]:
            for item in e["items"]:
                for n in range(repeat):
                    identity=(e["cell"],e["name"],candidate,item,n)
                    code=digest(identity)
                    if code in keys:
                        raise ValueError("Repeated slot in frozen plan")
                    keys[code]=identity
    return keys


def is_quota(r):
    if r.get("provider_quota",{}).get("provider_refused"):
        return True
    rec = r.get("record") or {}
    if (rec.get("model_call_failures") or {}).get("insufficient_user_quota"):
        return True
    return any(s=="insufficient_user_quota" or s.startswith("DeniedError: insufficient_user_quota:") for s in [r.get("error") or "",rec.get("error") or ""] if isinstance(s,str))


def build_manifest(name):
    rid = PREFIX+name
    native = NATIVE/rid
    old = DEPLOY/"paused-originals"/rid
    snap = STOP/rid
    future_path=old/"future-separate-attempts-private.json"
    expected=read(DEPLOY/"supplemental-code"/"future-attempts-summary.json")[rid]
    assert sha(future_path)==expected["manifest_sha256"]
    assert sha(native/"run-lock.json")==sha(snap/"run-lock.json")==expected["source_run_lock_sha256"]
    freeze=read(future_path)
    assert freeze["parent_run"]==rid and sha(old/"results.jsonl")==freeze["original_results_sha256"]
    lock=read(native/"run-lock.json")
    plan_keys=scheduled(lock)
    prior=rows(snap/"results.jsonl")
    marker=read(snap/"user-pause.json")
    assert marker["results_sha256"]==sha(snap/"results.jsonl")
    slots={x["original_slot_sha256"]:dict(x) for x in freeze["slots"]}
    assert len(slots)==expected["proposed"]
    added=0
    for path in sorted((snap/"item-budgets").glob("*.json")):
        frozen_budget = read(path)
        if path.stem in prior or path.stem in slots or not (frozen_budget.get("pending") or frozen_budget.get("spent_usd_micros",0)>0):
            continue
        slot=path.stem
        assert slot in plan_keys
        aid="mx2-"+hashlib.sha256((rid+"\0"+slot+"\0user-stop-attempt-2").encode()).hexdigest()[:32]
        slots[slot]={"original_slot_sha256":slot,"proposed_attempt_id":aid,"original_billing_state":"user_paused_unreceipted_billing","prior_budget_sha256":sha(path)}
        added+=1
    assert added == {"core":12,"transform":8,"judge-v2":7}[name], (name,added)
    assert set(slots)<=plan_keys.keys() and len({x["proposed_attempt_id"] for x in slots.values()})==len(slots)
    for k in slots:
        if k in prior:
            assert is_quota(prior[k]), "Existing non-quota result in supplement"
        else:
            b=read(snap/"item-budgets"/(k+".json"))
            assert b.get("pending") or b.get("spent_usd_micros",0)>0
    return {"schema":"matrix-supplement-execution-v1","parent_run":rid,"parent_lock_sha256":sha(native/"run-lock.json"),
            "original_protocol_sha256":expected["manifest_sha256"],"stopped_results_sha256":marker["results_sha256"],
            "slots":slots,"original_separate_slots":expected["proposed"],"additional_stopped_slots":added,
            "execution_contract":"Run one preassigned separate attempt per unresolved slot. Preserve all previous records and costs. Accept the disposition of that attempt regardless of correctness; publish raw errors separately. No repeat-until-pass."}


class CorrelatedGateway:
    def __init__(self, inner):
        self.inner=inner

    async def call(self, method, request):
        # Only the ledger's synthetic task identity changes. Prompt, scope, role,
        # constraints, tier, pin, tools and upstream generation settings are unchanged.
        request=type(request).FromString(request.SerializeToString())
        request.call.task_id=BILLING_ID.get()
        return await getattr(self.inner,method)(request)

    async def generate(self, request):
        return await self.call("generate",request)

    async def embed(self, request):
        return await self.call("embed",request)


def other_pause():
    for base in (NATIVE,SUPP):
        for n in NAMES:
            for marker in ("provider-quota-pause.json", "user-pause.json"):
                path=base/(PREFIX+n)/marker
                if path.exists():
                    return path
    return None


def check_native_assignment(folder,manifest):
    actual=rows(folder/"results.jsonl")
    for slot in manifest["slots"]:
        r=actual.get(slot)
        if r is not None:
            if not is_quota(r):
                raise ValueError("Sibling runner produced a final non-quota observation")
        else:
            budget=read(folder/"item-budgets"/(slot+".json"))
            if not (budget.get("pending") or budget.get("spent_usd_micros",0)>0):
                raise ValueError("Supplement overlaps a runnable native slot")


async def execute(name, dry):
    from agentruntime_learning.capability import matrix, quota
    from agentruntime_learning.capability.__main__ import _load
    from agentruntime_learning.evals.judging import gateway_from_env
    rid=PREFIX+name
    origin=NATIVE/rid
    folder=SUPP/rid
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest=build_manifest(name)
    eligible=manifest["slots"]
    owner=matrix.DirectoryOwner(folder)
    runner=None
    try:
        check_native_assignment(origin,manifest)
        atomic(folder/"attempts-private.json",manifest,freeze=True)
        plan,profiles,evals=_load(origin/"plan.json",DEPLOY/"profiles.json")
        frozen=read((DEPLOY/"core" if name=="core" else DEPLOY)/"freeze.json")
        revisions=read(DEPLOY/"profiles.json")["registry_revisions"]
        class Runner(matrix.MatrixRunner):
            async def _run_one(self, job):
                if other_pause():
                    return
                k=digest(job_key(job)); token=BILLING_ID.set(eligible[k]["proposed_attempt_id"])
                try:
                    await super()._run_one(job)
                finally:
                    BILLING_ID.reset(token)
        gw=None if dry else CorrelatedGateway(gateway_from_env())
        runner=Runner(plan,gw,folder,profiles=profiles,evals=evals,registry_revisions=revisions,
                      frozen=frozen,owner=owner,run_id=rid+"-supplement")
        assert sha(folder/"run-lock.json")==sha(origin/"run-lock.json")
        if not (folder/"plan.json").exists():
            (folder/"plan.json").write_bytes((origin/"plan.json").read_bytes())
        assert read(folder/"plan.json")==plan.raw
        ordered=[]
        for cp in plan.cells:
            alljobs=runner._jobs(cp,cp.candidates,{e.spec:(e.n,e.r) for e in cp.evals},include_finished=True)
            ordered.append([j for j in alljobs if digest(job_key(j)) in eligible])
        # Round robin across cells and candidates only changes emission order.
        by_model={}
        for group in ordered:
            for job in group:
                by_model.setdefault((job.cell.key,job.candidate,job.ev.name),[]).append(job)
        jobs=list(matrix._interleave(by_model.values()))
        assert {digest(job_key(j)) for j in jobs}==set(eligible) and len(jobs)==len(eligible)
        prior_rows=rows(folder/"results.jsonl")
        assert set(prior_rows)<=set(eligible)
        print(json.dumps({"parent":rid,"supplement_slots":len(jobs),"new_user_stop":manifest["additional_stopped_slots"],
                          "already_receipted":len(prior_rows),"dry_check":dry,"parent_lock_identical":True}),flush=True)
        if dry:
            return 0
        if other_pause():
            print("paused before first request",flush=True);return 75
        todo=[j for j in jobs if digest(job_key(j)) not in prior_rows]
        # Peer checks also gate repeated model calls within the same test.
        class GroupPause:
            @property
            def first_at(self):
                return local.first_at or (str(path) if (path:=other_pause()) else None)
            def trip(self):
                local.trip()
        local=runner.quota_pause
        runner.quota_pause=GroupPause()
        await runner._run_jobs(todo)
        if local.first_at:
            local.write(runner.rows)
        done=rows(folder/"results.jsonl")
        count=Counter(("quota" if is_quota(r) else "other_error" if r.get("error") or r.get("contaminated") else "scored") for r in done.values())
        print(json.dumps({"parent":rid,"observed":len(done),"statuses":count}),flush=True)
        return 75 if other_pause() else int(len(done)!=len(jobs))
    finally:
        if runner: runner.close()
        owner.close()


if __name__=="__main__":
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group",choices=NAMES)
    parser.add_argument("--dry-check",action="store_true")
    args=parser.parse_args()
    raise SystemExit(asyncio.run(execute(args.group,args.dry_check)))
