import assert from "node:assert/strict";
import test from "node:test";
import {displayGroup, matchesRunSearch, reasoningLabel, routingLabel, runtimeEvidence, subjectLabel} from "../src/runtime-identity.js";
import {campaignMatches} from "../src/swe-campaigns.js";

const zh = (value) => value;
const id = "mini50-tuned-c-gpt62-20261123";
const legacy = {suite:"swebench", run_id:id, subject:"runtime-c-gpt-6.2-sol-fixed", model:"runtime-c-gpt-6.2-sol-fixed"};

test("verified fixed and native runs have separate identity, display group, and measured effort", () => {
  assert.equal(subjectLabel(legacy,zh),"agentruntime-e3646dd0");
  assert.equal(displayGroup(legacy),"medium");
  assert.equal(routingLabel(runtimeEvidence(legacy),zh),"固定模型");
  assert.equal(reasoningLabel(runtimeEvidence(legacy),zh),"思考等级中");
  const native = {suite:"swebench",run_id:"route2-funded-20261122-native"};
  assert.equal(subjectLabel(native,zh),"agentruntime-014d63df");
  assert.equal(displayGroup(native),"medium");
  assert.equal(routingLabel(runtimeEvidence(native),zh),"内核自动路由");
  assert.equal(reasoningLabel(runtimeEvidence(native),zh),"思考等级混合");
});

test("a future run does not inherit a historical tag, effort or routing from its label or identical tree", () => {
  const future = {...legacy,run_id:id+"-later", runtime_tree:"e3646dd0ae3995fb8337eefad5dffa5a43417032"};
  assert.equal(displayGroup(future),null);
  assert.equal(subjectLabel(future,zh),"agentruntime-e3646dd0");
  assert.equal(routingLabel(runtimeEvidence(future),zh),"路由方式未记录");
  assert.equal(reasoningLabel(runtimeEvidence(future),zh),null);
  assert.equal(subjectLabel({...future,runtime_tree:null,model:"agentruntime-rev123"},zh),"agentruntime（版本未记录）");
});

test("legacy archive observations without run IDs match the frozen record, not later observations", () => {
  const archive = {suite:"task-loop",group:"history", created_at:"2026-10-22T05:50:31.948233+0000",subject:"worker-planner",split:"dev"};
  assert.equal(displayGroup(archive),"medium");
  assert.equal(displayGroup({...archive, created_at:"2027-01-01T00:00:00Z"}),null);
  assert.equal(displayGroup({...archive, run_id:"new-run"}),null);
});

test("reference runs, base-model capabilities and external systems retain their own identity", () => {
  for(const row of [{...legacy,suite:"capability",model:"a6api/gpt-6.2-sol"},
                    {...legacy,evaluation_type:"model",model:"gpt-6.2-sol"},
                    {suite:"swebench",model:"gold-smoke",run_id:"gold-smoke-2"},
                    {suite:"swebench",model:"OtherAgent",subject:"OtherAgent",run_id:"external-sol"}]) {
    assert.equal(runtimeEvidence(row),null);
    assert.equal(displayGroup(row),null);
    assert.equal(subjectLabel(row,zh),row.model);
  }
});

test("original ID and model remain searchable alongside the system version and frozen label", () => {
  for (const query of [legacy.model, legacy.run_id, "agentruntime-e3646dd0", "medium", "agentruntime-e3646dd0 medium"]) {
    assert(matchesRunSearch(legacy,query));
    assert(campaignMatches({title:"SWE",campaign_id:"mini",member_run_ids:[id]},new Map([[id,legacy]]),{search:query}));
  }
  assert.equal(matchesRunSearch(legacy,"不存在的运行"),false);
});

test("reused DeepEval run names only label the archived start time", () => {
  const row = {run_id:"deepeval", suite:"deepeval", subject:"agent-runtime"};
  assert.equal(displayGroup({...row,started_at:"2026-11-04T09:59:21Z"}),"medium");
  assert.equal(displayGroup({...row,started_at:"2026-11-24T09:59:21Z"}),null);
  assert.equal(displayGroup(row),null);
});
