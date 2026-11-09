#!/usr/bin/env python3
"""Finite model-context/cache regression probe using explicit model doubles."""
import asyncio,dataclasses,json,tempfile,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'services/py-worker'),str(ROOT/'services/py-worker/src'),str(ROOT/'libs/py-core/src'),str(ROOT/'proto/gen/python')]
from temporalio.testing import ActivityEnvironment
from agentruntime_worker.planner.activities import PlannerActivities
from agentruntime_worker.planner.context import StaticContextSource
from agentruntime_worker.planner.planner import Planner
from agentruntime_worker.executor.tool_loop import ModelToolLoop
from agentruntime_worker.context.builder import ContextBuilder
from tests.planner_support import FakeGateway,context,signals,plan_answer
from tests.test_planner_activity import plan_input
from tests.context_support import ContextGateway,objects,config
from tests.test_tool_loop_model import step_input
from tests.executor_harness import make_fixture,model,node_input
from tests.test_node_executor import env_for
from agentruntime_worker import names

class Collections:
 generation='fixed-generation-one'
 async def references(self,*args):return []
 async def describe(self,*args):return []
 async def collections(self,*args):return [{'id':'col_example','generation':self.generation,'embedding_version':'embed-fixed','revision':1,'root_count':21}]

async def main():
 checks=[]
 with tempfile.TemporaryDirectory(prefix='collection-context-') as tmp:
  root=Path(tmp);sources=Collections()
  gateway=FakeGateway(triage=signals('question',effect='none'),plan=[plan_answer()])
  output=await ActivityEnvironment().run(PlannerActivities(Planner(gateway),StaticContextSource(context()),attachments=sources).plan_round,plan_input('Find the clause in the selected collection.'))
  assert output.triage_level=='full' and all('fixed-generation-one' in ''.join(m.text for m in req.generation.messages) for req in gateway.requests)
  checks.append('collection_only_task_forces_tool_plan_and_injects_frozen_summary')
  step_model=ContextGateway(steps=[{'done':True},{'done':True}]);builder=ContextBuilder(step_model,objects(root),config(effective_tokens=32000));loop=ModelToolLoop(step_model,builder,attachments=sources)
  await loop.step(step_input([]))
  assert 'file_search' in ''.join(m.text for m in step_model.requests[-1].generation.messages) and 'fixed-generation-one' in ''.join(m.text for m in step_model.requests[-1].generation.messages)
  await loop.step(dataclasses.replace(step_input([]),isolated=True))
  assert 'fixed-generation-one' not in ''.join(m.text for m in step_model.requests[-1].generation.messages)
  checks.append('main_tool_loop_sees_collection_but_isolated_child_does_not')
  fixture=make_fixture(root/'executor');fixture.executor.deps.attachments=sources;fixture.models.reply({'value':'one'},{'value':'two'})
  inp=node_input(model('same-input',cache={'ttl_s':60}))
  first=await env_for(names.EXECUTE_MODEL_NODE).run(fixture.executor.execute_model_node,inp)
  sources.generation='fixed-generation-two'
  second=await env_for(names.EXECUTE_MODEL_NODE).run(fixture.executor.execute_model_node,inp)
  assert len(fixture.models.requests)==2 and first['result']!=second['result']
  assert 'fixed-generation-two' in ''.join(m.text for m in fixture.models.requests[-1].generation.messages)
  checks.append('collection_only_model_results_do_not_reuse_cross_generation_cache')
 report={'status':'passed','mode':'explicit_model_doubles_real_context_builder_and_executor','checks':checks}
 target=ROOT/'docs/plan/evidence/file-engines-m1/collection-context-acceptance.json';target.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
asyncio.run(main())
