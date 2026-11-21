"""Exercise real, non-model workspace operations and verify the per-arm Worker code."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib,json,subprocess
from pathlib import Path
from runtime_client import RuntimeClient, WORKSPACE_SERVICE

OUT=Path(__file__).resolve().parent
BASE="mini50-1121"
def run(*cmd, stdin=None):
 return subprocess.check_output(cmd,input=stdin,stderr=subprocess.DEVNULL)
def test(arm):
 name=BASE+"-"+arm
 ip=run("kubectl","-n","agent-runtime","get","svc",name+"-api","-o","jsonpath={.spec.clusterIP}").decode()
 # Short-lived, evaluation-scoped token stays in process memory and never appears in argv/output.
 token=run("/root/ar-release/ops-token.sh","swebench","mini50-preflight","20m").decode().strip()
 api=RuntimeClient("http://"+ip+":8080",token,timeout=15,operation_timeout=120)
 rid=name+"-verify-"+datetime.now(timezone.utc).strftime("%H%M%S")
 result={"arm":arm,"endpoint":"http://"+ip+":8080","tenant":"swebench","workspace_id":rid}
 inspected=run("kubectl","-n","agent-runtime","exec","deploy/"+name+"-workspace","--","python","-c",
  "from pathlib import Path;import json,hashlib,agentruntime_worker as w,os; d=Path(w.__file__).parent; print(json.dumps({'temporal_namespace':os.environ['AR_TEMPORAL_NAMESPACE'],'gateway':os.environ['AR_MODEL_GATEWAY_ADDRESS'],'files':{p:hashlib.sha256((d/p).read_bytes()).hexdigest() for p in ('reviewer/rules.py','workflows/verification.py')}}))")
 result.update(json.loads(inspected))
 freeze=json.loads((OUT/"freeze.json").read_text())
 assert result["files"]==freeze["baseline_files" if arm=="a" else "candidate_files"]
 try:
  doc=api.create_workspace_operation(rid,repo_url="",ref="",files=[{"path":"readme.txt","content":"isolated swebench preflight\n"}],
      request_id=rid+"-create",checkpoint=lambda state:result.update(create_operation=state))
  observed=next((x for x in api._call(WORKSPACE_SERVICE,"ListWorkspaces",{}).get("workspaces",[]) if x.get("workspaceId")==rid),{})
  initial=observed.get("snapshotId")
  archived=None
  if not initial and observed.get("head"):
   archived=api._workspace_operation("archive",{"workspace_id":rid},request_id=rid+"-archive",operation_id="",checkpoint=None,legacy_method="ArchiveWorkspace")
  snap=initial or (archived or {}).get("snapshotId")
  result.update(ready=bool(doc and observed.get("head") and snap),head=observed.get("head"),initial_snapshot=initial,snapshot=snap,snapshot_mode="create" if initial else "explicit_archive")
 except Exception as e:
  result.update(ready=False,create_error=str(e)[:300])
 finally:
  try:
   api.delete_workspace_operation(rid,request_id=rid+"-delete")
   result["delete_completed"]=True
  except Exception as e:
   result.update(delete_completed=False,delete_error=str(e)[:260])
 return result
if __name__=="__main__":
 with ThreadPoolExecutor(2) as ex:out=list(ex.map(test,("a","b")))
 (OUT/"workspace-preflight.json").write_text(json.dumps(out,indent=2)+"\n")
 print(json.dumps(out,ensure_ascii=False))
