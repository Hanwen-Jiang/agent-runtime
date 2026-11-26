"""Read evaluator inputs, ready environments and live routing configuration. No model calls."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

CACHE = Path('/opt/swebench/cache/mini50-20261121-runtime-arm')
RUNS = Path('/opt/swebench/runs')
SOURCE = RUNS / 'mini50-tuned-c-gpt62-20261123'
DATASET = Path('/opt/swebench/data/verified.jsonl')
COHORT = '594827f9dcc5c06dded01fc66872ceedb4821e30b8cd8e6484f1ace5cea65331'
DATA = '2d336d3f60d2a6496b21cf04ab73ed6cdd016eb52f19e1e4bb0328a278145d52'


def read(path):
    return json.loads(path.read_bytes())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def kub(kind, name, namespace='agent-runtime'):
    ns = ['-n', namespace] if namespace else []
    return json.loads(subprocess.check_output(['kubectl', *ns, 'get', kind, name, '-o', 'json']))


def routing_inputs():
    gw = kub('deploy', 'ar-agent-runtime-model-gateway')
    spec = gw['spec']['template']['spec']
    cm_names = {v['name']: v['configMap']['name'] for v in spec['volumes'] if 'configMap' in v}
    names = {k:cm_names[k] for k in ('model-profiles','tenant-policies')}
    doc = {k:kub('cm', name)['data'] for k,name in names.items()}
    profiles = json.loads(doc['model-profiles']['model-profiles.json'])
    policies = json.loads(doc['tenant-policies']['tenant-policies.json'])
    allow = policies['tenants']['swebench']['model_allowlist']
    assert len(allow) > 1 and set(allow) <= {p['id'] for p in profiles['profiles']}
    return names, doc, allow, profiles


def main():
    raw = (CACHE/'mini50.ids').read_bytes()
    assert sha(raw) == COHORT and sha(DATASET.read_bytes()) == DATA
    ids = raw.decode().splitlines()
    assert len(set(ids)) == len(ids) == 50
    rows = [read(SOURCE/'prewarm'/f'{iid}.json') for iid in ids]
    assert all(r['ready'] and r['released'] and r['instance_id']==iid for r,iid in zip(rows,ids))
    keys = [r['definition_key'] for r in rows]
    assert len(set(keys))==50 and all(re.fullmatch('[a-f0-9]{64}',k) for k in keys)
    quoted = ','.join("'" + k + "'" for k in keys)
    query = "BEGIN READ ONLY;SELECT image_key,state,image,digest FROM sandbox_images WHERE tenant_id='swebench' AND image_key IN ("+quoted+");COMMIT;"
    raw_rows = subprocess.check_output(['kubectl','-n','data','exec','-i','pg-1','-c','postgres',
               '--','psql','-qAt','-v','ON_ERROR_STOP=1','-d','agentruntime'],input=query.encode()).decode()
    db = {fields[0]:fields[1:] for line in raw_rows.splitlines() if len(fields:=line.split('|'))==4}
    volume=kub('pvc','ar-agent-runtime-env-registry')['spec']['volumeName']
    registry=Path(kub('pv',volume,None)['spec']['local']['path'])/'docker/registry/v2'
    by_case={}
    for iid,key,r in zip(ids,keys,rows):
        state, image, digest = db[key]
        assert re.fullmatch('sha256:[a-f0-9]{64}',digest)
        h=digest[7:]
        link=registry/'repositories/envs/swebench/env/_manifests/revisions/sha256'/h/'link'
        blob=registry/'blobs/sha256'/h[:2]/h/'data'
        by_case[iid] = {'definition_key':key, 'image':image,
            'ready_and_pullable':state=='ready' and image==r['registry_image'] and image.endswith('@'+digest) and
            link.is_file() and link.read_text().strip()==digest and blob.is_file() and sha(blob.read_bytes())==h}
    names, docs, allow, profiles=routing_inputs()
    node=kub('node',subprocess.check_output(['hostname','-f']).decode().strip(),None)
    print(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),'cohort_sha256':COHORT,'dataset_sha256':DATA,
        'host_free_gib':round(shutil.disk_usage('/').free/2**30,3),
        'node_disk_pressure':next(c['status'] for c in node['status']['conditions'] if c['type']=='DiskPressure'),
        'per_case':by_case,'ready_environment_count':sum(v['ready_and_pullable'] for v in by_case.values()),
        'current_configmaps':names,'catalog_sha256':sha(docs['model-profiles']['model-profiles.json'].encode()),
        'policy_sha256':sha(docs['tenant-policies']['tenant-policies.json'].encode()),
        'registry_profiles':len(profiles['profiles']), 'tenant_allowlist':allow,
        'measured_scores_by_role':{role:sum(c['role']==role for p in profiles['profiles'] for c in p.get('capability_scores',[]))
            for role in sorted({c['role'] for p in profiles['profiles'] for c in p.get('capability_scores',[])})}},indent=2))


if __name__=='__main__':
    main()
