from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from temporalio.api.common.v1 import Payload, Payloads, ActivityType, Memo
from temporalio.api.history.v1 import HistoryEvent, WorkflowExecutionStartedEventAttributes, ActivityTaskScheduledEventAttributes, ActivityTaskCompletedEventAttributes
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter

from agentruntime_learning.benchmarks import paired as pe, paired_completion as pc, paired_prepare as pp
from test_paired_evaluation import plan, provenance


def test_mandatory_prompt_is_exact_and_original_baseline_remains():
    cases = pc.completion_cases()
    assert len(cases) == 11 and sum(c['should_delegate'] for c in cases) == 6
    assert cases[-1]['message'] == pc.ORIGINAL_MESSAGE
    assert 'requests、httpx、aiohttp' in cases[-1]['message']
    assert pc.rubric()['mandatory_regression']['historical_task_id'] == pc.OLD_TASK
    assert len(pc.rubric()['semantic_dimensions']) == 5


def test_estimate_separates_soft_targets_and_judge_jev_reserves():
    p = plan()
    p['cost_reserves'] = {'semantic_judge_usd': 1, 'external_jev_usd': .25}
    estimate = pe.estimate(p)
    assert estimate['task_gateway_budget_usd'] == 2.75
    assert estimate['soft_task_target_total_usd'] == 2
    assert not estimate['is_hard_upper_bound']
    assert 'reserved_upper_bound_usd' not in estimate
    p['prior_external_cost_usd'] = 1
    assert not pe.estimate(p)['fits']
    for bad in (float('nan'), float('inf'), -1):
        p['cost_reserves']['external_jev_usd'] = bad
        with pytest.raises(ValueError):
            pe.estimate(p)


def test_structural_evidence_omits_prompts_and_checks_all_branches():
    graph = pc.sanitize_stage({'nodes': [
        {'id': 'a', 'mode': 'sub_agent', 'kind': 'goal', 'goal': 'SECRET', 'after': []},
        {'id': 'b', 'mode': 'sub_agent', 'after': []},
        {'id': 'c', 'mode': 'sub_agent', 'after': []},
        {'id': 'synthesize', 'role': 'act', 'after': ['a', 'b', 'c'], 'params': {'token': 'SECRET'}}]})
    assert 'SECRET' not in json.dumps(graph)
    ev = {'complete': True, 'plans': [{'graph': graph}]}
    assert pc.planning_score(ev, should_delegate=True, arm='rules')['passed']
    assert not pc.planning_score(ev, should_delegate=True, arm='off')['passed']
    graph['nodes'][0]['after'] = ['b']
    assert not pc.planning_score(ev, should_delegate=True, arm='rules')['independent_branches']
    graph['nodes'][1]['after'] = ['a']
    assert pc.planning_score(ev, should_delegate=True, arm='rules')['cyclic']
    assert not pc.planning_score({}, should_delegate=True, arm='rules')['complete']


def test_dense_planning_graphs_are_bounded_and_cycles_remain_invalid():
    # Deterministic work bound, not a flaky wall-clock timeout. A valid graph
    # may have exponentially many paths despite only 64 distinct nodes.
    expansions = 0

    class Node(dict):
        def get(self, key, default=None):
            nonlocal expansions
            if key == 'after':
                expansions += 1
                assert expansions <= 64 * 64 + 128, 'dependency traversal enumerated paths'
            return super().get(key, default)

    nodes = [Node(id=f'n{i}', after=[f'n{j}' for j in range(i)]) for i in range(64)]
    evidence = {'complete': True, 'plans': [{'graph': {'nodes': nodes}}]}
    score = pc.planning_score(evidence, should_delegate=False, arm='off')
    assert score['passed'] and score['valid_dag'] and not score['cyclic']
    expansions = 0
    nodes[0]['after'] = ['n63']
    score = pc.planning_score(evidence, should_delegate=False, arm='off')
    assert score['cyclic'] and not score['valid_dag'] and not score['passed']


async def test_temporal_plan_decodes_real_payloads_and_rejects_cross_tenant():
    converter = DataConverter.default
    memo = (await converter.encode(['tenant1']))[0]
    payload = (await converter.encode([{'triage_level': 'full', 'stage': {'nodes': [{'id': 'read', 'kind': 'goal', 'mode': 'tool_loop', 'after': [], 'prompt': 'SECRET'}]}}]))[0]
    usage = (await converter.encode([{'judge_usage': {'complete': True, 'usage_known': True, 'attempts': [{'task_id': 'task', 'attempt_id': 'r1', 'provider_attempted': True, 'input_tokens': 1000, 'usage_known': True, 'veto_rule_disabled': True}]}}]))[0]
    history = WorkflowHistory('task', [
        HistoryEvent(event_id=1, workflow_execution_started_event_attributes=WorkflowExecutionStartedEventAttributes(memo=Memo(fields={'agentruntime_scope': memo}))),
        HistoryEvent(event_id=2, activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(activity_type=ActivityType(name='plan_round'))),
        HistoryEvent(event_id=3, activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(scheduled_event_id=2, result=Payloads(payloads=[payload]))),
        HistoryEvent(event_id=4, activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(activity_type=ActivityType(name='review_round'))),
        HistoryEvent(event_id=5, activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(scheduled_event_id=4, result=Payloads(payloads=[usage])))])
    result = await pc.plan_evidence(history, 'tenant1')
    assert result['complete'] and 'SECRET' not in json.dumps(result)
    assert result['judge_usage']['cost']['cost_usd'] == pytest.approx(.000042)
    with pytest.raises(ValueError, match='tenant'):
        await pc.plan_evidence(history, 'tenant2')
    history.events[2].activity_task_completed_event_attributes.result.payloads[0].CopyFrom(Payload(metadata={'encoding': b'binary/agentruntime-claim-check'}, data=b'{}'))
    missing = await pc.plan_evidence(history, 'tenant1')
    assert not missing['complete'] and missing['errors'][0]['claim_check']


