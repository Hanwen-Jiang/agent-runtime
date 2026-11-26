"""Build an isolated Mini 50 worker/API/gateway from B, with a shared frozen routing policy."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

import prepare_native_pilot as pilot
from inventory import routing_inputs

HOME_DIR = Path(__file__).resolve().parent
B = Path('/opt/swebench/cache/mini50-20261121-runtime-arm/freeze.json')
PACKAGE = '/app/services/py-worker/src/agentruntime_worker/'
STUDY = 'mini50-native-1123'
OWNER = 'agentruntime.io/experiment'
GATEWAY = 'ar-agent-runtime-model-gateway'


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data,str) else data).hexdigest()


def save_same(path, data):
    data=json.dumps(data, indent=2, sort_keys=True)+'\n'
    if path.exists():
        if path.read_text()!=data:
            raise ValueError(f'Frozen document {path.name} changed')
    else:
        path.write_text(data)


def frozen_config():
    """Bind to the actual gateway's source, then scope the policy copy to this tenant."""
    names, docs, allow, profiles = routing_inputs()
    raw = docs['model-profiles']['model-profiles.json']
    policy_source = docs['tenant-policies']['tenant-policies.json']
    policies = json.loads(policy_source)
    restricted = {**policies,'tenants':{'swebench':policies['tenants']['swebench']}}
    pol = json.dumps(restricted,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    assert {p['id'] for p in profiles['profiles']}==set(profiles['registry_revisions'])
    values={'model-profiles':{'model-profiles.json':raw},'tenant-policies':{'tenant-policies.json':pol}}
    cm={}
    for key,data in values.items():
        rev=digest(next(iter(data.values())))[:10]
        cm[key]={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':f'{STUDY}-{key}-{rev}',
                 'namespace':pilot.NAMESPACE,'labels':{OWNER:STUDY}},'immutable':True,'data':data}
    proof={'origin_gateway':GATEWAY,'origin_configmaps':names,'catalog_sha256':digest(raw),
           'origin_policy_sha256':digest(policy_source),'isolated_policy_sha256':digest(pol),
           'models':len(profiles['profiles']),'swebench_allowlist':allow,
           'capability_measurements':{p['id']:p['capability_scores'] for p in profiles['profiles'] if p.get('capability_scores')},
           'configmaps':{k:v['metadata']['name'] for k,v in cm.items()}}
    return list(cm.values()),proof


def workloads(arm, config_names, overlay):
    experiment=f'{STUDY}-{arm}'
    docs=pilot.resources(experiment=experiment,temporal=experiment)
    extra=[]
    for d in docs:
        if d['kind']!='Deployment':
            continue
        pod=d['spec']['template']['spec']
        c,=pod['containers']
        c['env']=[e for e in c.get('env',[]) if e['name']!='AR_MODEL_ROUTING_OVERRIDES_FILE']
        c['volumeMounts']=[m for m in c.get('volumeMounts',[]) if m['name']!='route-lock']
        pod['volumes']=[v for v in pod.get('volumes',[]) if v['name']!='route-lock']
        for v in pod['volumes']:
            if v['name'] in config_names and 'configMap' in v:
                v['configMap']['name']=config_names[v['name']]
        if not overlay or d['metadata']['name'].removeprefix(experiment+'-') not in pilot.POOLS:
            continue
        for rel,source in overlay.items():
            if not re.fullmatch(r'[a-z_]+/[a-z_]+\.py',rel): raise ValueError('Invalid worker module')
            basename=Path(rel).name
            digest10=digest(source)[:10]
            cm_name=f'{experiment}-code-{digest10}'
            name='code-'+digest10
            target=PACKAGE+rel
            c['volumeMounts']=[m for m in c['volumeMounts'] if m['mountPath']!=target]
            c['volumeMounts'].append({'name':name,'subPath':basename,'mountPath':target,'readOnly':True})
            pod['volumes'].append({'name':name,'configMap':{'name':cm_name}})
            if not any(x['metadata']['name']==cm_name for x in extra):
                extra.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':cm_name,'namespace':pilot.NAMESPACE,
                             'labels':{OWNER:experiment}},'immutable':True,'data':{basename:source}})
    return extra+docs


