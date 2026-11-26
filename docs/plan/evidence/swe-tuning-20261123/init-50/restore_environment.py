"""Re-register a verified prior build without rebuilding it or generating a model answer."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT=Path("/opt/swebench/cache/swe-tuning-20261123")
HERE=ROOT/"init-50"
CACHE=ROOT/"recoverable-swe-docker-cache"
CAS=Path("/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256")
WORKER=["kubectl","-n","agent-runtime","exec","-i","deploy/swe50-1123-c-program","--","python","-c"]
PG=["kubectl","-n","data","exec","-i","pg-1","-c","postgres","--","psql","-XqAt","-v","ON_ERROR_STOP=1","-d","agentruntime"]
GUARD=56 * 2**30
CONTROLS=HERE/"min-free-gib"

def read(path):return json.loads(path.read_bytes())
def pg(sql):return subprocess.check_output(PG,input=sql,text=True).strip()
def case_state(key):
    row=pg(f"select json_build_object('state',state,'digest',digest,'image',image,'ready_at',ready_at,'deleted_at',deleted_at,'last_used_at',last_used_at) from sandbox_images where tenant_id='swebench' and image_key='{key}';")
    return json.loads(row) if row else None

def source(digest):
    assert re.fullmatch('sha256:[a-f0-9]{64}',digest)
    paths=(CAS/digest[7:],CACHE/'blobs'/digest[7:])
    return next((p for p in paths if p.is_file()),None)

def main(iid, *, minimum_free=None):
    cohort={r['id']:r for r in read(HERE/'preflight-inventory.json')}
    row=cohort[iid]; key=row['key']; assert re.fullmatch('[a-f0-9]{64}',key)
    if row['db_state']!='deleted':
        return
    initial=case_state(key)
    if not initial or initial['state']!='deleted':
        return
    archive=read(CACHE/'inventory.json')[key[:32]]
    root=archive['root']
    assert initial['image']==row['db_image']=='127.0.0.1:30500/envs/swebench/env@'+root
    assert initial['digest']==root
    indexes=read(ROOT/'grader-layer-index.json')
    expected=next(v for v in indexes.values() if v['image']==row['image'])
    descriptions=archive['descriptors']
    index=read(source(root))
    linux,=[v for v in index['manifests'] if v.get('platform')=={'architecture':'amd64','os':'linux'}]
    manifest=read(source(linux['digest']))
    assert [o['digest'] for o in manifest['layers'][:len(expected['layers'])]]==[o['digest'] for o in expected['layers']]
    objects=[{**d,'is_manifest':('manifest' in d['mediaType'] or 'index' in d['mediaType'])} for d in descriptions.values()]
    objects.sort(key=lambda d:(d['is_manifest'],d['digest']==root))
    client=WORKER+[(HERE/'publish_cached_oci.py').read_text()]
    plan={'root':root,'tag':key[:32],'objects':objects}
    needed=json.loads(subprocess.check_output([*client,'missing'],input=json.dumps(plan).encode()+b'\n'))
    objects=[d for d in objects if d['digest'] in needed]
    paths=[]
    for d in objects:
        f=source(d['digest'])
        if not f or f.stat().st_size!=d['size']:
            raise ValueError('cached descriptor unavailable: '+d['digest'])
        paths.append(f)
    size=sum(d['size'] for d in objects)
    floor=int(CONTROLS.read_text().strip())*2**30 if CONTROLS.exists() else GUARD
    if minimum_free is not None:
        floor=minimum_free
    if shutil.disk_usage('/').free - size<floor:
        print('waiting for disk reserve',iid,size,flush=True)
        return
    held=[]
    try:
        held=[p.open('rb') for p in paths]
        with subprocess.Popen([*client,'publish'],stdin=subprocess.PIPE,stdout=subprocess.PIPE) as proc:
            proc.stdin.write(json.dumps({**plan,'objects':objects}).encode()+b'\n')
            for f in held:
                shutil.copyfileobj(f,proc.stdin,1<<20)
            proc.stdin.close()
            result=json.loads(proc.stdout.read())
            assert proc.wait()==0 and result.get('digest')==root and result.get('registry_verified') is True
    finally:
        for f in held:f.close()
    # Restore only the same deleted snapshot. A concurrently started native build owns its row.
    stamp=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    sql=f"""UPDATE sandbox_images SET state='ready',reason='',deleted_at='',last_used_at='{stamp}'
        WHERE tenant_id='swebench' AND image_key='{key}' AND state='deleted' AND digest='{root}'
          AND image='127.0.0.1:30500/envs/swebench/env@{root}'
          AND (SELECT count(*) FROM sandbox_images WHERE tenant_id='swebench' AND state IN ('ready','building')) <94
        RETURNING image_key;"""
    restored=pg(sql)==key
    event={'at':stamp,'instance_id':iid,'definition_key':key,'prior':initial,'historical_root':root,
           'official_parent':row['image'],'platform_manifest':linux['digest'],'official_layer_count':len(expected['layers']),
           'sent_bytes':size,'registry_verified':True,'restored_deleted_row':restored}
    with (HERE/'restores.jsonl').open('a') as out:print(json.dumps(event),file=out)
    print(iid,'restored',restored,'transferred',size,'root',root,flush=True)
if __name__=='__main__':
    main(sys.argv[1])
