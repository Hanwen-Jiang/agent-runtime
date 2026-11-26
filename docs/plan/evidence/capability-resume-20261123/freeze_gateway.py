"""Record the scoped cluster and source inputs used by the component evaluators."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess

def digest(blob):
    return sha256(blob).hexdigest()

def serialize(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()

def kget(kind, name):
    data=subprocess.check_output(['kubectl','-n','agent-runtime','get',kind,name,'-o','json'])
    return json.loads(data)

def save(path, doc):
    encoded=json.dumps(doc, indent=2, ensure_ascii=False)+'\n'
    if path.exists() and path.read_text()!=encoded:
        raise ValueError(f'frozen input changed at {path.name}')
    path.write_text(encoded)

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('directory', type=Path);p.add_argument('runtime_commit');p.add_argument('coding_agent_commit');args=p.parse_args()
    d=args.directory
    deployments={}
    for name in ('ar-agent-runtime-model-gateway','ar-agent-runtime-memory','ar-agent-runtime-task-service'):
        doc=kget('deploy',name); template=doc['spec']['template']
        deployments[name]={'uid':doc['metadata']['uid'],'generation':doc['metadata']['generation'],
                           'template_sha256':digest(serialize(template)),'images':[c['image'] for c in template['spec']['containers']]}
    gateway=kget('deploy','ar-agent-runtime-model-gateway')['spec']['template']['spec']
    mounted=next(v['configMap']['name'] for v in gateway['volumes'] if v['name']=='model-profiles')
    catalog=json.loads(kget('cm',mounted)['data']['model-profiles.json'])
    actual={k: v for k,v in catalog['registry_revisions'].items() if v['scope']=='platform'}
    release={'gateway_configmap':mounted,'gateway_catalog_sha256':digest(serialize(catalog)),
             'deployments':deployments,'runner_archive_sha256':digest((d/'src.tgz').read_bytes()),
             'component_source_commit':args.runtime_commit, 'coding_agent_workspace_commit':args.coding_agent_commit,
             'coding_agent_usage':'not invoked by component adapters', 'memory_service_usage':'not invoked by these component adapters',
             'sub_agents':'off'}
    freeze={'runtime_commit':args.runtime_commit,'coding_agent_commit':args.coding_agent_commit,
            'deployment_manifest_sha256':digest(serialize(release)),
            'registry_revisions':[{'scope':'platform','kind':'model_profile','name':i,'version':v['version'],'document_sha256':v['sha256']} for i,v in sorted(actual.items())],
            'memory_revision':deployments['ar-agent-runtime-memory']['images'][0],
            'memory_config_sha256':deployments['ar-agent-runtime-memory']['template_sha256'],
            'subject_models':sorted(actual),'sub_agents':'off'}
    save(d/'freeze.json',freeze); save(d/'release.json',release)
    if catalog!=json.loads((d/'profiles.json').read_text()):raise ValueError('catalog changed')
    print('verified',mounted,len(actual),release['runner_archive_sha256'])