def validate_matrix(path):
    if not path.is_file():
        raise ValueError('Matrix completion receipt is required before deployment or model calls')
    doc=json.loads(path.read_bytes())
    if not (doc.get('closed_positions')==doc.get('planned')==3369 and doc.get('roles')==5
            and doc.get('planned_cells')==27 and doc.get('quota_or_missing')==0
            and re.fullmatch(r'[0-9a-f]{64}',doc.get('source_sha256',''))):
        raise ValueError('Expect the five-role, 27-cell final adjudication and its evidence digest')
    return doc


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['check','deploy'])
    p.add_argument('arm',choices=['b','d'])
    p.add_argument('--freeze',type=Path,default=B,help='B frozen tree or the reviewed D freeze with overlay files')
    p.add_argument('--matrix-receipt',type=Path,default=HOME_DIR/'matrix-release.json')
    args=p.parse_args()
    frozen=json.loads(args.freeze.read_bytes())
    extra=frozen.get('overlays',{})
    if args.arm=='d' and not extra: raise ValueError('A tuned candidate requires frozen worker modules')
    overlay={rel:(args.freeze.parent/local).read_text() for rel,local in extra.items()}
    if any(digest(body)!=frozen['candidate_files'][rel] for rel,body in overlay.items()):raise ValueError('Worker patch differs from the declared digest')
    if args.arm=='b':
        configs,proof=frozen_config()
    else:
        proof=json.loads((HOME_DIR/'routing-freeze.json').read_bytes())
        configs=[]
        for k,v in proof['configmaps'].items():
            original=pilot.get('cm',v)
            h=proof['catalog_sha256' if k=='model-profiles' else 'isolated_policy_sha256']
            if original.get('immutable') is not True or digest(next(iter(original['data'].values())))!=h:
                raise ValueError('Shared B routing snapshot differs')
    docs=configs+workloads(args.arm,proof['configmaps'],overlay)
    docs.sort(key=lambda d:{'ConfigMap':0,'NetworkPolicy':1,'Service':2,'Deployment':3}[d['kind']])
    if args.action=='deploy':validate_matrix(args.matrix_receipt)
    for d in docs:
        expected=d['metadata']['labels'][OWNER]
        previous=pilot.kub('get',d['kind'],d['metadata']['name'],'--ignore-not-found','-o','json')
        if previous and json.loads(previous)['metadata']['labels'].get(OWNER)!=expected:raise ValueError('Resource belongs to another experiment')
        subprocess.run(['kubectl','-n',pilot.NAMESPACE,'apply','--dry-run=server','-f','-'],text=True,input=json.dumps(d),check=True,stdout=subprocess.DEVNULL)
    if args.action=='check':
        print(json.dumps({'source':proof['origin_configmaps'],'models':proof['models'],'allowed':len(proof['swebench_allowlist']),
                         'catalog_sha256':proof['catalog_sha256'],'objects_checked':len(docs),'model_calls':0,'applied':False}))
        return
    if args.arm=='b':save_same(HOME_DIR/'routing-freeze.json',proof)
    temporal=['kubectl','-n','data','exec','deploy/temporal-admintools','--','temporal','operator','namespace']
    addr=['--namespace',f'{STUDY}-{args.arm}','--address','temporal-frontend:7233']
    if subprocess.run(temporal+['describe',*addr],capture_output=True).returncode:
        subprocess.run(temporal+['create',*addr,'--retention','7d'],check=True)
    for d in docs:
        subprocess.run(['kubectl','-n',pilot.NAMESPACE,'apply','-f','-'],text=True,input=json.dumps(d),check=True)
    for pool in ('gw','api',*pilot.POOLS):
        subprocess.run(['kubectl','-n',pilot.NAMESPACE,'rollout','status','deploy/'+f'{STUDY}-{args.arm}-{pool}','--timeout=180s'],check=True)
    print('Frozen native deployment ready',args.arm,frozen['candidate_tree'])


if __name__=='__main__':
    main()
