"""Frozen answers are judged once, under one durable cap and real ledger gate."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.benchmarks import paired_completion as pc
from agentruntime_learning.benchmarks import paired_semantic_judge as sj
from agentruntime_learning.benchmarks import provenance as pv
from agentruntime_learning.evals.audited_gateway import task_identity

PROFILE = 'judge-profile-v1'
TENANT = 'paired-semantic-test'
ADDRESS = 'gateway.test:8091'


def fixture():
    cases = pc.completion_cases()
    plan = {
        'run_id': 'paired-semantic-test', 'budget_usd': 40, 'cases': cases,
        'cost_reserves': {'semantic_judge_usd': 7, 'external_jev_usd': 12.5},
        'arms': {arm: {'provenance': {
            'semantic_judge_profile': PROFILE,
            'registry': {PROFILE: {'scope': 'platform', 'revision': 'v7', 'document_sha256': 'a' * 64}},
        }} for arm in ('off', 'rules')},
        'completion': {
            'version': pc.VERSION, 'rubric': pc.rubric(), 'cases_sha256': pv.digest(cases),
            'original_message_sha256': hashlib.sha256(pc.ORIGINAL_MESSAGE.encode()).hexdigest(),
            'price_estimate': {'semantic_judge_usd': .03, 'judge_assumptions': {'calls': 22, 'input_tokens': 10000, 'output_tokens': 2000}},
            'observation_receipts': {'off': {'receipt': 'off'}, 'rules': {'receipt': 'rules'}},
        },
    }
    items = []
    for case in cases:
        for arm in ('off', 'rules'):
            body = {
                'input': case['message'], 'actual_output': 'Frozen answer to ' + case['id'],
                'context': [f['content'] for f in case.get('files', [])],
                'planning': {'complete': True, 'plans': [{'graph': {'nodes': [{'id': 'answer', 'mode': 'tool_loop', 'after': []}]}}]},
                'tool_evidence': {'available': True, 'calls': []},
            }
            items.append({'id': pv.digest([plan['run_id'], arm, case['id']])[:24], 'evidence_sha256': pv.digest(body), **body})
    export = {'rubric': pc.rubric(), 'rubric_sha256': pv.digest(pc.rubric()),
              'items': sorted(items, key=lambda x: x['id']), 'judge_reserve_usd': 7,
              'instructions': 'Evaluate every preregistered dimension on existing answers only.'}
    return plan, export


def judgment(item):
    return {'scores': {d['name']: {'score': 1, 'reason': 'The preserved evidence supports this dimension.'}
                       for d in pc.rubric()['semantic_dimensions']}}


class FakeGateway:
    def __init__(self, plan, export, *, fail_at=None, malformed_at=None, served=PROFILE, finish_reason=mg.FINISH_REASON_STOP):
        self.task_id = task_identity(plan['run_id'] + '-semantic-judge')
        self.tenant = TENANT
        self.identity = {'gateway_address': ADDRESS}
        self.sequence = 0
        self.items = export['items']
        self.requests, self.rows = [], []
        self.fail_at, self.malformed_at, self.served = fail_at, malformed_at, served
        self.finish_reason = finish_reason

    async def generate(self, request):
        self.sequence += 1
        self.requests.append(type(request).FromString(request.SerializeToString()))
        if self.sequence == self.fail_at:
            raise TimeoutError('synthetic transport outcome unknown')
        item = self.items[self.sequence - 1]
        row = judgment(item)
        response = mg.GenerateResponse(text='not-json' if self.sequence == self.malformed_at else json.dumps(row))
        response.model.model_profile_id = self.served
        response.model.tier = 'strong@medium'
        response.finish_reason = self.finish_reason
        response.usage.cost_usd_micros = 1000
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        self.rows.append({'reservation_id': f'usage-{self.sequence}', 'task_id': self.task_id,
                          'node_id': f'eval_call_{self.sequence:08d}', 'model_profile_id': self.served,
                          'method': 'generate', 'actual_usd_micros': 1000,
                          'registry_scope': 'platform', 'registry_revision': 'v7',
                          'registry_document_sha256': 'a' * 64})
        return response

    async def ledger(self):
        return {'tenant_id': self.tenant, 'task_id': self.task_id, 'attempts': copy.deepcopy(self.rows)}

    async def aclose(self):
        pass


def prepare(tmp_path):
    plan, export = fixture()
    out = tmp_path / 'judge'
    manifest = sj.prepare(plan, export, out, tenant_id=TENANT, gateway_address=ADDRESS)
    return plan, export, out, manifest


def test_prepare_requires_complete_22_item_export_and_preserves_it(tmp_path):
    plan, export, out, manifest = prepare(tmp_path)
    assert len(export['items']) == 22
    assert manifest['task_id'] == task_identity(plan['run_id'] + '-semantic-judge')
    assert not (out / 'review.json').exists()
    assert export['rubric'] == pc.rubric()
    assert manifest['max_output_tokens'] == 2000 and manifest['thinking_budget_tokens'] == 0
    assert manifest['system_prompt'] == sj.SYSTEM and len(manifest['request_sha256']) == 22


@pytest.mark.parametrize('visible,thinking', [(0, 0), (-1, 0), (True, 0), (1000.0, 0),
    (1000, -1), (1000, True), (1000, .5), (2000, 1)])
def test_generation_limits_cannot_expand_or_round_the_original_estimate(tmp_path, visible, thinking):
    plan, export = fixture()
    with pytest.raises(sj.JudgeError, match='original frozen output estimate'):
        sj.prepare(plan, export, tmp_path / 'judge', tenant_id=TENANT, gateway_address=ADDRESS,
                   max_output_tokens=visible, thinking_budget_tokens=thinking)
    assert not (tmp_path / 'judge' / 'manifest.json').exists()


async def test_explicit_visible_and_thinking_limits_reach_all_22_requests(tmp_path):
    plan, export = fixture()
    plan['completion']['price_estimate']['judge_assumptions']['output_tokens'] = 12000
    before = copy.deepcopy(plan)
    out = tmp_path / 'judge'
    manifest = sj.prepare(plan, export, out, tenant_id=TENANT, gateway_address=ADDRESS,
                          max_output_tokens=3000, thinking_budget_tokens=8192)
    gateway = FakeGateway(plan, export)
    result = await sj.run(out, gateway, gateway.ledger)
    assert len(result['items']) == 22 and manifest['budget_usd'] == 7
    assert plan == before
    assert all(request.generation.max_output_tokens == 3000 and request.generation.thinking_budget_tokens == 8192
               for request in gateway.requests)
    with pytest.raises(sj.JudgeError, match='prepared judge inputs changed'):
        sj.prepare(plan, export, out, tenant_id=TENANT, gateway_address=ADDRESS,
                   max_output_tokens=3001, thinking_budget_tokens=8192)


def test_blinding_removes_internal_judgments_but_preserves_all_plan_and_source_evidence(tmp_path):
    plan, export = fixture()
    item = export['items'][0]
    item['context'] = [{'path': 'fixtures/client.txt', 'content': 'source facts'},
                       {'primary_reference': {'source': 'https://official.example/docs', 'facts': ['HTTP fact']}}]
    item['tool_evidence'] = {'events': [{'tool': 'repo_read', 'path': 'fixtures/client.txt', 'result': 'source facts'}]}
    item['planning'] = {'complete': True, 'judge_usage': {'verdict': 'INTERNAL_VERDICT', 'score': .9},
        'plans': [{'triage_level': 'full', 'graph': {'nodes': [{'id': 'first', 'after': []}]}, 'judge_score': 'INTERNAL_SCORE'},
                  {'triage_level': 'replan', 'graph': {'nodes': [{'id': 'second', 'after': []}]}, 'judge_usage': 'INTERNAL_CALL'}]}
    item['evidence_sha256'] = pv.digest({key: item[key] for key in sj.FIELDS})
    original = copy.deepcopy(export)
    manifest = sj.prepare(plan, export, tmp_path / 'judge', tenant_id=TENANT, gateway_address=ADDRESS)
    request = sj._request(manifest, export, item)
    material = json.loads(request.generation.messages[1].text)
    assert material['rubric'] == pc.rubric()
    assert material['material']['planning'] == {'plans': [
        {key: row[key] for key in ('triage_level', 'graph')} for row in item['planning']['plans']]}
    assert not any(marker in request.generation.messages[1].text for marker in ('INTERNAL_VERDICT', 'INTERNAL_SCORE', 'INTERNAL_CALL'))
    for key in ('input', 'actual_output', 'context', 'tool_evidence'):
        assert material['material'][key] == item[key]
    assert export == original
    assert sj.validate_scores(item, judgment(item))['evidence_sha256'] == original['items'][0]['evidence_sha256']


@pytest.mark.parametrize('field,value', [('max_output_tokens', 1999), ('thinking_budget_tokens', 1),
    ('system_prompt', 'different prompt'), ('material_version', 'different projection'),
    ('tier', 'strong@low'), ('request_sha256', {})])
async def test_changed_manifest_cannot_make_a_new_call_or_resume(tmp_path, field, value):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export)
    await sj.run(out, gateway, gateway.ledger)
    manifest = json.loads((out / 'manifest.json').read_text())
    manifest[field] = value
    (out / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(sj.JudgeError):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 22


async def test_changed_request_construction_is_detected_before_first_rpc(tmp_path, monkeypatch):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export)
    original_request = sj._request
    def changed(*args):
        request = original_request(*args)
        request.generation.messages[1].text += '\nNew scoring instruction.'
        return request
    monkeypatch.setattr(sj, '_request', changed)
    with pytest.raises(sj.JudgeError, match='immutable judgment inputs'):
        await sj.run(out, gateway, gateway.ledger)
    assert not gateway.requests


def test_prepare_cli_freezes_explicit_token_split_without_execution(tmp_path):
    plan, export = fixture()
    plan['completion']['price_estimate']['judge_assumptions']['output_tokens'] = 12000
    plan_path, export_path = tmp_path / 'plan.json', tmp_path / 'export.json'
    plan_path.write_text(json.dumps(plan)); export_path.write_text(json.dumps(export))
    out = tmp_path / 'judge'
    assert sj.main(['prepare', '--out', str(out), '--plan', str(plan_path), '--export', str(export_path),
                    '--tenant', TENANT, '--gateway', ADDRESS, '--max-output-tokens', '3000',
                    '--thinking-budget-tokens', '8192']) == 0
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['max_output_tokens'] == 3000 and manifest['thinking_budget_tokens'] == 8192
    assert not (out / 'calls.jsonl').exists() and not (out / 'budget.json').exists()


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'body', 'rubric', 'reserve', 'profile', 'registry'])
def test_prepare_rejects_changed_cohort_evidence_or_frozen_contract(tmp_path, change):
    plan, export = fixture()
    if change == 'missing': export['items'].pop()
    elif change == 'duplicate': export['items'][-1] = copy.deepcopy(export['items'][0])
    elif change == 'body': export['items'][0]['actual_output'] = 'Changed after collection'
    elif change == 'rubric': export['rubric']['acceptance']['minimum_semantic_quality_gain'] = 0
    elif change == 'reserve': export['judge_reserve_usd'] = 8
    elif change == 'profile':
        plan['arms']['rules']['provenance']['semantic_judge_profile'] = 'different-profile'
        plan['arms']['rules']['provenance']['registry']['different-profile'] = copy.deepcopy(plan['arms']['rules']['provenance']['registry'][PROFILE])
    elif change == 'registry': plan['arms']['rules']['provenance']['registry'][PROFILE]['revision'] = 'v8'
    with pytest.raises(ValueError):
        sj.prepare(plan, export, tmp_path / 'judge', tenant_id=TENANT, gateway_address=ADDRESS)


@pytest.mark.parametrize('change', ['bool', 'unsupported_anchor', 'missing_dimension', 'blank_reason', 'wrong_id', 'wrong_hash'])
def test_score_validation_keeps_exact_anchors_dimensions_and_evidence(change):
    _, export = fixture()
    item = export['items'][0]
    row = judgment(item)
    name = next(iter(row['scores']))
    if change == 'bool': row['scores'][name]['score'] = True
    elif change == 'unsupported_anchor': row['scores'][name]['score'] = .75
    elif change == 'missing_dimension': row['scores'].pop(name)
    elif change == 'blank_reason': row['scores'][name]['reason'] = '  '
    elif change == 'wrong_id': row['id'] = 'a' * 24
    elif change == 'wrong_hash': row['evidence_sha256'] = 'b' * 64
    with pytest.raises(ValueError):
        sj.validate_scores(item, row)


async def test_complete_review_uses_one_cap_and_resume_does_not_call_models_again(tmp_path):
    plan, export, out, manifest = prepare(tmp_path)
    gateway = FakeGateway(plan, export)
    result = await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 22
    assert len(result['items']) == 22
    assert result['actual_cost_usd'] == pytest.approx(.022)
    assert len(pc.semantic_review(export, result)) == 22
    assert all(r.call.pinned_model_profile == PROFILE for r in gateway.requests)
    assert all(r.call.max_cost_usd_micros <= 7_000_000 for r in gateway.requests)
    assert all(r.generation.max_output_tokens == 2000 for r in gateway.requests)
    assert all(r.tenant.tenant_id == 'evals' for r in gateway.requests), 'audited adapter alone rewrites to the explicit experiment tenant'
    assert gateway.requests[-1].call.max_cost_usd_micros < gateway.requests[0].call.max_cost_usd_micros
    resumed = await sj.run(out, gateway, gateway.ledger)
    assert resumed == result and len(gateway.requests) == 22


async def test_unknown_call_remains_pending_and_cannot_be_blindly_retried(tmp_path):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export, fail_at=2)
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 2 and not (out / 'review.json').exists()
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 2, 'the existing paid/unknown request must not run again'


async def test_paid_malformed_judgment_stops_and_is_not_rebilled_on_resume(tmp_path):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export, malformed_at=1)
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 1 and not (out / 'review.json').exists()
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('change', ['pending', 'registry', 'scope', 'model', 'extra_node', 'missing_node', 'cost', 'tenant'])
async def test_final_review_requires_exact_authoritative_ledger(tmp_path, change):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export)
    async def bad_ledger():
        result = await gateway.ledger()
        if not result['attempts'] and change != 'tenant': return result
        if change == 'pending': result['attempts'][0]['actual_usd_micros'] = None
        elif change == 'registry': result['attempts'][0]['registry_revision'] = 'v8'
        elif change == 'scope': result['attempts'][0]['registry_scope'] = 'someone-else'
        elif change == 'model': result['attempts'][0]['model_profile_id'] = 'other-model'
        elif change == 'extra_node': result['attempts'].append({**result['attempts'][0], 'reservation_id': 'extra', 'node_id': 'unrelated'})
        elif change == 'missing_node': result['attempts'].pop()
        elif change == 'cost': result['attempts'][0]['actual_usd_micros'] = 0
        elif change == 'tenant': result['tenant_id'] = 'other-tenant'
        return result
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, bad_ledger)
    assert not (out / 'review.json').exists()
    assert len(gateway.requests) <= 22


async def test_wrong_gateway_or_served_model_stops_before_further_spend(tmp_path):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export)
    gateway.identity['gateway_address'] = 'other-gateway:8091'
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert not gateway.requests
    gateway.identity['gateway_address'] = ADDRESS
    gateway.served = 'fallback-profile'
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 1 and not (out / 'review.json').exists()


@pytest.mark.parametrize('finish_reason', [mg.FINISH_REASON_MAX_TOKENS, mg.FINISH_REASON_TOOL_CALLS, mg.FINISH_REASON_UNSPECIFIED])
async def test_nonterminal_or_truncated_judgment_is_not_a_success_even_with_valid_json(tmp_path, finish_reason):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export, finish_reason=finish_reason)
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 1 and not (out / 'review.json').exists()
    assert json.loads((out / 'budget.json').read_text())['spent_usd_micros'] == 1000
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 1, 'a charged invalid finish must not be called again'


async def test_resume_rejects_changed_raw_response_without_new_model_calls(tmp_path):
    plan, export, out, _ = prepare(tmp_path)
    gateway = FakeGateway(plan, export)
    await sj.run(out, gateway, gateway.ledger)
    original = json.loads((out / 'review.json').read_text())
    response = out / (export['items'][0]['id'] + '.response.json')
    body = json.loads(response.read_text())
    body['text'] = 'The saved provider response was changed after billing.'
    response.write_text(json.dumps(body))
    with pytest.raises((ValueError, RuntimeError)):
        await sj.run(out, gateway, gateway.ledger)
    assert len(gateway.requests) == 22
    assert json.loads((out / 'review.json').read_text()) == original
