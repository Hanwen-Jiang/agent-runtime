import hashlib
import json
from pathlib import Path

from agentruntime_learning.evals.public_capability import PublicCapabilitySource, project_scores
from agentruntime_learning.evals.public_matrix_panel import fingerprint, panel, MEMBERS


def test_planned_coverage_separates_recorded_from_numeric():
    p = {"n":2,"seed":0,"cells":[{"role":"act","tier":"medium","evals":["bfcl"],"candidates":["a","b","c"]}]}
    sc = {"cells":[{"role":"act","tier":"medium","evals":[{"name":"bfcl-v4","version":"v1"}],
                    "candidates":[{"candidate":"a","runs":3,"score":.5,"items":0},
                                  {"candidate":"b","runs":3,"score":0.0,"items":2}]}]}
    run = project_scores("test",sc,p,"d")
    assert run["coverage"] == {"planned":3,"observed":2,"numeric":1,"eligible":0}
    assert run["columns"][0]["planned_models"] == ["a","b","c"]


def test_verified_locked_ids_only_for_public_matching_samples():
    c={"role":"judge","tier":"medium","candidates":["m"],"evals":["judgebench","s10"]}
    p={"cells":[c],"seed":4,"n":5}
    lock={"evaluations":[{"cell":"general.unclassified/judge/medium","spec":name,"name":name,
                          "version":"v1","items":ids} for name,ids in (("judgebench",["q2","q1"]),("s10",["private-1"]))]}
    candidate={"candidate":"m","items":2,"runs":4,"score":0.5,"observed_cohorts":{
        "judgebench":{"count":1,"sha256":fingerprint(["q2"])},
        "s10":{"count":1,"sha256":fingerprint(["private-1"])}}}
    sc={"cells":[{**c,"evals":[{"name":n,"version":"v1"} for n in ("judgebench","s10")],"candidates":[candidate]}]}
    run=project_scores("test",sc,p,"d",lock=lock)
    infos=run["models"][0]["cells"]["general.unclassified/judge.medium"]["selection_info"]["groups"]
    assert infos[0]["selection_ids"]==["q2"] and infos[1]["ids_hidden"]
    assert "private-1" not in json.dumps(run)
    candidate["observed_cohorts"]["judgebench"]["sha256"]="0"*64
    ci=project_scores("test",sc,p,"d",lock=lock)["models"][0]["cells"]["general.unclassified/judge.medium"]
    assert "selection_ids" not in ci["selection_info"]["groups"][0]
    p["split"]="holdout"
    assert "q2" not in json.dumps(project_scores("test",sc,p,"d",lock=lock))


def test_default_is_multirole_and_explicit_composition_fails_closed(tmp_path):
    for n, roles, at in [("old", ["plan", "route"], "2026-02-03"),("probe", ["act"], "2026-03-04")]:
        folder=tmp_path/"mx"/n
        folder.mkdir(parents=True)
        cells=[{"role":r,"tier":"low","evals":["bfcl"],"candidates":["m"]} for r in roles]
        (folder/"plan.json").write_text(json.dumps({"cells":cells}))
        (folder/"scores.json").write_text(json.dumps({"cells":[],"scored_at":at}))
    payload=json.loads(PublicCapabilitySource(tmp_path).get())
    assert (payload["primary_run_id"],payload["latest_run_id"]) == ("old","probe")
    wrong={name:{"deployment":{},"evaluations":[],"plan":{}} for name in MEMBERS}
    assert panel({name:{} for name in MEMBERS},wrong) is None


def test_refusals_preserve_records_without_becoming_measurements():
    from agentruntime_learning.evals.public_capability import apply_progress
    planned={"cells":[{"role":"act","tier":"medium","candidates":["m"],"evals":["bfcl"]}]}
    col="general.unclassified/act.medium"
    scores={"cells":[{"role":"act","tier":"medium","candidates":[{"candidate":"m","items":1,"runs":3,"score":0.0}]}]}
    r=project_scores("test",scores,planned,"d")
    apply_progress(r,{"counts":[{"column":col,"model":"m","expected":5,"runs":3,"clean":0,"quota":3}]})
    assert r["coverage"] == {"planned":1,"observed":1,"numeric":0,"eligible":0}
    assert r["trial_coverage"] == {"expected":5,"runs":3,"clean":0,"quota":3,"clean_cells":0}
    assert r["models"][0]["cells"][col]["score"] is None


def test_pause_requires_current_ledger_and_marker_identity(tmp_path):
    rid="capability-resume-20261123-core"
    f=tmp_path/"mx"/rid
    f.mkdir(parents=True)
    plan={"name":rid,"seed":0,"cells":[{"role":"act","tier":"medium","candidates":["m"],"evals":["bfcl"]}]}
    (f/"plan.json").write_text(json.dumps(plan))
    lock=json.dumps({"plan":plan,"evaluations":[]}).encode()
    (f/"run-lock.json").write_bytes(lock)
    ledger=f/"results.jsonl"
    ledger.write_bytes(b"private trial\n")
    marker=f/"provider-quota-pause.json"
    marker.write_bytes(b"private operator proof")
    for path in (ledger,marker):
        path.chmod(0o600)
    sig=lambda p:[p.stat().st_size,p.stat().st_mtime_ns]
    public={"schema":2,"run_id":rid,"lock_sha256":hashlib.sha256(lock).hexdigest(),
            "ledger_sha256":hashlib.sha256(ledger.read_bytes()).hexdigest(),
            "files":{"results.jsonl":sig(ledger),"provider-quota-pause.json":sig(marker)},
            "pause":{"reason":"insufficient_user_quota","detected_at":"2026-11-23T00:00:00Z"},
            "counts":[{"column":"general.unclassified/act.medium","model":"m","expected":4,"runs":2,"quota":2,"clean":0}]}
    (f/"public-matrix-progress.json").write_text(json.dumps(public))
    source=PublicCapabilitySource(tmp_path,ttl_s=0)
    read=lambda:json.loads(source.get())["runs"][0]
    assert read()["pause"]["reason"]=="insufficient_user_quota"
    ledger.write_bytes(b"private trial\nnew trial\n")
    assert "pause" not in read()
    marker.unlink()
    assert "pause" not in read()


