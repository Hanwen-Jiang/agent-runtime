"""Reconcile the website's Mini 50 card against the archived official per-case evidence."""
import hashlib
import json
from pathlib import Path

import pytest

from agentruntime_learning.evals.public_benchmarks import PublicBenchmarkSource
from agentruntime_learning.evals.public_swe_campaigns import MANIFEST, swe_campaigns

DATA = Path(__file__).resolve().parents[3] / 'docs/plan/evidence/swe-mini50-20261121'


def read(path):
    return json.loads(path.read_bytes())


def write_run(root, rid, ids, rows, model, original=None):
    run = {'run_id':rid, 'benchmark':'swe-bench', 'created_at':'2026-11-21T16:05:00Z',
           'instance_ids':ids, 'settings':{'hints':False,'environment':'official','model_name':model},
           'selection':{'dataset_split':'test','instances_sha256':read(DATA/'cohort.json')['source_file_sha256'],
                        'instances_file':'verified.jsonl','ids':True}}
    resolved = sum(r['grading']=='resolved' for r in rows)
    report = original or {'run_id':rid,'benchmark':'swe-bench','model_name':model,'selected':len(ids),
                          'resolved':resolved,'ran':len(rows),'not_run':len(ids)-len(rows),'errors':0,'graded':True,
                          'graded_complete':len(rows)==len(ids),'resolve_rate':resolved/len(ids),'instances':rows}
    dst = root/rid
    dst.mkdir(exist_ok=True)
    for name, data in [('run.json',run),('report.json',report)]:
        (dst/name).write_text(json.dumps(data))


def add_original_and_recovery(root):
    ids = (DATA/'mini50.ids').read_text().splitlines()
    for arm in 'ab':
        report = read(DATA/'final'/f'mini50-{arm}-report.json')
        write_run(root,report['run_id'],ids,report['instances'],report['model_name'],report)
        spec = MANIFEST['arms'][arm]
        (root/report['run_id']/'report.json').write_bytes((DATA/'final'/f'mini50-{arm}-report.json').read_bytes())
        (root/report['run_id']/'paired-protocol.json').write_text(json.dumps({
            'run_id':report['run_id'], 'model_label':report['model_name'], 'runtime_tree':spec['runtime_tree']}))
        retry = read(DATA/'final'/f'mini50-quota-recovery-{arm}.json')
        tested = {iid:label for label in ('resolved','unresolved') for iid in retry['grader'][label+'_ids']}
        rows = [{'instance_id':iid,'state':'finished','grading':label} for iid,label in tested.items()]
        write_run(root,retry['run_id'],retry['protocol']['ids'],rows,spec['recovery_model'])
        bind_recovery(root,retry['run_id'],spec['initial_run_id'],retry['protocol'])


def bind_recovery(root, rid, parent, protocol):
    directory = root/rid
    run = read(directory/'run.json')
    run['selection']['source_mini_run'] = parent
    (directory/'run.json').write_text(json.dumps(run))
    (directory/'recovery-protocol.json').write_text(json.dumps(protocol))


def test_original_50_and_retries_count_distinct_issues(tmp_path):
    add_original_and_recovery(tmp_path)
    source = PublicBenchmarkSource(tmp_path,ttl_s=0)
    feed = json.loads(source.get())
    campaign, = feed['campaigns']
    assert len(feed['runs']) == 4 and campaign['selected'] == 50
    assert [c['instance_id'] for c in campaign['cases']] == campaign['selection_info']['selection_ids']
    a,b = campaign['arms']
    assert [(r['initial_resolved'],r['regraded_resolved'],r['retry_resolved'],r['cumulative_resolved'],
             r['graded_count']) for r in (a,b)] == [(26,0,7,33,46),(27,1,5,33,47)]
    assert campaign['paired']['both_resolved'] == 31
    assert set(campaign['paired']['only_b_ids']) == {'django__django-11790','sphinx-doc__sphinx-10673'}
    for row in b['regrades']:
        path = DATA/'final'/row['artifact']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['source_sha256']
        assert read(path)[row['grade']+'_ids'] == [row['instance_id']]
    # A real infra stage can add evidence one official verdict at a time.
    spec = MANIFEST['arms']['a']
    infra_ids = spec['infra_ids']
    rows = [{'instance_id':infra_ids[0],'state':'finished','grading':'resolved'},
            {'instance_id':infra_ids[1],'state':'finished','grading':'unresolved'}]
    write_run(tmp_path,spec['infra_run_id'],infra_ids,rows,spec['infra_model'])
    protocol = {**read(DATA/'final/mini50-quota-recovery-a.json')['protocol'], 'mode':'missing_verdict_recovery','ids':infra_ids}
    bind_recovery(tmp_path,spec['infra_run_id'],spec['initial_run_id'],protocol)
    updated, = json.loads(source.get())['campaigns']
    assert updated['arms'][0]['cumulative_resolved'] == 34
    assert updated['arms'][0]['graded_count'] == 48
    assert updated['arms'][0]['infra_graded'] == 2
    assert updated['arms'][1] == b


def test_later_official_win_never_counts_twice(tmp_path):
    add_original_and_recovery(tmp_path)
    a = MANIFEST['arms']['a']
    path = tmp_path/a['initial_run_id']/'report.json'
    report = read(path)
    retry_iid = 'django__django-11964'
    row = next(row for row in report['instances'] if row['instance_id']==retry_iid)
    row['grading'] = 'resolved'
    report['resolved'] += 1
    report['resolve_rate'] = report['resolved']/50
    path.write_text(json.dumps(report))
    campaign, = json.loads(PublicBenchmarkSource(tmp_path).get())['campaigns']
    arm = campaign['arms'][0]
    case = next(row for row in campaign['cases'] if row['instance_id']==retry_iid)
    assert case['a']['initial'] == case['a']['retry'] == 'resolved'
    assert arm['initial_resolved'] == 27 and arm['retry_resolved'] == 6
    assert arm['cumulative_resolved'] == 33 and arm['graded_count'] == 46


