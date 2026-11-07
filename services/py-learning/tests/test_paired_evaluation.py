"""Regression for paid-run recovery, configuration provenance and paired evidence."""
from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from agentruntime_learning.benchmarks import paired as pe
from agentruntime_learning.benchmarks import provenance as pv
from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks import terminalbench as tb
from agentruntime_learning.benchmarks.client import ApiError, Event, RuntimeClient, token_from_env
from swebench_support import FakeRuntime, Script, records, settings, write_instances
from test_swebench import Flaky
from test_terminalbench import FakeDocker, make_task


def provenance(arm='off'):
    worker = pv.worker_configuration({'containers': [{'name': 'worker', 'image': 'worker@sha256:' + 'a' * 64, 'env': [{'name': 'AR_SUB_AGENT_POLICY', 'value': arm}, {'name': 'AR_MODEL_GATEWAY_ADDRESS', 'value': 'gateway:8080'}]}]})
    return {'runtime_commit': 'a' * 40, 'gateway_config_sha256': 'b' * 64, 'worker_config_sha256': worker['normalized_sha256'], 'worker_configuration': worker,
            'tenant_policy_sha256': 'd' * 64, 'sub_agent_policy': arm, 'scoring_version': pe.RUBRIC_VERSION,
            'models': dict.fromkeys(('plan', 'act', 'route', 'judge', 'sub_agent'), 'profile1'),
            'registry': {'profile1': {'revision': 'v7', 'scope': 'platform', 'document_sha256': 'e' * 64}},
            'memory': {'revision': 'memory-v2', 'corpus_sha256': 'f' * 64}}


def plan():
    return {'run_id': 'test-paired', 'seed': 0, 'tenant_id': 'evals', 'budget_usd': 4.0, 'task_usd_limit': 1.0,
            'thresholds': {'minimum_quality_gain': .1, 'max_cost_ratio': 2.0},
            'arms': {a: {'task_api': 'https://example.invalid', 'snapshot_command': ['inspect', a], 'provenance': provenance(a)} for a in ('off', 'rules')},
            'cases': [{'id': 'single-fact', 'message': 'Read version.txt and report the version.', 'required_patterns': [r'1\.2'],
                       'should_delegate': False, 'files': [{'path': 'version.txt', 'content': '1.2'}]}]}


def row(task='t1', *, node='act', cost=100_000):
    return {'reservation_id': task + node, 'task_id': task, 'node_id': node, 'model_profile_id': 'profile1',
            'actual_usd_micros': cost, 'registry_scope': 'platform', 'registry_revision': 'v7', 'registry_document_sha256': 'e' * 64}


def test_explicit_continuation_rejects_new_unknown_before_any_task(tmp_path):
    from test_paired_admission import fixture
    from agentruntime_learning.benchmarks import paired_admission as admission
    _, receipt, baseline, _ = fixture()
    p = plan()
    p.update(tenant_id=admission.TENANT, budget_usd=37.945261,
             cost_reserves={"semantic_judge_usd": 7, "external_jev_usd": 12.5})
    receipt["plan_sha256"] = admission.digest(p)
    runtime = FakeRuntime()
    extra = dict(baseline[-1], reservation_id="unexpected-new-pending")
    with pytest.raises(ValueError, match="new or changed pending"):
        pe.run_arm(p, tmp_path, "off", runtime=runtime, inspect=lambda argv: p["arms"]["off"]["provenance"],
                   collection_continue=receipt, scope_ledger=lambda: [*baseline, extra])
    assert not runtime.submits and not runtime.create_calls


