"""Freeze only the two intended candidate files; no working-tree index changes."""
from pathlib import Path
import argparse,hashlib,json,os,subprocess,tempfile
ROOT=Path(__file__).resolve().parents[5]
BASE="da58416909fc54f02519de95706df01d9bbd1701"
def git(*args,env=None,data=None):
    return subprocess.check_output(["git","-C",str(ROOT),*args],env=env,input=data).strip()
p=argparse.ArgumentParser();p.add_argument("out",type=Path);a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=True);(a.out/"candidate").mkdir(exist_ok=True)
fields={"baseline_commit":BASE,"baseline_tree":git("rev-parse",BASE+"^{tree}").decode(),
        "baseline_files":{},"candidate_files":{},"model_profile":"a6api/gpt-6.2-sol@2026-11","tier":"strong@medium","thinking_budget":8192}
with tempfile.TemporaryDirectory() as tmp:
    env={**os.environ,"GIT_INDEX_FILE":tmp+"/index"}
    git("read-tree",BASE,env=env)
    for rel in ("reviewer/rules.py","workflows/verification.py"):
        f="services/py-worker/src/agentruntime_worker/"+rel
        old=subprocess.check_output(["git","-C",str(ROOT),"show",BASE+":"+f]);new=(ROOT/f).read_bytes()
        fields["baseline_files"][rel]=hashlib.sha256(old).hexdigest();fields["candidate_files"][rel]=hashlib.sha256(new).hexdigest()
        if old==new:raise ValueError(f"missing proposed change in {f}")
        (a.out/"candidate"/Path(rel).name).write_bytes(new)
        oid=git("hash-object","-w","--stdin",data=new).decode()
        git("update-index","--cacheinfo","100644",oid,f,env=env)
    fields["candidate_tree"]=git("write-tree",env=env).decode()
    fields["changed_paths"]=git("diff","--name-only",BASE,fields["candidate_tree"]).decode().splitlines()
    assert len(fields["changed_paths"])==2
(a.out/"freeze.json").write_text(json.dumps(fields,sort_keys=True,indent=2)+"\n")
print(fields["baseline_tree"],fields["candidate_tree"])
