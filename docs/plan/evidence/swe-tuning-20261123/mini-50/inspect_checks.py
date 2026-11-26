"""Compare observed test-command repetition around the first source edit; never infer the command's test outcome."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


OLD = "mini50-gpt62-formal-b-20261121T1930Z"
TEST_COMMAND = re.compile(r'(?:^|[ /;&|])(?:pytest|py\.test|runtests(?:\.py)?|unittest(?:\.main)?|tox)(?:\b|$)|\b(?:manage\.py test|assert\s+|make\s+(?:check|test))')
TEST_PATH = re.compile(r'(^|/)(?:tests?(?:/|_)|[^/]+_test\.py$)')
TASK_ID = re.compile(r"tsk_[0-9A-Z]+")


def extract(root, baseline, partial=False):
    new_dir = root/"mini50-tuned-c-gpt62-20261123"
    old = json.loads(baseline.read_bytes())["mini-50"]["cases"]
    task_for = {}
    for iid, row in old.items():
        source = row["source_run"]
        if source.startswith("repair-mini50-"):
            source = OLD  # Both regrades used original B's patch and Task.
        assert source in (OLD,"mini50-infra-20261123-b","mini50-quota-20261122-b")
        for arm, folder in (("b",root/source),("c",new_dir)):
            recpath=folder/"records"/(iid+".json")
            if partial and not recpath.exists():
                continue
            rec=json.loads(recpath.read_bytes())
            if partial and not rec.get("task_finished"):
                continue
            t=rec["task_id"]; assert TASK_ID.fullmatch(t)
            task_for[t]=(arm,iid)
    in_list = ",".join("'"+t+"'" for t in task_for)
    sql=f"""begin read only; select json_build_object('t',task_id,'at',created_at,'e',data::jsonb->'payload'->'value')
    from task_events where tenant_id='swebench' and task_id in ({in_list}) and event_type='tool_status' order by created_at,event_id; commit;"""
    proc=subprocess.run(["kubectl","-n","data","exec","-i","pg-1","-c","postgres","--","psql","-XqAt","-d","agentruntime","-v","ON_ERROR_STOP=1"],
                        input=sql,text=True,capture_output=True,check=True)
    events=defaultdict(list)
    for line in proc.stdout.splitlines():
        j=json.loads(line); v=j["e"]
        if isinstance(v,str): v=json.loads(v)
        if v.get("state")=="running" and v.get("target") and v.get("tool") in ("shell_run","repo_edit"):
            events[j["t"]].append((j["at"],v["tool"],v["target"]))
    pairs={}
    for t,(arm,iid) in task_for.items():
        seq=events.get(t,[])
        edits=[ts for ts,name,target in seq if name=="repo_edit" and not TEST_PATH.search(target)]
        checks=[(ts,target) for ts,name,target in seq if name=="shell_run" and TEST_COMMAND.search(target)]
        cut=min(edits) if edits else None
        before={target for ts,target in checks if cut and ts<cut}
        after={target for ts,target in checks if cut and ts>cut}
        pairs.setdefault(iid,{})[arm]={"task_id":t,"has_tool_targets":bool(seq),"first_non_test_repo_edit_observed":bool(cut),
          "test_invocations_before":len(before),"test_invocations_after":len(after),"visible_test_invocations":len(checks),
          "repeated_visible_test_command_hashes":[hashlib.sha256(s.encode()).hexdigest() for s in sorted(before&after)]}
    return {"checked_at":datetime.now(timezone.utc).isoformat(),
        "definition":"Identical visible shell command text containing a recognized test runner or assertion, observed before and after the first non-test repo_edit. This is neither proof of red-to-green nor evidence of no testing when absent. Shell-based edits and truncated commands can be missed.",
        "cases":pairs}
if __name__=="__main__":
    print(json.dumps(extract(Path(sys.argv[1]), Path(sys.argv[2]), "--partial" in sys.argv),indent=2))
