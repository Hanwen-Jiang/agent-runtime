import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from agentruntime_learning.evals.public_benchmarks import PublicBenchmarkSource, VERIFIED_EXPORT
from agentruntime_learning.evals.public_swe_progress import publish, read_progress


def test_private_checkpoint_progress_pause_and_official_score(tmp_path):
    fcntl = pytest.importorskip('fcntl')
    root = tmp_path
    rid = 'mini50-quota-test-a'
    p = root / rid
    p.mkdir(mode=0o700)
    records = p / 'records'
    records.mkdir(mode=0o700)
    ids = ['django__django-11790','django__django-12050']
    run = {'benchmark':'swe-bench','run_id':rid,'instance_ids':ids,'created_at':datetime.now(UTC).isoformat(),
           'settings':{'model_name':'runtime-a','environment':'official','hints':False},
           'selection':{'instances_sha256':VERIFIED_EXPORT,'exclude_repos':['psf/requests']}}
    parent = 'mini50-20261121-a'
    run['selection']['source_mini_run'] = parent
    protocol = {'arm':'a','mode':'missing_verdict_recovery','runtime_tree':'a'*40,'ids':ids,
                'profile':'a6api/gpt-6.2-sol@2026-11','tier':'strong@medium', 'original_tasks':{'private':'ignored'}}
    (p/'recovery-protocol.json').write_text(json.dumps(protocol))
    (p/'run.json').write_text(json.dumps(run))
    checkpoint = {'instance_id':ids[0],'task_id':'t1','state':'finished','patch':'PRIVATE_PATCH_CONTENT'}
    (records/f'{ids[0]}.json').write_text(json.dumps(checkpoint))
    projected = root / ('dashboard-progress-'+rid)
    def public():
        publish(root,rid)
        return json.loads(PublicBenchmarkSource(root,ttl_s=0).get())['runs'][0]
    with (p/'.runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        row = public()
        assert (row['status'],row['submitted'],row['patches'],row['done'],row['graded_count']) == ('running',1,1,1,None)
        assert row['scores'] == {} and row['cost_usd'] is None and 'resolved' not in row
        assert row['selection_info']['selection_ids'] == ids
        case_folder = p/'stream'/ids[0]
        case_folder.mkdir(parents=True)
        case_report = case_folder/f'runtime-a.{rid}.json'
        case_report.write_text(json.dumps({'submitted_ids':[ids[0]],'completed_ids':[ids[0]],
                                           'resolved_ids':[],'unresolved_ids':[ids[0]]}))
        receipt = {'run_id':rid,'model':'runtime-a','instance_id':ids[0],
                   'patch_sha256':hashlib.sha256(checkpoint['patch'].encode()).hexdigest(),
                   'official_report_sha256':hashlib.sha256(case_report.read_bytes()).hexdigest()}
        proof = case_folder/'receipt.json'
        proof.write_text(json.dumps(receipt))
        row = public()
        assert (row['status'],row['graded_count'],row['resolved']) == ('running',1,0)
        for key in ('patch_sha256','official_report_sha256'):
            proof.write_text(json.dumps({**receipt,key:'0'*64}))
            assert public()['graded_count'] is None
        flagged = {**json.loads(case_report.read_text()),'ambiguous_failure_ids':[ids[0]]}
        case_report.write_text(json.dumps(flagged))
        proof.write_text(json.dumps({**receipt,'official_report_sha256':hashlib.sha256(case_report.read_bytes()).hexdigest()}))
        assert public()['graded_count'] is None
        proof.unlink()
        partial = {'run_id':rid, 'model_name':'runtime-a', 'selected':2, 'graded':True, 'resolved':1,
                   'instances':[{'instance_id':ids[0], 'grading':'resolved'},{'instance_id':ids[1], 'grading':'not_run'}]}
        (p/'report.json').write_text(json.dumps(partial))
        row = public()
        assert (row['status'], row['graded_count'], row['resolved'], row['scores']['resolved_rate']) == ('running',1,1,.5)
        (p/'report.json').unlink()
        note = {'run_id':rid,'reason':'insufficient_user_quota','instance_id':ids[0],'task_id':'foreign'}
        (p/'provider-quota-pause.json').write_text(json.dumps(note))
        assert public()['status'] == 'running'
        (p/'provider-quota-pause.json').write_text(json.dumps({**note,'task_id':'t1'}))
        assert public()['status'] == 'paused'
        (p/'provider-quota-pause.json').unlink()
        (p/'quota-stop.json').write_text(json.dumps({'code':'insufficient_user_quota','consecutive_refusals':3,'active_in_arm':['t1']}))
        assert public()['status'] == 'paused'
        (p/'quota-stop.json').unlink()
        doc=json.loads((projected/'public-progress.json').read_text())
        assert 'PRIVATE_' not in str(doc) and 't1' not in doc.values()
        doc['status']='running'
        assert read_progress(doc,projected.name,datetime.now(UTC)+timedelta(minutes=3))['status']=='stalled'
    assert public()['status'] == 'stalled'
    (records/f'{ids[1]}.json').write_text(json.dumps({'instance_id':ids[1],'state':'error'}))
    assert public()['status'] == 'awaiting_grade'
    report = {'run_id':rid,'benchmark':'swe-bench','model_name':'runtime-a','selected':2,'resolved':1,'resolve_rate':.5,
              'graded':True,'graded_complete':True,'ran':1,'not_run':0,'errors':1,'cost_usd':.4,
              'instances':[{'instance_id':ids[0],'state':'finished','status':'done','grading':'resolved'},
                           {'instance_id':ids[1],'state':'error','status':None,'grading':'run_error'}]}
    (p/'report.json').write_text(json.dumps(report))
    assert publish(root,rid) and 'PRIVATE_' not in (projected/'report.json').read_text()
    rows = json.loads(PublicBenchmarkSource(root).get())['runs']
    row, = rows
    assert row['scores']['resolved_rate'] == .5 and row['graded_count'] == 1
    assert (row['status'],row['finished'],row['complete'],row['graded_complete']) == ('partial',True,False,False)
    final = json.loads((projected/'run.json').read_text())
    assert final['selection']['source_mini_run'] == parent
    assert final['selection']['exclude_repos'] == ['psf/requests']
    assert (row['selection_info']['eligible_count'],row['selection_info']['excluded_count']) == (492,8)
    assert final['recovery_protocol'] == {k:v for k,v in protocol.items() if k != 'original_tasks'}
    assert json.loads((projected/'report.json').read_text())['graded_complete'] is False
    progress = read_progress(json.loads((projected/'public-progress.json').read_text()),projected.name)
    assert (progress['status'],progress['finished'],progress['graded_count']) == ('partial',True,1)
    assert os.stat(p).st_mode & 0o777 == 0o700
    outside = root/'other'
    outside.write_text('{"instance_id":"wrong"}')
    (records/f'{ids[0]}.json').unlink()
    (records/f'{ids[0]}.json').symlink_to(outside)
    with pytest.raises(OSError):
        publish(root,rid)

def test_private_source_alongside_public_projection_does_not_mark_feed_stale(tmp_path, monkeypatch):
    source = PublicBenchmarkSource(tmp_path, ttl_s=0)
    (tmp_path/'private-run').mkdir()
    real_access = os.access
    def access(path, *args, **kwargs):
        if path == 'private-run':
            return False
        return real_access(path, *args, **kwargs)
    monkeypatch.setattr(os, 'access', access)
    assert source._names() == []
    assert json.loads(source.get())['errors'] == []
