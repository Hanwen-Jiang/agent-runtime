import asyncio,tempfile
from pathlib import Path
import grpc
from google.protobuf import struct_pb2,json_format
from agentruntime.common.v1 import payload_pb2
from agentruntime.memory.v1 import knowledge_pb2 as kp,knowledge_pb2_grpc as kg
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg,model_gateway_pb2_grpc as mgg
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.knowledge import KnowledgeService,register_knowledge
from agentruntime_memory.embedding import GatewayEmbedder,HashingEmbedder
from agentruntime_memory.wiring import GatewayClient,GatewayReranker
from agentruntime_memory.identity import DevTokenAuthenticator,Identity,issue_dev_token
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
class Model(mgg.ModelGatewayServiceServicer):
 async def Embed(self,r,c):
  assert r.tenant.tenant_id=='tenant_a'
  return mg.EmbedResponse(embedding_version='probe',embeddings=[mg.Embedding(values=v) for v in await HashingEmbedder().embed(None,r.texts)])
 async def Score(self,r,c):
  assert r.tenant.tenant_id=='tenant_a'
  return mg.ScoreResponse(scores=[1 if 'refund' in s else 0 for s in r.candidates])
async def main():
 model=grpc.aio.server();mgg.add_ModelGatewayServiceServicer_to_server(Model(),model);mp=model.add_insecure_port('127.0.0.1:0');await model.start()
 db=SQLiteRelationalStoreProvider(Path(tempfile.mkdtemp())/'db');vector=InMemoryVectorStoreProvider();gateway=GatewayClient('127.0.0.1:'+str(mp))
 async def authorize(identity,c,g,versions):return set(versions) if identity.user_id=='alice' else set()
 engine=RetrievalEngine(vectors=vector,embedder=GatewayEmbedder(gateway,version='probe'),relational=db,authorize=authorize,rerank=GatewayReranker(gateway));await engine.setup()
 server=grpc.aio.server();key=b'knowledge-probe-key';register_knowledge(server,KnowledgeService(engine),DevTokenAuthenticator(key),'internal-probe');port=server.add_insecure_port('127.0.0.1:0');await server.start()
 async with grpc.aio.insecure_channel('127.0.0.1:'+str(port)) as channel:
  stub=kg.KnowledgeServiceStub(channel);identity=Identity('tenant_a','alice','work_a');token=issue_dev_token(key,tenant='tenant_a',user='alice')
  def request(operation,value):
   payload=struct_pb2.Value();json_format.ParseDict(value,payload)
   return kp.KnowledgeServiceExecuteRequest(tenant=identity.tenant_context(),operation=operation,payload=payload_pb2.JsonPayload(value=payload))
  params={'collection_id':'collection','generation':'generation'}
  index=request('index',{**params,'documents':[{'version_id':'version','representation_id':'rep','blocks':[{'id':'b1','text':'refund policy returns money','anchor':{'page':1}}]}]})
  try:await stub.Execute(index,metadata=(('authorization','Bearer '+token),))
  except grpc.aio.AioRpcError as e:assert e.code()==grpc.StatusCode.PERMISSION_DENIED
  else:raise AssertionError('missing internal token accepted')
  metadata=(('authorization','Bearer '+token),('x-ar-knowledge-token','internal-probe'))
  indexed=await stub.Execute(index,metadata=metadata);assert json_format.MessageToDict(indexed.result.value)['status']=='staged'
  searched=await stub.Execute(request('search',{**params,'embedding_version':'probe','query':'refund','limit':8}),metadata=metadata)
  hit=json_format.MessageToDict(searched.result.value)['hits'][0];assert hit['text']=='refund policy returns money' and hit['anchor']['page']==1
  resolved=await stub.Execute(request('resolve',{**params,'node_ids':[hit['id']]}),metadata=metadata);assert json_format.MessageToDict(resolved.result.value)['evidence']
  await stub.Execute(request('delete_generation',params),metadata=metadata)
 await server.stop(0);await model.stop(0);await db.aclose()
 print('PASS: real Knowledge gRPC + real Gateway Embed/Score gRPC; service-token denial; numeric Struct limit; index/search/citation/resolve/delete. Synthetic model only.')
asyncio.run(main())