def test_collection_review_never_changes_an_unknown_cost_verdict(tmp_path):
    from test_paired_admission import fixture
    from agentruntime_learning.benchmarks import paired_admission as admission
    from agentruntime_learning.benchmarks.paired_completion import collection_record
    p, receipt, rows, metric = fixture()
    folder = tmp_path / "off" / "case"
    folder.mkdir(parents=True)
    (folder / "metrics.json").write_text(json.dumps(metric))
    (folder / "model_usage.json").write_text(json.dumps(rows))
    (tmp_path / "collection-continue.json").write_text(json.dumps(receipt))
    before = (folder / "metrics.json").read_bytes()
    observed = collection_record(tmp_path, {**p, "rubric_version": pe.RUBRIC_VERSION}, folder)
    assert observed["valid"] is False and observed["ledger"]["known"] is False
    assert (folder / "metrics.json").read_bytes() == before
    metric["runtime_stable"] = False
    (folder / "metrics.json").write_text(json.dumps(metric))
    with pytest.raises(ValueError, match="incomplete quality"):
        collection_record(tmp_path, p, folder)


def test_partial_pairs_never_report_a_complete_cohort_cost_ratio(tmp_path):
    p = plan()
    p["cases"].append({**p["cases"][0], "id": "unsettled-case"})
    (tmp_path / "paired-manifest.json").write_text(json.dumps(p))
    metric = {"valid": True, "quality": 1, "delegation_correct": True, "elapsed_s": 1,
              "extra_rounds": 0, "repeated_tool_targets": 0, "jev_vetoes": 0,
              "ledger": {"total_cost_usd": 1, "sub_agent_cost_usd": 0}}
    for arm in ("off", "rules"):
        directory = tmp_path / arm / "single-fact"
        directory.mkdir(parents=True)
        (directory / "metrics.json").write_text(json.dumps(metric))
    result = pe.report(tmp_path)
    assert result["paired_cases"] == 1 and result["cost_totals_are_partial"]
    assert result["cost_ratio"] is None and not result["n16_r_quality_and_gateway_cost_gate"]


def test_context_summary_has_its_own_frozen_profile_without_loosening_transform():
    frozen = provenance()
    frozen["models"].update(summary="small-summary", transform="mid-transform", probe="small-probe")
    for profile in ("small-summary", "mid-transform", "small-probe"):
        frozen["registry"][profile] = {"revision": "v7", "scope": "platform", "document_sha256": "e" * 64}
    observations = [
        {**row(node="gather#summary"), "model_profile_id": "small-summary"},
        {**row(node="synthesize"), "model_profile_id": "mid-transform"},
        {**row(node="gather#probe"), "model_profile_id": "small-probe"},
    ]
    assert pe.ledger_metrics(observations, [], frozen, {"synthesize": "transform"})["model_mismatch_reservations"] == []
    observations[0]["model_profile_id"] = "mid-transform"
    assert pe.ledger_metrics(observations, [], frozen, {"synthesize": "transform"})["model_mismatch_reservations"] == [observations[0]["reservation_id"]]
    observations[0]["model_profile_id"] = "small-summary"
    del frozen["models"]["summary"]
    assert pe.ledger_metrics(observations, [], frozen, {"synthesize": "transform"})["model_mismatch_reservations"] == [observations[0]["reservation_id"]]


def test_expanded_judges_use_real_activity_roles_and_time_not_the_parent_label():
    frozen = provenance()
    frozen["models"].update(deliver="deliver-profile", judge="judge-profile", summary="small-profile")
    for profile in ("deliver-profile", "judge-profile", "small-profile"):
        frozen["registry"][profile] = {"revision": "v7", "scope": "platform", "document_sha256": "e" * 64}
    evidence = {"complete": True, "observations": [
        {"task_id": "t1", "node_id": "answer.2", "context_node_id": "answer.2", "role": "judge",
         "scheduled_at_unix_ms": 100, "terminal_at_unix_ms": 200},
        {"task_id": "t1", "node_id": "answer.2", "context_node_id": "answer.2", "role": "deliver",
         "scheduled_at_unix_ms": 300, "terminal_at_unix_ms": 400},
    ]}
    earlier = {**row(node="answer.2"), "reserved_at": 150, "model_profile_id": "judge-profile"}
    later = {**row(node="answer.2"), "reservation_id": "later", "reserved_at": 350, "model_profile_id": "deliver-profile"}
    result = pe.ledger_metrics([earlier, later], [], frozen, {"answer": "deliver"}, evidence)
    assert result["model_mismatch_reservations"] == [] and result["unresolved_role_reservations"] == []
    wrong = {**earlier, "model_profile_id": "deliver-profile"}
    assert pe.ledger_metrics([wrong], [], frozen, {"answer": "deliver"}, evidence)["model_mismatch_reservations"] == [wrong["reservation_id"]]
    outside = {**earlier, "reserved_at": 250}
    assert pe.ledger_metrics([outside], [], frozen, {}, evidence)["unresolved_role_reservations"] == [outside["reservation_id"]]
    overlap = {"complete": True, "observations": [*evidence["observations"], {
        **evidence["observations"][0], "role": "deliver",
    }]}
    assert pe.resolved_attempt_role(earlier, overlap) is None
    summary = {**earlier, "node_id": "answer.2#summary", "model_profile_id": "small-profile"}
    assert pe.resolved_attempt_role(summary, evidence) == "summary"