def test_user_stopped_recovery_replaces_cached_scores_only_with_archived_evidence(tmp_path):
    name = "capability-resume-20261123-transform"
    folder = tmp_path / "mx" / name
    folder.mkdir(parents=True)
    plan = {"name":name,"seed":0,"cells":[{"role":"transform","tier":"low","candidates":["m"],"evals":["s11"]}]}
    (folder/"plan.json").write_text(json.dumps(plan))
    lock = json.dumps({"plan":plan,"evaluations":[]}).encode()
    (folder/"run-lock.json").write_bytes(lock)
    (folder/"results.jsonl").write_text("private receipt\n")
    scores = folder/"scores.json"
    scores.write_text(json.dumps({"plan":name, "seed":0, "scored_at":"2026-11-23T00:01:00Z","cells":[]}))
    source = PublicCapabilitySource(tmp_path,ttl_s=0)
    read = lambda:json.loads(source.get())
    assert read()["runs"][0]["at"]
    scores.rename(folder/"scores.before-quota-recovery.json")
    assert read()["stale"]
    for file in ("user-pause.json","resume-reconciliation.json","provider-quota-pause.before-recovery.json"):
        (folder/file).write_bytes(b"private proof")
    files = {f.name:[f.stat().st_size,f.stat().st_mtime_ns] for f in folder.iterdir()}
    doc = {"schema":2,"run_id":name,"lock_sha256":hashlib.sha256(lock).hexdigest(),"ledger_sha256":"0"*64,
           "files":files,"pause":{"reason":"user_requested","detected_at":"2026-11-23T01:00:00Z"},
           "previous_quota":{"results_sha256":"1"*64,"scores_sha256":"2"*64,"runs":3,"clean":1,"quota":2,"retained":1},
           "counts":[{"column":"general.unclassified/transform.low","model":"m","expected":5,"clean":2,"runs":2,"quota":0}]}
    (folder/"public-matrix-progress.json").write_text(json.dumps(doc))
    response = read()
    assert not response["stale"]
    current, = response["runs"]
    assert current["pause"]["reason"] == "user_requested"
    assert current["previous_quota"]["clean"] == 1 and current["trial_coverage"]["clean"] == 2
    assert current["coverage"]["numeric"] == 0
    (folder/"user-pause.json").unlink()
    assert "pause" not in read()["runs"][0]


def test_designated_recovery_counts_logical_trials_and_preserves_native_scores(tmp_path):
    import hashlib
    import pytest
    from agentruntime_learning.evals.public_capability import PublicCapabilitySource, SUPPLEMENT_MANIFESTS

    rid = "capability-resume-20261123-judge-v2"
    folder = tmp_path / "mx" / rid
    folder.mkdir(parents=True)
    native = folder / "results.jsonl"
    native.write_bytes(b"original receipt\n")
    sig = [native.stat().st_size,native.stat().st_mtime_ns]
    native_sha = hashlib.sha256(native.read_bytes()).hexdigest()
    lk = "e"*64
    column = "general.unclassified/judge.medium"
    cell = {"column":column,"model":"candidate","expected":2,"runs":2,"clean":2,"quota":0,"terminal":2}
    progress = {"files":{"results.jsonl":sig},"ledger_sha256":native_sha,"counts":[cell]}
    merged = {"schema":"matrix-supplement-coverage-v1","run_id":rid,"lock_sha256":lk,
              "manifest_sha256":SUPPLEMENT_MANIFESTS[rid],"native_ledger_sha256":native_sha,
              "supplemental_ledger_sha256":"b"*64,"native_signature":sig,"selected_sources":{"native":1,"supplemental":1},
              "native_receipts":2,"supplemental_receipts":1,"observed":2,"expected":2,"counts":[cell],
              "terminal_dispositions":2,"quota":0,"clean":2,"complete":False}
    proof = folder/"public-supplemental-coverage.json"
    def write(value):
        proof.write_text(json.dumps(value))
    source = PublicCapabilitySource(tmp_path)
    write(merged)
    summary, scores, _ = source._supplement(rid,lk,progress)
    assert summary["observed"] == 2 and json.loads(scores) == {"cells":[]}
    write({**merged,"selected_sources":{"native":2,"supplemental":1}})
    with pytest.raises(ValueError):
        source._supplement(rid,lk,progress)
    scorefile = folder/"supplemental-scores.json"
    scorefile.write_bytes(b'{"plan":"test","cells":[]}')
    write({**merged,"complete":True,"scores_sha256":hashlib.sha256(scorefile.read_bytes()).hexdigest()})
    assert source._supplement(rid,lk,progress)[1] == scorefile.read_bytes()
    scorefile.write_bytes(b'{"other":"changed"}')
    with pytest.raises(ValueError):
        source._supplement(rid,lk,progress)
