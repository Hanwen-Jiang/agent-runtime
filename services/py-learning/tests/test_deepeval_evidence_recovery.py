import copy
import json
from types import SimpleNamespace as NS
import pytest
from agentruntime_learning.evals import deepeval_evidence as e

class Payloads:
 def __init__(self,value):self.payloads=[value]
 def SerializeToString(self,**kw):return json.dumps(self.payloads).encode()
class Event:
 def __init__(self,id,kind,value):self.event_id=id;self.kind=kind;setattr(self,kind,value)
 def HasField(self,field):return field==self.kind
class History:
 def __init__(self,inp,out):
  self.events=[Event(2,'activity_task_scheduled_event_attributes',NS(activity_type=NS(name='call_tool'),input=Payloads(inp))),Event(3,'activity_task_completed_event_attributes',NS(scheduled_event_id=2,result=Payloads(out)))]
 def to_json(self):return 'history'
async def decode(payloads):return payloads

def inp():return {'task_id':'t','tenant':{'tenant_id':'tenant'},'round_seq':1,'node_id':'read.2','tool':'repo_read','args':{'path':'pyproject.toml'}}

@pytest.mark.asyncio
async def test_actual_fixture_output_is_bound_and_original_record_unchanged():
 out={'path':'pyproject.toml','content':'version = "0.3.1"','truncated':False}
 found=await e.extract_tools(History(inp(),out),decode,task_id='t',tenant_id='tenant',fixture_paths=['pyproject.toml'])
 assert len(found['events'])==1 and '0.3.1' in found['events'][0]['result']['text']
 record={'version':{'task_id':'t','deliverable':'0.3.1'}};original=copy.deepcopy(record)
 run={'instance_ids':['version'],'identity':{'rubric_sha256':'rubric'}}
 m=e.manifest(run,record,{'version':found});rows=[{'id':'version','actual_output':'0.3.1','tool_evidence':['limited']}]
 improved=e.enriched_rows(rows,m)
 assert record==original and improved[0]['actual_output']==rows[0]['actual_output'] and rows[0]['tool_evidence']==['limited']
 assert len(improved[0]['tool_evidence'])==3 and m['changed_rubric'] is False
 with pytest.raises(ValueError,match='output changed'):
  e.enriched_rows([{**rows[0],'actual_output':'invented improved answer'}],m)

@pytest.mark.asyncio
async def test_mismatched_task_cannot_supply_evidence():
 with pytest.raises(ValueError,match='bound'):
  await e.extract_tools(History(inp(),{}),decode,task_id='other',tenant_id='tenant',fixture_paths=['pyproject.toml'])

@pytest.mark.asyncio
async def test_paths_outside_fixture_and_skill_instructions_are_not_evidence():
 found=await e.extract_tools(History(inp(),{'path':'private.txt','content':'no'}),decode,task_id='t',tenant_id='tenant',fixture_paths=['pyproject.toml'])
 assert found['events']==[]
 i=inp();i['tool']='skill_read'
 found=await e.extract_tools(History(i,{'instructions':'not evidence'}),decode,task_id='t',tenant_id='tenant',fixture_paths=['pyproject.toml'])
 assert found['events']==[]

def test_bounded_redacted_results_do_not_invent_exit_codes():
 result=e.selected_result('shell_run',{}, {'stdout':'x','other':'secret'},frozenset())
 assert result=={'stdout':'x'} and 'exit_code' not in result
 assert e._bounded('x'*17000)['truncated']

def test_manifest_rejects_wrong_case_binding():
 with pytest.raises(ValueError):e.manifest({'instance_ids':['a']},{'a':{'task_id':'t'}},{'a':{'task_id':'other'}})
