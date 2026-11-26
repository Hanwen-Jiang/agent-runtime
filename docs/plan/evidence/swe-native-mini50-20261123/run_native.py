"""Run the original public 50 in fixed order. Only the native kernel selects models and tiers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading

from deploy_native import STUDY, validate_matrix
from inventory import COHORT, DATA, sha
from run_paired_arm import sb, token_from_env
from run_quota_recovery import RecoveryClient
from run_routing_pilot import env, locked_output, POOLS, ready_pod, resource, snapshot

ALLOWED = ('tenant_id','task_id','node_id','role','method','model_profile_id','tier','vendor','input_tokens',
           'output_tokens','cache_read_tokens','cost_usd_micros','status','error_code','usage_missing',
           'registry_scope','registry_revision','registry_document_sha256')
HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_bytes())


def watch(client, prefix, start, stop):
    """Record routing from the dedicated gateway and halt new work at the first quota error."""
    since=start
    output=client.out/'gateway-meter.jsonl'
    events=set()
    if output.exists():
        events={json.loads(line)['event'] for line in output.open() if line.strip()}
    cancelled=set()
    while True:
        closing=stop.wait(3)
        try:
            result=subprocess.run(['kubectl','-n','agent-runtime','logs','deploy/'+prefix+'-gw',
                                   '--timestamps=true','--since-time='+since],capture_output=True,text=True,timeout=30)
        except (OSError,subprocess.TimeoutExpired) as exc:
            print('Gateway evidence read pending',type(exc).__name__,flush=True)
            if closing: return
            continue
        if result.returncode:
            if closing: return
            continue
        with output.open('a',encoding='utf8') as f:
            for line in result.stdout.splitlines():
                at,_,raw=line.partition(' ')
                # Errors from unrelated pods cannot trip this arm's admissions.
                if not at[:4].isdigit():
                    continue
                since=at
                try:
                    row=json.loads(raw)
                except ValueError:
                    row=None
                quota=False
                if isinstance(row,dict) and row.get('tenant_id')=='swebench':
                    quota=row.get('error_code')=='insufficient_user_quota'
                    if row.get('task_id') and row.get('model_profile_id'):
                        key=hashlib.sha256(line.encode()).hexdigest()
                        if key not in events:
                            events.add(key)
                            print(json.dumps({'event':key,'at':at,**{k:row[k] for k in ALLOWED if k in row}},separators=(',',':')),file=f)
                elif row is None:
                    quota=bool(re.search(r'\bprofile=\S+ .*?\bcode=insufficient_user_quota\b',raw))
                if quota and not client.stop_path.exists():
                    client.stop_path.write_text(json.dumps({'at':at,'code':'insufficient_user_quota','gateway':prefix+'-gw'})+'\n')
                    print('Provider quota insufficient; collection paused',flush=True)
        if client.stop_path.exists() or (client.out/'provider-quota-pause.json').exists():
            for r in sb.read_records(client.out).values():
                tid=r.get('task_id')
                if tid and not r.get('task_finished') and tid not in cancelled:
                    try:
                        client.cancel(tid,'provider_insufficient_user_quota')
                        cancelled.add(tid)
                    except Exception as exc:
                        print('Cancellation pending',tid,type(exc).__name__,flush=True)
        if closing: return


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('arm',choices=('b','d'))
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--freeze',required=True,type=Path)
    p.add_argument('--prewarm',required=True,type=Path)
    p.add_argument('--reference',type=Path,help='The native B route protocol (B must already be graded before D).')
    p.add_argument('--concurrency',type=int,default=2)
    p.add_argument('--ids',type=Path,default=Path('/opt/swebench/cache/mini50-20261121-runtime-arm/mini50.ids'))
    p.add_argument('--dataset',type=Path,default=Path('/opt/swebench/data/verified.jsonl'))
    p.add_argument('--gate',type=Path,default=HERE/'matrix-release.json')
    a=p.parse_args()
    validate_matrix(a.gate)
    if not a.prewarm.is_file() or sha(a.ids.read_bytes())!=COHORT or sha(a.dataset.read_bytes())!=DATA:
        raise ValueError('Dataset, frozen issue IDs or real project prewarm differs')
    if a.arm=='d' and not a.reference: p.error('D needs the native B reference protocol')
    ids=a.ids.read_text().splitlines()
    cases=sb.select(sb.load_instances(a.dataset),ids=ids)
    assert len(ids)==len(set(ids))==50 and [i.instance_id for i in cases]==ids
    prefix=STUDY+'-'+a.arm
    frozen=read(a.freeze)
    conditions,api_ip,routes=snapshot(prefix, frozen, 'native')
    for workload in (*POOLS,'api','gw'):
        pod=ready_pod(resource('deploy',prefix+'-'+workload)['spec']['selector']['matchLabels'])
        if 'AR_MODEL_ROUTING_OVERRIDES_FILE' in env(pod) or any(v['name']=='route-lock' for v in pod['spec']['volumes']):
            raise ValueError(f'Found fixed routing configuration in {workload}')
    selected_config=read(HERE/'routing-freeze.json')
    if (routes or conditions['catalog_sha256']!=selected_config['catalog_sha256'] or
            conditions['policy_sha256']!=selected_config['isolated_policy_sha256']):
        raise ValueError('Live router differs from the frozen automatic policy')
    settings=sb.Settings(run_id=a.out.name,model_name='agentruntime-'+conditions['runtime_tree'][:8],
                         budget_usd=150.0,task_usd_limit=3.0,concurrency=a.concurrency,task_timeout_s=3600,
                         hints=False,environment='official',poll_s=10)
    fixed={k:v for k,v in conditions.items() if k not in ('runtime_tree','candidate_files')}
    fixed.update(cohort_sha256=COHORT, dataset_sha256=DATA, matrix_gate_sha256=sha(a.gate.read_bytes()),
                 concurrency=a.concurrency, task_usd_limit=3.0, timeout_s=3600,hints=False,environment='official',
                 prewarm_sha256=sha(a.prewarm.read_bytes()),single_generation_attempt=True)
    if a.reference:
        before=read(a.reference)
        if before['arm']!='b' or before['shared']!=fixed or before['run_id']==a.out.name or before['mode']!='native':
            raise ValueError('Candidate must preserve the native B comparison conditions')
    protocol={'run_id':a.out.name,'mode':'native','arm':a.arm,'shared':fixed,'runtime_tree':conditions['runtime_tree'],
              'candidate_files':conditions['candidate_files'],'requested_routes':routes,'instance_ids':ids}
    select={'dataset_name':'SWE-bench/SWE-bench_Verified','dataset_split':'test','population_count':500,'ids':True,
            'ids_sha256':COHORT,'dataset_sha256':DATA,'instances_sha256':DATA,'runtime_batch':'mini-50',
            'runtime_tree':conditions['runtime_tree'],'method_description':'Frozen Verified Mini 50 (25 Django, 25 Sphinx)',
            'reference_url':'https://github.com/MariusHobbhahn/SWEBench-verified-mini/blob/1ce432065d9ed226017ef1ac88b5f91818383c64/data/subsets/size_optimized_sample_ids.json'}
    with locked_output(a.out):
        path=a.out/'route-protocol.json'
        if path.exists() and read(path)!=protocol: raise ValueError('Resume requires the same native runtime and route snapshot')
        if not path.exists(): path.write_text(json.dumps(protocol,indent=2)+'\n')
        if any((a.out/n).exists() for n in ('provider-quota-pause.json','quota-stop.json')):
            return 75
        shared=resource('svc','ar-agent-runtime-task-service')['spec']['clusterIP']
        client=RecoveryClient(f'http://{api_ip}:8080',token_from_env(),a.out,a.prewarm,
                              {sb.workspace_id(a.out.name,i):i for i in ids},diff_api=f'http://{shared}:8080')
        start=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        done=threading.Event()
        monitor=threading.Thread(target=watch,args=(client,prefix,start,done),daemon=True)
        monitor.start()
        try:
            result=sb.run(client,cases,a.out,settings,selection=select)
        except sb.ProviderQuotaPaused:
            return 75
        finally:
            done.set()
            monitor.join(timeout=35)
        print(result,flush=True)
        return 75 if client.stop_path.exists() else int(bool(result.errors or result.not_run))


if __name__=='__main__':
    raise SystemExit(main())
