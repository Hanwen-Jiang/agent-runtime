import asyncio, json, tempfile
from pathlib import Path
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.knowledge import CatalogAuthorizer, KnowledgeService
from agentruntime_memory.embedding import HashingEmbedder, GatewayEmbedder
from agentruntime_memory.identity import Identity
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_core.errors import ConflictError,NotFoundError,DeniedError
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

class Models:
    def __init__(self):self.calls=[];self.hashing=HashingEmbedder(version='synthetic-embed')
    async def embed(self,request):
        self.calls.append(request)
        assert request.tenant.tenant_id=='tenant_a'
        vectors=await self.hashing.embed(None,request.texts)
        return mg.EmbedResponse(embedding_version='synthetic-embed',embeddings=[mg.Embedding(values=list(v)) for v in vectors])

async def main():
    path=Path(tempfile.mkdtemp(prefix='retrieval-evidence-'))
    db=SQLiteRelationalStoreProvider(path/'db');vectors=InMemoryVectorStoreProvider();models=Models()
    await db.migrate((Migration(419,'synthetic_files_acl',(
        'CREATE TABLE runtime_files(file_id TEXT PRIMARY KEY,workspace_id TEXT,created_by TEXT,lifecycle TEXT)',
        'CREATE TABLE runtime_file_versions(version_id TEXT PRIMARY KEY,file_id TEXT)',
        'CREATE TABLE runtime_file_grants(file_id TEXT,workspace_id TEXT,subject_type TEXT,subject_id TEXT,permission TEXT)',
    )),))
    alice=Identity('tenant_a','alice','work_a');bob=Identity('tenant_a','bob','work_a')
    async with db.bind(alice.tenant_context()).transaction() as tx:
        for ident in ('refund','shipping'):
            await tx.execute("INSERT INTO runtime_files VALUES($1,$2,$3,'active')",(ident,'work_a','alice'))
            await tx.execute('INSERT INTO runtime_file_versions VALUES($1,$2)',('version_'+ident,ident))
    auth=CatalogAuthorizer(db)
    rerank_calls=[]
    async def rank(identity,query,candidates):
        rerank_calls.append((identity,query,candidates));return [float('refund' in text.lower()) for text in candidates]
    engine=RetrievalEngine(vectors=vectors,embedder=GatewayEmbedder(models,version='synthetic-embed'),relational=db,authorize=auth,rerank=rank)
    await engine.setup()
    docs=[{'version_id':'version_refund','representation_id':'rep_refund','blocks':[{'id':'p1','kind':'paragraph','text':'Refund policy: customers receive a refund within thirty days. 退款需要保留原始收据。','anchor':{'page':3,'bbox':[10,20,300,90]}}]},
          {'version_id':'version_shipping','representation_id':'rep_shipping','blocks':[{'id':'p2','kind':'paragraph','text':'Shipping schedules: parcels leave the warehouse each Monday.','anchor':{'page':8}}]}]
    indexed=await engine.index(alice,'collection_a','gen_a',docs)
    assert indexed['chunks']==2 and indexed['status']=='staged'
    hits=await engine.search(alice,'collection_a','gen_a','refund receipt',embedding_version='synthetic-embed')
    assert hits['hits'][0]['version_id']=='version_refund' and hits['hits'][0]['anchor']['page']==3
    assert 'refund' in hits['hits'][0]['text'].lower() and hits['hits'][0]['quote_hash']
    ids=[hit['id'] for hit in hits['hits']]
    assert (await engine.index(alice,'collection_a','gen_a',docs))==indexed
    evidence=await engine.resolve(alice,'collection_a','gen_a',ids)
    assert {hit['id'] for hit in evidence['evidence']}==set(ids)
    assert not (await engine.search(bob,'collection_a','gen_a','refund',embedding_version='synthetic-embed'))['hits']
    async with db.bind(alice.tenant_context()).transaction() as tx:
        await tx.execute("INSERT INTO runtime_file_grants VALUES('refund','work_a','user','bob','read')")
    shared=await engine.search(bob,'collection_a','gen_a','refund',embedding_version='synthetic-embed')
    assert len(shared['hits'])==1 and shared['hits'][0]['version_id']=='version_refund'
    for identity in (Identity('tenant_b','alice','work_a'),Identity('tenant_a','alice','work_b')):
        try:await engine.search(identity,'collection_a','gen_a','refund',embedding_version='synthetic-embed')
        except NotFoundError:pass
        else:raise AssertionError('cross-scope search allowed')
    async with db.bind(alice.tenant_context()).transaction() as tx:await tx.execute("DELETE FROM runtime_file_grants")
    assert not (await engine.resolve(bob,'collection_a','gen_a',ids))['evidence']
    deleted=await engine.delete_generation(alice,'collection_a','gen_a');assert deleted['deleted_chunks']==2
    assert await vectors.bind(alice.tenant_context()).count('document_chunks')==0
    try:await engine.index(alice,'collection_a','gen_a',docs)
    except ConflictError:pass
    else:raise AssertionError('deleted generation resurrected')
    started,release=asyncio.Event(),asyncio.Event()
    async def slow_embed(identity,texts):
        started.set();await release.wait();return await models.hashing.embed(identity,texts)
    slow=HashingEmbedder(version='slow-embed');slow.embed=slow_embed
    racing=RetrievalEngine(vectors=vectors,embedder=slow,relational=db,authorize=auth)
    running=asyncio.create_task(racing.index(alice,'collection_a','gen_race',docs))
    await started.wait()
    await racing.delete_generation(alice,'collection_a','gen_race');release.set()
    try:await running
    except ConflictError:pass
    else:raise AssertionError('deleted generation completed indexing')
    assert await vectors.bind(alice.tenant_context()).count('document_chunks')==0
    print('PASS: delete during blocked embedding prevents late vector upsert/resurrection')
    await db.aclose()
    (path/'result.json').write_text(json.dumps({'indexed':indexed,'search':hits,'deleted':deleted,'gateway_embed_calls':len(models.calls),'rerank_calls':len(rerank_calls)},ensure_ascii=False,indent=2))
    print('PASS: real LlamaIndex split/adapter/retriever; SQLite staged metadata; gateway embed request; relevant citation+anchor; stable IDs/retry; shared-file grant and revoke; cross tenant/workspace; delete generation/no resurrection. Evidence:',path)
asyncio.run(main())
