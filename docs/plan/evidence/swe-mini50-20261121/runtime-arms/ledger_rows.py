"""In-cluster, read-only query, tenant RLS and explicit selected task IDs. No credentials in output."""
import asyncio,json,os,re,sys
from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
from agentruntime_core.tenancy import TenantContext
async def main():
    payload=json.load(sys.stdin);tenant=payload["tenant"];ids=payload["task_ids"]
    if tenant not in ("evals","swebench") or not isinstance(ids,list) or not 0<len(ids)<=100 or len(set(ids))!=len(ids):
        raise SystemExit("unscoped ledger query")
    if not all(isinstance(i,str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}",i) for i in ids):raise SystemExit("invalid task id")
    store=await PostgresRelationalStoreProvider.connect(os.environ["AR_RELATIONAL_DSN"])
    try:
        async with store.bind(TenantContext(tenant_id=tenant)).transaction(read_only=True) as tx:
            rows=await tx.query("SELECT task_id,node_id,model_profile_id,method,actual_usd_micros,registry_scope,registry_revision,registry_document_sha256 FROM model_usage WHERE task_id IN ("+",".join(f"${i}" for i in range(1,len(ids)+1))+") ORDER BY task_id,node_id,model_profile_id,method,reserved_at",ids)
        print(json.dumps([dict(r) for r in rows],default=str))
    finally:await store.aclose()
asyncio.run(main())