def test_rotating_ops_token_file_is_reread_without_exposing_credentials(tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('first.jwt')
    monkeypatch.setenv('AR_TASK_API_TOKEN_FILE', str(token))
    client = RuntimeClient('https://example.invalid', token_from_env())
    assert client._headers()['Authorization'] == 'Bearer first.jwt'
    token.write_text('second.jwt\n')
    assert client._headers()['Authorization'] == 'Bearer second.jwt'
    assert 'jwt' not in repr(client)
    token.write_text('bad\nsecret')
    with pytest.raises(ValueError, match='one token') as exc:
        client._headers()
    assert 'secret' not in str(exc.value)


@pytest.mark.parametrize("head", [None, ""])
def test_empty_workspace_contract_accepts_an_unborn_head(monkeypatch, head):
    client = RuntimeClient("https://example.invalid", "")
    calls = []
    workspace = {"workspaceId": "empty-workspace"}
    if head is not None:
        workspace["head"] = head
    def call(service, method, body, **kwargs):
        calls.append((method, body))
        if method == "StartWorkspaceOperation":
            return {"operationId": "op-empty"}
        return {"operationId": "op-empty", "status": "WORKSPACE_OPERATION_STATUS_COMPLETED",
                "result": {"workspace": workspace}}
    monkeypatch.setattr(client, "_call", call)
    result = client.create_workspace_operation("empty-workspace", repo_url="", ref="", files=[], request_id="stable")
    assert result == {"workspace_id": "empty-workspace", "head": "", "branch": ""}
    assert [c[0] for c in calls] == ["StartWorkspaceOperation", "GetWorkspaceOperation"]
    assert calls[0][1]["create"] == {"workspace_id": "empty-workspace", "repo_url": "", "ref": "", "files": []}


@pytest.mark.parametrize("workspace,args", [
    ({"workspaceId": "another", "head": ""}, {}),
    ({"head": ""}, {}),
    ({"workspaceId": "empty-workspace", "head": ""}, {"repo_url": "https://example.invalid/repo.git"}),
    ({"workspaceId": "empty-workspace", "head": ""}, {"ref": "main"}),
    ({"workspaceId": "empty-workspace", "head": ""}, {"files": [{"path": "a.txt", "content": "a"}]}),
])
def test_unborn_head_exception_cannot_accept_clones_fixtures_or_wrong_workspace(monkeypatch, workspace, args):
    client = RuntimeClient("https://example.invalid", "")
    monkeypatch.setattr(client, "_workspace_operation", lambda *a, **kw: {"workspace": workspace})
    with pytest.raises(ApiError) as caught:
        client.create_workspace_operation("empty-workspace", **{"repo_url": "", "ref": "", "files": [], **args}, request_id="stable")
    assert caught.value.code == "invalid_workspace_result"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_empty_head_recovery_preserves_workspace_and_submits_only_one_task(tmp_path, ambiguous):
    class EmptyRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.responses = {}
            self.request_keys = []

        def create_workspace(self, workspace_id, **kwargs):
            return {**super().create_workspace(workspace_id, **kwargs), "head": ""}

        def submit(self, **kwargs):
            key = kwargs["client_request_id"]
            self.request_keys.append(key)
            if key in self.responses:
                return {**self.responses[key], "deduplicated": True}
            response = super().submit(**kwargs)
            self.responses[key] = response
            if ambiguous:
                raise ApiError(0, "transport", "accepted Task response was lost")
            return response

    case = pe.Case("empty-http", "Compare HTTP libraries.", ("HTTP",), True)
    out = tmp_path / "run"
    (out / "records").mkdir(parents=True)
    runtime = EmptyRuntime()
    config = settings(environment="none", keep_workspaces=True)
    runner = pe.Runner(runtime, [case], out, config)
    workspace = runner._workspace_id(case)
    if not ambiguous:
        # A durable older created checkpoint has no operation-result cache.
        runtime.create_workspace(workspace, repo_url="", ref="", files=[])
        runner._save({"instance_id": case.instance_id, "state": "created", "head": "",
                      "workspace_id": workspace, "workspace_generation": "already-created"})
    runner.go()
    if ambiguous:
        saved = records(out)[case.instance_id]
        assert saved["submission_state"] == "pending" and saved["head"] == ""
        assert not saved.get("task_id") and len(runtime.tasks) == 1
        runner = pe.Runner(runtime, [case], out, config)
        runner.go()
    assert runner.summary().finished == 1
    assert runtime.create_calls == [workspace] and runtime.deleted == []
    assert len(runtime.submits) == len(runtime.tasks) == 1
    assert len(runtime.request_keys) == (2 if ambiguous else 1)
    assert len(set(runtime.request_keys)) == 1
    assert runtime.diffs == [(workspace, "")]
    pe.Runner(runtime, [case], out, config).go()
    assert len(runtime.submits) == 1 and runtime.create_calls == [workspace]


def test_pending_submission_without_a_head_checkpoint_still_fails(tmp_path):
    case = pe.Case("empty-http", "Compare HTTP libraries.", ("HTTP",), True)
    runtime = FakeRuntime()
    runner = pe.Runner(runtime, [case], tmp_path, settings(environment="none"))
    with pytest.raises(RuntimeError, match="missing recovery metadata"):
        runner._ensure_workspace(case, {"submission_state": "pending", "client_request_id": "stable"}, "empty")
    assert runtime.create_calls == [] and runtime.submits == []


def test_recovery_of_executed_task_does_not_rerun_or_forget_cost(tmp_path):
    insts = sb.load_instances(write_instances(tmp_path / 'cases.jsonl'))[:1]
    runtime = Flaky(diff_errors=[ApiError(403, 'permission_denied', 'egress quota')],
                    scripts={insts[0].instance_id: Script(cost_usd=.4)})
    out = tmp_path / 'run'
    assert sb.run(runtime, insts, out, settings()).errors == 1
    first = records(out)[insts[0].instance_id]
    assert first['task_finished'] and first['cost_usd'] == .4
    summary = sb.run(runtime, insts, out, settings(budget_usd=0))
    assert summary.finished == 1 and summary.spent_usd == .4
    assert len(runtime.submits) == 1


def test_resuming_failed_stream_uses_existing_task_instead_of_submitting_again(tmp_path):
    insts = sb.load_instances(write_instances(tmp_path / 'cases.jsonl'))[:1]
    runtime = FakeRuntime()
    out = tmp_path / 'run'
    sb.run(runtime, insts, out, settings())
    [rec] = records(out).values()
    rec.update(state='error')
    rec.pop('task_finished')
    (out / 'records' / f'{rec["instance_id"]}.json').write_text(json.dumps(rec))
    summary = sb.run(runtime, insts, out, settings(budget_usd=0))
    assert summary.finished == 1 and len(runtime.submits) == 1


def test_resume_cannot_remove_network_grant_or_version_freeze(tmp_path):
    insts = sb.load_instances(write_instances(tmp_path / 'cases.jsonl'))[:1]
    runtime = FakeRuntime()
    out = tmp_path / 'run'
    frozen = settings(network_hosts=('github.com',), provenance=provenance())
    sb.run(runtime, insts, out, frozen)
    with pytest.raises(sb.ResumeMismatch):
        sb.run(runtime, insts, out, settings())
    with pytest.raises(sb.ResumeMismatch):
        sb.run(runtime, insts, out, dataclasses.replace(frozen, task_usd_limit=2))


def test_tb_verifier_and_prepared_contents_cannot_change_during_resume(tmp_path):
    task = tb.load_task(make_task(tmp_path, 'task-a'))
    out = tmp_path / 'run'
    tb.prepare([task], out, docker=FakeDocker())
    # No task expenditure is needed to freeze a manifest.
    tb.run(FakeRuntime(), [task], out, settings(budget_usd=0, provenance=provenance()))
    (task.path / 'tests' / 'test.sh').write_text('# changed verifier')
    with pytest.raises(sb.ResumeMismatch, match='content changed'):
        tb.run(FakeRuntime(), [task], out, settings(budget_usd=0, provenance=provenance()))


def test_report_distinguishes_executed_export_failure_unrun_and_model_failure():
    assert tb.classify({'state': 'error', 'task_id': 'paid'}, {}, official=True) == 'environment_failure'
    assert tb.classify({'state': 'error'}, {}, official=True) == 'not_run'
    assert tb.classify({'state': 'finished', 'task_id': 'paid', 'environment_used': True}, {'resolved': False}, official=True) == 'model_unsolved'
    assert tb.classify({'state': 'finished', 'environment_used': False}, {'resolved': True}, official=True) == 'environment_failure'


def test_historical_budget_deduplicates_copied_runs_and_fails_unknown_cost(tmp_path):
    paths = []
    for name in ('original', 'copied'):
        root = tmp_path / name
        (root / 'records').mkdir(parents=True)
        (root / 'records' / 'a.json').write_text(json.dumps({'task_id': 'paid', 'cost_usd': 1.5}))
        paths.append(root)
    assert pv.historical_cost(paths)['known_usd'] == 1.5
    (paths[0] / 'records' / 'b.json').write_text(json.dumps({'task_id': 'unknown'}))
    assert pv.historical_cost(paths)['unknown_task_ids'] == ['unknown']
    (paths[1] / 'records' / 'a.json').write_text(json.dumps({'task_id': 'paid', 'cost_usd': 2}))
    with pytest.raises(ValueError, match='conflicting cost'):
        pv.historical_cost(paths)


def test_ledger_counts_every_attempt_and_exact_subagent_node_boundary():
    rows = [row(node='child.2', cost=20), row(node='childish.2', cost=30), row(node='plan', cost=10)]
    result = pe.ledger_metrics([*rows, rows[0]], ['child'], provenance())
    assert result['attempts'] == 3 and result['total_cost_usd'] == .00006 and result['sub_agent_cost_usd'] == .00002
    assert result['known'] and not result['registry_mismatch_reservations']
    rows[0]['actual_usd_micros'] = None
    assert not pe.ledger_metrics(rows, ['child'], provenance())['known']
    rows[1]['registry_revision'] = 'wrong'
    assert pe.ledger_metrics(rows, ['child'], provenance())['registry_mismatch_reservations']


def test_sse_replay_dedupes_events_and_preserves_jev_repeat_evidence():
    events = [
        {'event_id': 1, 'event': 'node_started', 'mode': 'sub_agent', 'node_id': 'child'},
        {'event_id': 2, 'event': 'tool_status', 'state': 'running', 'tool': 'repo_read', 'target_sha256': 'x'},
        {'event_id': 3, 'event': 'tool_status', 'state': 'running', 'tool': 'repo_read', 'target_sha256': 'x'},
        {'event_id': 4, 'event': 'verdict', 'reason_code': 'rule_judge_disowns', 'round_seq': 1},
        {'event_id': 5, 'event': 'verdict', 'reason_code': 'rule_judge_disowns', 'round_seq': 2}]
    observed = pe.observations({'observations': events + events, 'rounds': 3})
    assert observed['tool_calls_observed'] == 2 and observed['repeated_tool_targets'] == 1
    assert observed['jev_vetoes'] == 2 and observed['extra_rounds'] == 2


def test_paired_preflight_rejects_model_or_memory_drift():
    good = plan()
    assert pe.estimate(good)['fits']
    for key, value in (('runtime_commit', 'f' * 40), ('memory', {'revision': 'other', 'corpus_sha256': 'f' * 64})):
        changed = copy.deepcopy(good)
        changed['arms']['rules']['provenance'][key] = value
        with pytest.raises(ValueError, match='differ'):
            pe.validate_plan(changed)


def test_paired_run_stops_on_live_drift_before_any_task(tmp_path):
    config = plan()
    runtime = FakeRuntime()
    with pytest.raises(ValueError, match='does not match'):
        pe.run_arm(config, tmp_path, 'off', runtime=runtime, inspect=lambda argv: provenance('rules'))
    assert not runtime.submits


def test_paired_runs_share_cost_and_only_report_complete_valid_pairs(tmp_path):
    config = plan()
    runtime = FakeRuntime()
    inspect = lambda argv: provenance(argv[-1])
    pe.run_arm(config, tmp_path, 'off', runtime=runtime, inspect=inspect, ledger=lambda task: [row(task)], histories=lambda task: {'sub_agent_nodes': [], 'elapsed_s': 2.0, 'complete': True})
    result = pe.run_arm(config, tmp_path, 'rules', runtime=runtime, inspect=inspect, ledger=lambda task: [row(task)], histories=lambda task: {'sub_agent_nodes': [], 'elapsed_s': 2.0, 'complete': True})
    assert result['paired_cases'] == 1 and result['incomplete_or_invalid'] == []
    assert result['means']['off']['total_cost_usd'] == .1
    assert not result['n16_r_quality_and_gateway_cost_gate'], 'quality did not improve; do not declare acceptance'
    pe.run_arm(config, tmp_path, 'rules', runtime=runtime, inspect=inspect, ledger=lambda task: pytest.fail('duplicate bill'), histories=lambda task: pytest.fail('duplicate history'))
    assert len(runtime.submits) == 2


def test_a_missing_bill_in_other_arm_blocks_new_spending(tmp_path):
    config = plan()
    root = tmp_path / 'rules' / 'single-fact' / 'records'
    root.mkdir(parents=True)
    (root / 'single-fact.json').write_text(json.dumps({'task_id': 'possibly-billable', 'state': 'submitted'}))
    runtime = FakeRuntime()
    with pytest.raises(ValueError, match='uncollected ledger'):
        pe.run_arm(config, tmp_path, 'off', runtime=runtime, inspect=lambda argv: provenance(argv[-1]))
    assert not runtime.submits


def test_actual_primary_model_must_match_even_if_fallback_has_valid_registry():
    frozen = provenance()
    frozen['models']['sub_agent'] = 'child-profile'
    frozen['registry']['child-profile'] = dict(frozen['registry']['profile1'])
    attempt = row(node='main.1')
    attempt['model_profile_id'] = 'child-profile'
    metric = pe.ledger_metrics([attempt], [], frozen)
    assert metric['model_mismatch_reservations'] and not metric['registry_mismatch_reservations']


def test_original_tb_audit_does_not_merge_unversioned_labels(tmp_path):
    from agentruntime_learning.benchmarks.experiment_audit import audit
    roots = []
    for name in ('a', 'b'):
        root = tmp_path / name
        (root / 'records').mkdir(parents=True)
        (root / 'run.json').write_text(json.dumps({'run_id': name, 'settings': {'model_name': 'rev29'}, 'instance_ids': [name]}))
        rec = {'state': 'error', 'task_id': 'paid', 'cost_usd': .5} if name == 'a' else {'state': 'error'}
        (root / 'records' / f'{name}.json').write_text(json.dumps(rec))
        roots.append(root)
    result = audit(roots)
    assert result['execution_counts'] == {'executed_error': 1, 'not_run': 1}
    assert not result['comparable_frozen_baseline'] and result['remaining_usd'] == 79.5


def test_budget_stop_is_distinct_without_erasing_a_verified_solution():
    rec = {'task_id': 'task', 'state': 'finished', 'environment_used': True, 'stop_reason': 'task_usd_limit'}
    assert tb.classify(rec, {'resolved': False}, official=True) == 'budget_exhausted'
    assert tb.classify(rec, {'resolved': True}, official=True) == 'resolved'
    assert tb.budget_exhausted(rec)


def test_tb_execution_prepares_exact_40_and_does_not_increase_other_policy_limits(tmp_path):
    from agentruntime_learning.benchmarks.experiment_audit import prepare_terminalbench
    runs = []
    for index in range(2):
        root = tmp_path / str(index)
        root.mkdir()
        (root / 'run.json').write_text(json.dumps({'instance_ids': [f'case-{n}' for n in range(index * 20, (index + 1) * 20)]}))
        runs.append(root)
    source = {'budget': {'usd': 3, 'tool_calls': 400}, 'model_budget': {'daily_usd': 100},
              'sandbox': {'backend': 'kata', 'quota': {'daily_egress_bytes': 20_000_000_000}}}
    execution = prepare_terminalbench(runs, source, tmp_path / 'prepared')
    seed = json.loads((tmp_path / 'prepared' / 'tenant-policy-seed.json').read_text())['entries'][0]
    assert execution['count'] == 40 and not execution['merge_legacy_results']
    assert seed['tenant'] == 'evaltb20261106' and seed['document']['budget']['usd'] == 1
    assert seed['document']['model_budget'] == {'daily_usd': 40, 'monthly_usd': 40}
    assert source['budget']['usd'] == 3 and seed['document']['sandbox'] == source['sandbox']
    assert 'soft' in execution['budget_semantics']['task_workflow_and_client']


def test_frozen_paired_cases_cover_both_delegation_decisions():
    root = Path(__file__).resolve().parents[1] / 'evalsets' / 'paired' / 'cases.json'
    cases = pe.load_cases(json.loads(root.read_text()))
    assert len(cases) == 10 and sum(c.should_delegate for c in cases) == 5


@pytest.mark.parametrize('scope,revision', [('platform', 'a' * 40), ('_platform', 'v7'), ('platform', '7'), ('platform', 'v0')])
def test_registry_identity_uses_the_real_published_contract(scope, revision):
    frozen = provenance()
    frozen['registry']['profile1'].update(scope=scope, revision=revision)
    with pytest.raises(ValueError, match='real vN'):
        pv.validate(frozen)


def test_worker_normalization_only_removes_the_observed_arm_switch():
    off, rules = provenance('off'), provenance('rules')
    assert off['worker_configuration']['raw_sha256'] != rules['worker_configuration']['raw_sha256']
    assert off['worker_config_sha256'] == rules['worker_config_sha256']
    assert pv.comparable(off, rules, paired=True)
    changed = copy.deepcopy(rules)
    source = changed['worker_configuration']['configuration']
    source['containers'][0]['env'][1]['value'] = 'different-gateway:8080'
    observed = pv.worker_configuration(source)
    changed.update(worker_configuration=observed, worker_config_sha256=observed['normalized_sha256'])
    assert not pv.comparable(off, changed, paired=True)
    changed['worker_config_sha256'] = off['worker_config_sha256']
    with pytest.raises(ValueError, match='hashes'):
        pv.comparable(off, changed, paired=True)


def test_worker_normalization_evidence_cannot_fake_or_hide_an_arm():
    frozen = provenance('off')
    frozen['worker_configuration']['removed'][0]['value'] = 'rules'
    with pytest.raises(ValueError, match='evidence'):
        pv.validate(frozen)
    with pytest.raises(ValueError, match='no observed'):
        pv.worker_configuration({'env': [{'name': 'OTHER', 'value': 'off'}]})
    with pytest.raises(ValueError, match='literal'):
        pv.worker_configuration({'env': [{'name': 'AR_SUB_AGENT_POLICY', 'valueFrom': {'configMapKeyRef': {'name': 'x', 'key': 'mode'}}}]})
    frozen = provenance('off')
    frozen['sub_agent_policy'] = 'rules'
    with pytest.raises(ValueError, match='do not match'):
        pv.validate(frozen)