def test_jev_missing_usage_stays_unknown_and_all_checks_cost():
    checks = [{'task_id': 't', 'attempt_id': 'r1', 'input_tokens': 1000, 'usage_known': True},
              {'task_id': 't', 'attempt_id': 'r2', 'input_tokens': 2000, 'usage_known': True, 'veto_rule_disabled': True}]
    assert pc.jev_cost(checks, 2)['cost_usd'] == pytest.approx(.000126)
    assert not pc.jev_cost(checks, 3)['known']
    checks[1]['input_tokens'] = None
    checks[1]['usage_known'] = False
    assert pc.jev_cost(checks, 2)['cost_usd'] is None


def test_scope_cost_includes_memory_and_rejects_changed_baseline():
    before = [{'reservation_id': 'old', 'task_id': 'old', 'actual_usd_micros': 5}]
    after = before + [{'reservation_id': 'task', 'task_id': 't', 'actual_usd_micros': 100},
                      {'reservation_id': 'embedding', 'task_id': '', 'actual_usd_micros': 10}]
    cost = pc.scope_cost(before, after, {'t'})
    assert cost['total_gateway_usd'] == .00011 and cost['additional_gateway_usd'] == .00001
    altered = copy.deepcopy(after); altered[0]['actual_usd_micros'] = 0
    with pytest.raises(ValueError, match='baseline'):
        pc.scope_cost(before, altered, {'t'})


def test_semantic_review_cannot_drop_hard_case_or_change_evidence():
    export = {'rubric': pc.rubric(), 'rubric_sha256': 'frozen', 'items': [{'id': 'hard', 'evidence_sha256': 'real'}]}
    review = {'rubric_sha256': 'frozen', 'reviewer_version': 'manual-frozen-v1', 'items': [{'id': 'hard', 'evidence_sha256': 'real',
              'scores': {d['name']: {'score': 1, 'reason': 'supported'} for d in pc.rubric()['semantic_dimensions']}}]}
    assert pc.semantic_review(export, review) == {'hard': 1}
    review['items'] = []
    with pytest.raises(ValueError, match='every'):
        pc.semantic_review(export, review)


def test_sensitive_literal_environment_never_enters_receipt():
    with pytest.raises(ValueError, match='sensitive'):
        pp.literal_env({'containers': [{'env': [{'name': 'AR_DEV_SIGNING_KEY', 'value': 'secret'}]}]})
    assert pp.literal_env({'containers': [{'env': [{'name': 'AR_JUDGE_KEY_FILE', 'value': '/private/path'}]}]})


def test_scaffold_is_not_an_observation_and_never_generates_a_ready_plan(tmp_path):
    with pytest.raises(ValueError, match='observed'):
        pp.prepare({'observed': False}, {}, {})


def test_price_estimate_uses_observed_prices_and_token_based_jev():
    settings = {'expected_usage_per_task': {a: {'act': {'calls': 2, 'input_tokens': 1000, 'output_tokens': 500}} for a in ('off', 'rules')},
                'expected_judge_usage': {'calls': 2, 'input_tokens': 1000, 'output_tokens': 500},
                'expected_jev_usage': {'max_calls': 10, 'input_tokens_per_call': 1000, 'usd_per_million_input_tokens': .042, 'price_basis': 'official'}}
    result = pp.price_estimate(settings, {'m': {'in_per_m': 1, 'out_per_m': 2}}, {'act': 'm'}, 'm', 1)
    assert result['model_tasks_usd'] == .008 and result['semantic_judge_usd'] == .004
    assert result['external_jev_usd'] == pytest.approx(.00042)
    assert not result['is_hard_upper_bound']


