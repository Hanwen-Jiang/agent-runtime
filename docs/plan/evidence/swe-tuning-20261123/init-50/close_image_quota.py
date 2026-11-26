"""Close this experiment's image registrations; keep registry blobs and originals."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess

BASE=Path('/opt/swebench/cache/swe-tuning-20261123/init-50')
RUNS=Path('/opt/swebench/runs')
INIT=RUNS/'init50-gpt62-tuned-20261123'
MINI=RUNS/'mini50-tuned-c-gpt62-20261123'
def read(p):
    return json.loads(p.read_bytes())
def db(sql):
    return subprocess.check_output(['kubectl','-n','data','exec','-i','pg-1','-c','postgres','--','psql','-XqAt','-v','ON_ERROR_STOP=1','-d','agentruntime'],input=sql,text=True).strip()
def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def verify_complete(run):
    manifest=read(run/'run.json'); complete=read(run/'report.json')
    f,=(run/'grading').glob('*.json');grade=read(f);ids=set(manifest['instance_ids'])
    assert complete['graded'] and len(ids)==50 and ids==set(grade['submitted_ids'])
    assert ids==set(grade['completed_ids'])|set(grade.get('empty_patch_ids',[]))
def counts():
    return json.loads(db("select coalesce(json_object_agg(state,n),'{}') from (select state,count(*) n from sandbox_images where tenant_id='swebench' group by 1)a;"))
def main(commit):
    verify_complete(INIT);verify_complete(MINI)
    cohort=read(BASE/'preflight-inventory.json'); exclusive=[r for r in cohort if r['db_state']=='deleted']
    assert len(exclusive)==44 and sum(r['db_state']=='ready' for r in cohort)==6
    since=read(INIT/'run.json')['created_at']
    assert re.fullmatch(r'[0-9T:+.Z-]+',since)
    report={'at':stamp(),'mode':'apply' if commit else 'preview','counts_before':counts(),'cases':[]}
    for r in exclusive:
        iid=r['id'];key=r['key']; assert re.fullmatch('[0-9a-f]{64}',key)
        proof_file=INIT/'prewarm'/(iid+'.json');proof=read(proof_file)
        assert proof['instance_id']==iid and proof['ready'] and proof['released'] and proof['definition_key']==key
        image=proof['registry_image'];assert re.fullmatch(r'127\.0\.0\.1:30500/envs/swebench/env@sha256:[0-9a-f]{64}',image)
        ws=proof['workspace'];assert re.fullmatch('[a-z0-9-]+',ws)
        where=f"tenant_id='swebench' and image_key='{key}'"
        current=json.loads(db(f"select to_json(s) from (select image_key,state,image,digest,workspace_id,size_bytes,reason,ready_at,deleted_at,last_used_at,build_ms from sandbox_images where {where})s;"))
        leases=f"tenant_id='swebench' and image='{image}'"
        refs=json.loads(db(f"select coalesce(json_agg(s),'[]') from (select workspace_id,state,count(*) n from sandbox_envs where {leases} and created_at >= '{since}' group by 1,2 order by 1,2) s;"))
        entry={'instance_id':iid,'key':key,'historical_deleted_image':r['db_image'],'verified_run_image':image,'before':current,'references':refs,'prewarm_sha256':sha256(proof_file.read_bytes()).hexdigest()}
        owned=all(v['workspace_id']==ws and v['state'] in ('destroyed','failed','lost') for v in refs)
        active=db(f"select count(*) from sandbox_envs where {leases} and state not in ('destroyed','failed','lost');")!='0'
        if current['state']=='deleted':
            entry['status']='already_released'
        elif not owned or active or current['state']!='ready' or current['image']!=image or current['digest']!=image.split('@')[1]:
            entry['status']='retained_changed_or_in_use'
        elif not commit:
            entry['status']='eligible'
        else:
            now=stamp()
            updated=db(f"""update sandbox_images set state='deleted',deleted_at='{now}' where {where} and state='ready' and image='{image}' and digest='{image.split('@')[1]}'
               and not exists(select 1 from sandbox_envs where {leases} and (state not in ('destroyed','failed','lost') or (created_at>='{since}' and workspace_id<>'{ws}'))) returning image_key;""")
            entry['status']='released' if updated==key else 'retained_changed_or_in_use'
        report['cases'].append(entry)
        if commit:
            with (BASE/'quota-close-journal.jsonl').open('a') as dest:
                print(json.dumps({'at':stamp(),**entry}),file=dest)
    report['counts_after']=counts()
    print(json.dumps(report,indent=2))
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');main(parser.parse_args().apply)
