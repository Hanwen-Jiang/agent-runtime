"""Project public tool-operation metadata for a graded example; no hidden tests or output content."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess

CHECK = re.compile(r"(?:\b(?:py\.test|pytest|unittest|runtests\.py|tox|nose|test_suite)\b|\bpython(?:[0-9.]*)?\s+-c\b)")
def read(p):
    return json.loads(p.read_bytes())
def main(run, iid):
    manifest=read(run/'run.json')
    if iid not in manifest['instance_ids']:
        raise ValueError("case outside this cohort")
    rec=read(run/'records'/(iid+'.json'))
    tid=rec['task_id']
    assert re.fullmatch('tsk_[0-9A-Z]+',tid)
    baseline=read(Path('/opt/swebench/cache/swe-tuning-20261123/init-50/baseline.json'))
    prior=next(r for r in baseline['instances'] if r['instance_id']==iid)
    f=run/'grading/logs/run_evaluation'/manifest['run_id']/manifest['settings']['model_name']/iid/'report.json'
    result=read(f)[iid]
    query=f"begin read only;select data::json->'payload'->'value' from task_events where tenant_id='swebench' and task_id='{tid}' and event_type='tool_status' order by event_id;commit;"
    command=['kubectl','-n','data','exec','-i','pg-1','-c','postgres','--','psql','-XqAt','-d','agentruntime']
    events=[json.loads(v) for v in subprocess.check_output(command,input=query.encode()).splitlines() if v]
    actions=[a for a in events if a.get('state')=='running']
    first=next((n for n,a in enumerate(actions) if a.get('tool')=='repo_edit'),None)
    checks=[]
    for n,event in enumerate(actions):
        target=event.get('target','')
        if event.get('tool')!='shell_run' or not CHECK.search(target):
            continue
        checks.append({'tool_sequence':n+1,'relative_to_first_repo_edit':('unknown' if first is None else ('before' if n<first else 'after')),
                       'round_seq':event.get('round_seq'),'target_sha256':sha256(target.encode()).hexdigest(),
                       'command_type':'repository_test_command' if ' -c ' not in target else 'inline_python_check'})
    before={v['target_sha256'] for v in checks if v['relative_to_first_repo_edit']=='before'}
    after={v['target_sha256'] for v in checks if v['relative_to_first_repo_edit']=='after'}
    print(json.dumps({'instance_id':iid,'task_id':tid,'prior':prior['grading'],'new_resolved':result['resolved'],
           'terminal':rec['status'],'patch_files':rec.get('patch_files'),'rounds':rec['rounds'],
           'tool_counts':dict(Counter(v['tool'] for v in actions)),'first_repo_edit_sequence':None if first is None else first+1,
           'checks':checks,'equal_recorded_shell_targets_across_first_repo_edit':sorted(before & after),
           'official_tests':{k:{g:len(v) for g,v in result['tests_status'][k].items()} for k in ('FAIL_TO_PASS','PASS_TO_PASS')},
           'interpretation':'Matching targets are observed invocations; tool_status does not contain test stdout or a process exit code.'},indent=2))
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('run',type=Path);parser.add_argument('instance_id');args=parser.parse_args();main(args.run,args.instance_id)
