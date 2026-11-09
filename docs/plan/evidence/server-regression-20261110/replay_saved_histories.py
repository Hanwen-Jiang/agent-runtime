import argparse, asyncio, hashlib, json
from pathlib import Path
from agentruntime_worker.replay import load_histories, replay_histories
from agentruntime_worker.workflows.files import FileOperationWorkflow
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[4])
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    base=args.repo/'docs/plan/evidence/file-engines-m1'
    cases=[]
    for name in ('temporal-histories','child-scope-histories'):
        directory=base/name
        histories=load_histories(directory)
        if not histories:raise RuntimeError("No histories in "+str(directory))
        results=await replay_histories(histories,workflows=[FileOperationWorkflow,TaskWorkflow,SubAgentWorkflow,SubgraphWorkflow])
        for history,result in zip(histories,results,strict=True):
            path=directory/(history.workflow_id+'.json')
            cases.append({'history':str(path.relative_to(base)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'passed':result.replay_failure is None,'error':str(result.replay_failure) if result.replay_failure else None})
    out={'scope':'Saved real Temporal file-operation and child-scope histories; no histories regenerated','cases':cases,'passed':sum(item['passed'] for item in cases),'failed':sum(not item['passed'] for item in cases)}
    if args.output:args.output.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({'passed':out['passed'],'failed':out['failed']}))
    if out['failed']:raise SystemExit(1)

asyncio.run(main())