def deployment_fixture(arm='off'):
    spec = {'containers': [{'name': 'worker', 'image': 'repo/worker:abcdef1', 'env': [{'name': 'AR_SUB_AGENT_POLICY', 'value': arm}]}],
            'volumes': [{'name': 'profiles', 'configMap': {'name': 'profiles'}}]}
    dep = {'metadata': {'uid': 'real-uid', 'generation': 2, 'resourceVersion': '20'},
           'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'worker'}}, 'template': {'spec': spec}},
           'status': {'observedGeneration': 2, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}}
    pod = {'metadata': {}, 'spec': copy.deepcopy(spec), 'status': {'containerStatuses': [
        {'name': 'worker', 'ready': True, 'imageID': 'containerd://sha256:' + 'f' * 64,
         'state': {'running': {'startedAt': '2026-11-06T10:00:00Z'}}}]}}
    cm = {'metadata': {'managedFields': [{'time': '2026-11-06T09:00:00Z'}]}, 'data': {'model-profiles.json': '{}'}}
    def get(ns, resource, name='', **kwargs):
        return dep if resource == 'deployment' else {'items': [pod]} if resource == 'pods' else cm
    return get, dep, pod, cm


def test_live_deployment_observation_checks_running_images_and_loaded_config():
    get, dep, pod, cm = deployment_fixture()
    observed, receipt = pp.deployment('test', 'worker', get)
    assert receipt['deployment_uid'] == 'real-uid'
    assert observed['source']['image_ids']['worker'].endswith('f' * 64)
    cm['metadata']['managedFields'][0]['time'] = '2026-11-06T11:00:00Z'
    with pytest.raises(ValueError, match='after process start'):
        pp.deployment('test', 'worker', get)
    cm['metadata']['managedFields'][0]['time'] = '2026-11-06T09:00:00Z'
    pod['spec']['containers'][0]['image'] = 'repo/worker:old'
    with pytest.raises(ValueError, match='template'):
        pp.deployment('test', 'worker', get)


def test_materialized_provenance_requires_exact_loaded_registry_document():
    raw = '{"cost":{"in_per_m":1,"out_per_m":2},"id":"subject"}'
    profile = json.loads(raw)
    identity = {'scope': 'platform', 'version': 'v7', 'sha256': hashlib.sha256(raw.encode()).hexdigest()}
    env = [{'name': 'AR_MODEL_PROFILES_REQUIRE_REGISTRY', 'value': 'true'}, {'name': 'AR_MODEL_PROVIDER', 'value': 'openai_compat'}]
    source = {'spec': {'containers': [{'name': 'gateway', 'image': 'repo/gateway:abcdef1', 'env': env}]}}
    get, _, _, _ = deployment_fixture()
    worker, _ = pp.deployment('test', 'worker', get)
    worker['source']['configmap_sha256'] = {}
    gateway = {'source': source, 'configmaps': {'catalog': {'model-profiles.json': json.dumps({'profiles': [profile], 'registry_revisions': {'subject': identity}})},
               'policy': {'tenant-policies.json': json.dumps({'tenants': {'test': {'model_allowlist': ['subject'], 'model_budget': {'daily_usd': 20, 'monthly_usd': 20}}}})}}}
    spec = {'workers': ['worker'], 'gateway': 'gateway', 'memory': 'memory', 'runtime_commit': 'abcdef1' + 'a' * 33, 'tenant_id': 'test',
            'models': dict.fromkeys(('plan', 'act', 'route', 'judge', 'sub_agent'), 'subject'), 'semantic_judge_profile': 'subject'}
    db = {'profiles': [{'name': 'subject', 'version': 7, 'document': raw}], 'tenant_policy': {'revision': 'v2', 'document': {}},
          'memory_corpus_sha256': 'e' * 64, 'memory_scopes': []}
    result = pp.materialize(spec, {'worker': worker, 'gateway': gateway}, db)
    assert result['provenance']['registry']['subject']['revision'] == 'v7'
    assert result['provenance']['sub_agent_policy'] == 'off'
    db['profiles'][0]['version'] = 8
    with pytest.raises(ValueError, match='immutable Registry'):
        pp.materialize(spec, {'worker': worker, 'gateway': gateway}, db)
def test_goal_release_tag_still_requires_the_exact_source_commit():
    from agentruntime_learning.benchmarks.paired_prepare import ensure_revision
    revision = "a1234567" + "b" * 32
    source = {"spec": {"containers": [{"image": "registry/worker:goal-a1234567"}]}}
    ensure_revision(source, revision)
    import pytest
    for tag in ("goal-deadbeef", "goal-latest", "other-a1234567", "goal-a1234567-extra"):
        source["spec"]["containers"][0]["image"] = "registry/worker:" + tag
        with pytest.raises(ValueError):
            ensure_revision(source, revision)


def test_observations_allow_only_known_nonsecret_numeric_settings():
    from agentruntime_learning.benchmarks.paired_prepare import literal_env
    import pytest
    fields = {"AR_SECRETS_ENCRYPT": "1", "AR_CONTEXT_EFFECTIVE_TOKENS": "32000", "AR_CONTEXT_OFFLOAD_TOKENS": "2000"}
    assert literal_env({"containers": [{"env": [{"name": k, "value": v} for k, v in fields.items()]}]}) == fields
    for key, value in [("API_TOKEN", "1234"), ("AR_SECRETS_ENCRYPT", "secret"), ("AR_CONTEXT_EFFECTIVE_TOKENS", "https://user:pass@host")]:
        with pytest.raises(ValueError):
            literal_env({"containers": [{"env": [{"name": key, "value": value}]}]})
