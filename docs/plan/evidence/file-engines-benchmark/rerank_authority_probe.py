"""Manual authority/union probe; synthetic embeddings test plumbing, not quality."""
import asyncio, math, tempfile
from pathlib import Path
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import Identity
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.errors import ConflictError,InvalidError

async def main():
    who=Identity('probe','alice','workspace');db=SQLiteRelationalStoreProvider(Path(tempfile.mkdtemp())/'db')
    provider=InMemoryVectorStoreProvider();allowed={f'ver_{i}' for i in range(90)};calls=[]
    async def auth(identity,collection,generation,versions):return set(versions)&allowed
    mode='normal'
    async def score(identity,query,candidates):
        assert identity==who and 0<len(candidates)<=80
        calls.append(len(candidates))
        if mode=='revoke':allowed.clear()
        if mode=='delete':await engine.delete_generation(who,'collection','generation')
        if mode=='bad_count':return []
        if mode=='nonfinite':return [math.nan]*len(candidates)
        return list(range(len(candidates)))
    engine=RetrievalEngine(vectors=provider,embedder=HashingEmbedder(),relational=db,authorize=auth,rerank=score)
    await engine.setup()
    await engine.index(who,'collection','generation',[{'version_id':f'ver_{i}','representation_id':f'rep_{i}','blocks':[{'id':'block','text':f'Refund purchase policy clause {i}; deadline and receipt requirements.', 'anchor':{'page_index':i}}]} for i in range(90)])
    result=await engine.search(who,'collection','generation','refund receipt',embedding_version=engine.embedder.version)
    assert len(result['hits'])==8 and result['retrieval']['candidate_limit']==80 and result['retrieval']['fusion']=='dense_sparse_union_rerank'
    mode='revoke'
    assert not (await engine.search(who,'collection','generation','refund receipt',embedding_version=engine.embedder.version))['hits']
    allowed.update(f'ver_{i}' for i in range(90))
    for mode in ('bad_count','nonfinite'):
        try:await engine.search(who,'collection','generation','refund receipt',embedding_version=engine.embedder.version)
        except InvalidError:pass
        else:raise AssertionError(mode+' accepted')
    mode='delete'
    try:await engine.search(who,'collection','generation','refund receipt',embedding_version=engine.embedder.version)
    except ConflictError:pass
    else:raise AssertionError('generation deleted during score remained visible')
    assert await provider.bind(who.tenant_context()).count('document_chunks')==0
    await db.aclose()
    print('PASS: bounded candidate union, post-score revocation, invalid scorer outputs, generation deletion during scoring; candidate counts:',calls)
asyncio.run(main())
