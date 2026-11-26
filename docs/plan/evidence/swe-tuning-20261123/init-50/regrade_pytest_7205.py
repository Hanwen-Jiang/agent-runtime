"""Independently replay the archived prediction once in an empty official harness directory."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
BASE=Path('/opt/swebench/cache/swe-tuning-20261123')
sys.path.insert(0,str(BASE))
from stream_grade import grade_one
run=Path('/opt/swebench/runs/init50-gpt62-tuned-20261123')
iid='pytest-dev__pytest-7205'
dest=BASE/'init-50/independent-7205'/run.name
def read(p):return json.loads(p.read_bytes())
def h(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def capture(out):
    f=out/'stream'/iid/'receipt.json'
    receipt=read(f); summary=read(f.parent/(receipt['model']+'.'+run.name+'.json'))
    log=out/'grading/logs/run_evaluation'/run.name/receipt['model']/iid
    raw=read(log/'report.json')[iid]
    lines=(log/'test_output.txt').read_text(errors='replace').splitlines()
    tests=raw['tests_status']
    # Retain only the matched marker and the outermost run totals, without test inputs.
    nested=[{'line':n,'matched':m.group(0)} for n,l in enumerate(lines,1) if (m:=re.search(r'(?i)(no tests ran|collected 0 items?)',l))]
    outer=[{'line':n,'text':l.strip()[:165]} for n,l in enumerate(lines,1) if re.match(r'^=+ .*\d+ failed,.*\d+ passed',l)]
    return {'patch_sha256':receipt['patch_sha256'],'docker_host':receipt.get('docker_host','unix:///var/run/docker.sock'),
            'docker_image_id':receipt['used_image']['image_id'],'image_repo_digests':receipt['used_image']['repo_digests'],
            'completed':iid in summary['completed_ids'],'unresolved':iid in summary['unresolved_ids'],
            'ambiguous_failure':iid in summary['ambiguous_failure_ids'],'reason':summary['failure_reasons'].get(iid),
            'test_counts':{k:{name:len(cases) for name,cases in tests[k].items()} for k in ('FAIL_TO_PASS','PASS_TO_PASS')},
            'outer_summary':outer[-1:],'embedded_no_tests_markers':nested[:12],
            'source_sha256':{'official_summary':h(f.parent/(receipt['model']+'.'+run.name+'.json')),'official_case_report':h(log/'report.json'),'stdout':h(log/'test_output.txt'),'official_eval_script':h(log/'eval.sh')}}
def main():
    assert os.getenv('DOCKER_HOST','unix:///var/run/docker.sock')=='unix:///var/run/docker.sock'
    model=read(run/'run.json')['settings']['model_name']; rec=read(run/'records'/(iid+'.json'))
    assert rec['state']=='finished'
    dest.mkdir(exist_ok=True,parents=True)
    with (BASE/'grader-host.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not (dest/'stream'/iid/'receipt.json').exists():
            assert not (dest/'grading/logs').exists(), "an incomplete previous independent invocation requires inspection"
            for name in ('instances.jsonl','run.json'):
                if not (dest/name).exists():(dest/name).symlink_to(run/name)
            if not grade_one(dest,iid,rec,model):raise RuntimeError('no completed verdict on the independent replay')
    first=capture(run); second=capture(dest)
    for key in ('patch_sha256','docker_host','docker_image_id','completed','unresolved','ambiguous_failure','reason','test_counts'):
        assert first[key]==second[key],key
    assert first['source_sha256']['official_eval_script']==second['source_sha256']['official_eval_script']
    assert first['outer_summary'] and second['outer_summary'] and first['embedded_no_tests_markers'] and second['embedded_no_tests_markers']
    print(json.dumps({'iid':iid,'generation_attempts_added':0,'fresh_official_evaluations':1,'original':first,'independent_replay':second},indent=2))
if __name__=='__main__':main()