def test_diagnostics_and_unrelated_or_mismatched_runs_cannot_contribute(tmp_path):
    source = PublicBenchmarkSource(tmp_path,ttl_s=0)
    assert json.loads(source.get())['campaigns'] == []
    add_original_and_recovery(tmp_path)
    diag = next(d for d in MANIFEST['diagnostics'] if d['kind']=='routing_funded')
    rows = [{'instance_id':iid,'state':'finished','grading':'resolved'} for iid in diag['ids']]
    write_run(tmp_path,diag['run_ids'][0],diag['ids'],rows,'candidate-fixed-routing')
    write_run(tmp_path,'mini50-unrelated-2',diag['ids'],rows,'other')
    campaign, = json.loads(source.get())['campaigns']
    assert 'mini50-unrelated-2' not in campaign['member_run_ids']
    assert [a['cumulative_resolved'] for a in campaign['arms']] == [33,33]
    assert next(d for d in campaign['diagnostics'] if d['kind']=='routing_funded')['results'][0]['resolved'] is None
    observed = json.loads(source.get())['runs']
    next(r for r in observed if r['run_id']==diag['run_ids'][0])['source_sha256'] = diag['archives'][diag['run_ids'][0]]
    verified, = swe_campaigns(observed)
    fixed = next(d for d in verified['diagnostics'] if d['kind']=='routing_funded')['results'][0]
    assert fixed['resolved'] == fixed['graded_count'] == 2
    spec = MANIFEST['arms']['a']
    write_run(tmp_path,spec['recovery_run_id'],diag['ids'],rows,'incorrect cohort')
    updated, = json.loads(source.get())['campaigns']
    assert updated['arms'][0]['retry_resolved'] is None
    assert updated['arms'][0]['cumulative_resolved'] == 26
    # Even the expected primary run ID cannot substitute another test population.
    write_run(tmp_path,spec['initial_run_id'],diag['ids'],rows,spec['initial_model'])
    assert json.loads(source.get())['campaigns'] == []

@pytest.mark.parametrize('mutation', ['runtime_tree','arm','mode','parent','label','bad_sidecar','reversed_base','conflicting_verdict','changed_original'])
def test_source_audit_failures_keep_previously_judged_and_reject_wrong_retries(tmp_path, mutation):
    add_original_and_recovery(tmp_path)
    source = PublicBenchmarkSource(tmp_path,ttl_s=0)
    a,b = MANIFEST['arms'].values()
    directory = tmp_path/a['recovery_run_id']
    if mutation in ('runtime_tree','arm','mode'):
        protocol = read(directory/'recovery-protocol.json')
        protocol[mutation] = {'runtime_tree':b['runtime_tree'],'arm':'b','mode':'missing_verdict_recovery'}[mutation]
        bind_recovery(tmp_path,a['recovery_run_id'],a['initial_run_id'],protocol)
    elif mutation == 'bad_sidecar':
        (directory/'recovery-protocol.json').write_text('broken')
        snapshot = json.loads(source.get())
        assert not snapshot['stale']
        child = next(r for r in snapshot['runs'] if r['run_id'] == a['recovery_run_id'])
        assert child['recovery_source_status'] == 'unreadable_protocol'
    elif mutation == 'parent':
        bind_recovery(tmp_path,a['recovery_run_id'],b['initial_run_id'],read(directory/'recovery-protocol.json'))
    elif mutation == 'label':
        for name in ('run','report'):
            path=directory/(name+'.json')
            obj=read(path)
            (obj['settings'] if name=='run' else obj)['model_name']=b['recovery_model']
            path.write_text(json.dumps(obj))
    elif mutation == 'reversed_base':
        path=tmp_path/b['initial_run_id']/'run.json'
        run=read(path);run['instance_ids'].reverse();path.write_text(json.dumps(run))
        assert json.loads(source.get())['campaigns'] == []
        return
    elif mutation == 'changed_original':
        path=tmp_path/b['initial_run_id']/'report.json'
        path.write_text(json.dumps(read(path)))  # different original-report fingerprint
        c,=json.loads(source.get())['campaigns']
        assert c['arms'][1]['regrades']==[] and c['arms'][1]['cumulative_resolved']==32
        assert c['ignored_sources'][0]['reason']=='same_patch_regrades_original_report_mismatch'
        return
    elif mutation == 'conflicting_verdict':
        path=tmp_path/a['initial_run_id']/'report.json'
        r=read(path); iid='django__django-11964'
        next(row for row in r['instances'] if row['instance_id']==iid)['grading']='unresolved'
        path.write_text(json.dumps(r))
        c,=json.loads(source.get())['campaigns']
        assert c['arms'][0]['cumulative_resolved']==32
        assert {'arm':'a','instance_id':iid,'stage':'retry','retained':'unresolved','reason':'already_graded'} in c['ignored_sources']
        return
    c,=json.loads(source.get())['campaigns']
    assert c['arms'][0]['cumulative_resolved']==26 and c['arms'][0]['retry_resolved'] is None
    assert c['arms'][1]['cumulative_resolved']==33
    assert {'run_id':a['recovery_run_id'],'reason':'recovery_provenance_mismatch'} in c['ignored_sources']
