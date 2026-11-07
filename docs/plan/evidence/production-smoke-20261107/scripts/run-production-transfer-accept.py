"""Host orchestration for the prepared final-deployment-only transfer acceptance."""
import argparse,hashlib,json,os,re,shlex,stat,subprocess,time
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--execute-after-final-deployment',action='store_true',required=True)
parser.add_argument('--expected-worker-image',required=True)
parser.add_argument('--expected-sandbox-image',required=True)
parser.add_argument('--worker-deployment',default='ar-agent-runtime-worker-program')
parser.add_argument('--expected-fc-manager-sha256',required=True)
parser.add_argument('--script',type=Path,default=Path('/root/proj/agent-projs/.goal-tools/production-transfer-accept.py'))
parser.add_argument('--out',required=True)
args=parser.parse_args()
assert re.fullmatch(r'ar-agent-runtime-worker-(?:program|model|task|workspace|decision)',args.worker_deployment)
assert re.fullmatch(r'[0-9a-f]{64}',args.expected_fc_manager_sha256)
with Path('/usr/local/bin/fc-manager').open('rb') as stream:
    assert hashlib.file_digest(stream,'sha256').hexdigest()==args.expected_fc_manager_sha256,'unexpected production manager binary'
env={**os.environ,'KUBECONFIG':'/etc/rancher/k3s/k3s.yaml'}
base=['kubectl','-n','agent-runtime']
def kube(*cmd):return json.loads(subprocess.check_output([*base,*cmd,'-o','json'],env=env))
images={}
for name,expected in [(args.worker_deployment,args.expected_worker_image),('ar-agent-runtime-sandbox-service',args.expected_sandbox_image)]:
    deployment=kube('get','deployment',name)
    status=deployment['status'];desired=deployment['spec'].get('replicas',1)
    assert status.get('observedGeneration')==deployment['metadata']['generation']
    assert status.get('updatedReplicas')==desired and status.get('availableReplicas')==desired
    image=deployment['spec']['template']['spec']['containers'][0]['image']
    assert image==expected,(name,image,expected)
    images[name]=image
    if name=='ar-agent-runtime-sandbox-service':
        values={v['name']:v.get('value') for v in deployment['spec']['template']['spec']['containers'][0]['env']}
        assert values.get('AR_WORKSPACE_CHUNKED_SNAPSHOTS')=='true','chunk writing must be enabled after the compatible reader rollback point'
backends=json.loads(kube('get','configmap','ar-agent-runtime-sandbox-backends')['data']['backends.json'])
fc=backends['backends']['firecracker']
assert fc.get('enabled',True) and fc['kind']=='firecracker'
assert args.script.resolve().parent==Path('/root/proj/agent-projs/.goal-tools'),'acceptance script must belong to the prepared tool directory'
source=args.script.read_bytes()
out=Path(args.out);out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
(out/'preflight.json').write_text(json.dumps({'images':images,'manager_sha256':args.expected_fc_manager_sha256,'script':str(args.script),'source_sha256':hashlib.sha256(source).hexdigest(),'started_unix':time.time()},indent=2)+'\n')
with (out/'pod-output.log').open('wb') as log:
    result=subprocess.run([*base,'exec','-i','deployment/'+args.worker_deployment,'--','env','AR_TRANSFER_ACCEPT_EXECUTE=1','AR_TRANSFER_BACKEND=firecracker','AR_TRANSFER_FC_URL='+fc['manager_url'],'python','-'],input=source,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
report=None
for line in (out/'pod-output.log').read_text().splitlines():
    try:
        value=json.loads(line)
        if isinstance(value,dict) and value.get('tenant','').startswith('transfer-accept-') and 'cleanup' in value:report=value
    except (ValueError,TypeError):pass
if report is None:raise RuntimeError('worker produced no cleanup report; inspect isolated test log')
report['images']=images
report['source_sha256']=hashlib.sha256(source).hexdigest()
report['host_completed_unix']=time.time()
report['cleanup']['cache_removed']=[]
if report['cleanup'].get('envs_destroyed'):
    state_dir='/var/lib/agentruntime/fc/state'
    for line in Path('/etc/agentruntime/fc-manager.env').read_text().splitlines():
        if line.startswith('AR_FC_STATE_DIR='):
            state_dir=shlex.split(line.split('=',1)[1])[0]
    root=Path(state_dir)/'blobs'
    assert root.is_absolute() and not root.is_symlink()
    for digest,size in report.get('owned_cache_blobs',{}).items():
        assert re.fullmatch('[0-9a-f]{64}',digest) and 0<=size<=64<<20
        path=root/digest
        try:meta=path.lstat()
        except FileNotFoundError:continue
        assert stat.S_ISREG(meta.st_mode) and meta.st_size==size
        with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        assert actual==digest
        path.unlink()
        report['cleanup']['cache_removed'].append(digest)
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':report.get('passed'), 'tenant':report['tenant'],'checks':sorted(report.get('checks',{})),'cleanup':report['cleanup'],'report':str(out/'report.json')},ensure_ascii=False))
raise SystemExit(result.returncode or (0 if report.get('passed') else 1))
