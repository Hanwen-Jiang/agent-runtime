import test from "node:test";
import assert from "node:assert/strict";
import { campaignHref, campaignMatches, campaignRunIdsMatching, exactCampaignRunId, separateCampaignRuns } from "../src/swe-campaigns.js";
import { buildCategoryCharts, buildTrendGroups } from "../src/result-series.js";
import { bestResults } from "../src/best-results.js";
import { reportRuns } from "../src/report-runs.js";
const id="swe-verified-mini50-20261121";
const c={campaign_id:id, title:"SWE-bench Verified Mini 50", suite:"swebench", split:"verified", selected:50,
  member_run_ids:["a-first","b-first","b-retry","routing-two"], arms:[
    {arm:"a",initial_run_id:"a-first",graded_count:46,initial_resolved:26,regraded_resolved:0,retry_resolved:7,cumulative_resolved:33},
    {arm:"b",initial_run_id:"b-first",graded_count:47,initial_resolved:27,regraded_resolved:1,retry_resolved:5,cumulative_resolved:33},
  ]};
const row=(id,n,rate)=>({suite:"swebench", run_id:id, split:"verified",model:id+"-model",selected:n,rate, scores:{resolved_rate:rate},
  status:"finished",complete:true,cohort_sha256:"a".repeat(64),source:"benchmark_artifacts",
  created_at:"2026-11-22T10:00:00Z"});
const miniHash="92fc6b6b100c4f07caedda76d36e39aabb9ab266435c35de90597bdd3d29efff";
const initial={...row("a-first",50,.52),cohort_sha256:miniHash}, candidate={...row("b-first",50,.54),cohort_sha256:miniHash,created_at:"2026-11-22T10:30:00Z"}, diagnostic=row("routing-two",2,1), retry=row("b-retry",10,.5), other=row("init-50-legacy",50,.76);
const bench={campaigns:[c],runs:[initial,candidate,diagnostic,retry,other]};
const live={runs:[{...diagnostic,source:"telemetry",finished:true,started_at:"2026-11-22T10:30:00Z"}]};
const archive={reports:{benchmarks:[diagnostic,other]}};

test("group membership preserves historical links, search and split scope",()=>{
  const rows=new Map(bench.runs.map(r=>[r.run_id,r]));
  for(const search of [id,"mini50","mini-50","MINI 50","routing-two","b-retry-model","a-fir"])
    assert.equal(campaignMatches(c,rows,{search}),true);
  assert.equal(campaignMatches(c,rows,{search:"init-50-legacy"}),false);
  assert.equal(campaignMatches(c,rows,{status:"finished"}),false);
  assert.equal(campaignMatches(c,rows,{status:"partial"}),true);
  assert.equal(campaignMatches({...c,arms:c.arms.map(a=>({...a,graded_count:50}))},rows,{status:"finished"}),true);
  assert.equal(campaignMatches(c,rows,{split:"holdout"}),false);
  assert.deepEqual(campaignRunIdsMatching(c,rows,"routing-t"),["routing-two"]);
  for(const search of ["mini50","mini-50","a-fir","b-retry-model",id]) assert.equal(exactCampaignRunId(c,search),undefined);
  assert.equal(exactCampaignRunId(c,"B-RETRY"),"b-retry");
  assert.equal(campaignHref(c),"/results?q="+id+"#run-records");
});
test("all data sources exclude child runs from standalone bests and trend points, preserving unrelated runs",()=>{
  const filtered=separateCampaignRuns(live,archive,bench);
  assert.deepEqual(separateCampaignRuns([diagnostic,other],{runs:[diagnostic,other]},bench).archive.runs,[other]);
  assert.deepEqual(separateCampaignRuns([diagnostic,other],archive,bench).live,[other]);
  const best=bestResults(reportRuns(filtered.live,filtered.archive,filtered.benchmarks),filtered.archive.reports);
  assert.equal(best[0].run.run_id,other.run_id);
  const trends=buildTrendGroups(live,archive,bench,{includeSingle:true});
  assert.deepEqual(trends.flatMap(g=>g.points.map(p=>p.id)).sort(),[initial.run_id,candidate.run_id,other.run_id]);
  assert.equal(bench.runs.length,5);
  const category=buildCategoryCharts(live,archive,bench).find(cat=>cat.suite==="swebench");
  assert.deepEqual(category.metrics.map(m=>m.id),["resolved_rate"]);
  assert.equal(category.metrics[0].views.length,1);
  const series=category.metrics[0].views[0].series.filter(g=>g.batch_id==="mini-50");
  assert.equal(series.length,1);
  assert.deepEqual(series[0].points.map(p=>[p.id,p.arm,p.value]),[["a-first","A",.66],["b-first","B",.66]]);
  assert.ok(series[0].points.every(p=>p.sample_count===50&&p.href===campaignHref(c)));
  const fresh={...bench,campaigns:[{...c,arms:[c.arms[0],{...c.arms[1],cumulative_resolved:35,graded_count:50}]}]};
  const updated=buildTrendGroups(live,archive,fresh).find(g=>g.batch_id==="mini-50").points;
  assert.deepEqual(updated.map(p=>p.value),[.66,.70]);
});
test("an unavailable campaign leaves original runs accessible",()=>{
  const alone=separateCampaignRuns(live,archive,{runs:bench.runs});
  assert.equal(alone.live,live);assert.equal(alone.archive,archive);
  assert.equal(alone.benchmarks.runs.length,5);
});

test("a partially graded archived run retains its official observation and coverage while zero coverage cannot inherit a telemetry score",()=>{
  const raw={...other,complete:false,graded_complete:false,finished:true,status:"partial",graded_count:48};
  const stale={...raw,source:"telemetry",status:"finished",scores:{resolved_rate:1},started_at:raw.created_at};
  const grouped=graded_count=>buildTrendGroups({runs:[stale]},{reports:{benchmarks:[other]}},{runs:[{...raw,graded_count}]},{includeSingle:true});
  const [group]=grouped(48);
  assert.deepEqual(group.points.map(p=>[p.value,p.graded_count]),[[.76,48]]);
  assert.deepEqual(grouped(0),[]);
});
