"""Read-only, source-free live aggregation of the frozen v3 validation pair.

Quality comes from selected records. All-attempt costs use a bounded identity
index and ledger, checked against current usage directories and call journals.
An absent/stale accounting snapshot never becomes a fabricated settled zero.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Callable
from typing import Any

from agentruntime_learning.evals.audited_gateway import task_identity
from agentruntime_learning.evals.live_context_comparison import MODEL, _model_evidence

ROOT = 'longbench-context-20261110-retrieval-v3'
IDENTITY = ROOT + '-validation'
PROTOCOL = 'longbench-context-source-coverage-v3'
ANSWER_PROTOCOL = 'json-choice-v1-stream-identical-both-arms'
SCHEMA = 'longbench-context-optimization-v1'
ITEM = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$')
SHA = re.compile(r'^[a-f0-9]{64}$')
ARMS = ('baseline', 'candidate')


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode() + b'\n'


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _number(value: Any) -> int | float | None:
    if value is None: return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2**53-1:
        raise ValueError('invalid v3 public measurement')
    return value


def _sum(values: list[Any]) -> int | float | None:
    known = [_number(value) for value in values if value is not None]
    return _number(math.fsum(known)) if known else None


def _ids(value: Any, expected: int | None = None) -> list[str]:
    if not isinstance(value,list) or expected is not None and len(value) != expected or len(value)>10000:
        raise ValueError('invalid v3 cohort')
    if any(not isinstance(v,str) or not ITEM.fullmatch(v) for v in value) or len(set(value)) != len(value):
        raise ValueError('invalid v3 item identity')
    return value


def build_live_focus(read: Callable[[str], bytes], names: Callable[[str], list[str]], definition: dict[str, Any]) -> dict[str, Any]:
    if definition.get('id') != IDENTITY or definition.get('comparison_id') != IDENTITY or definition.get('schema') != SCHEMA or definition.get('protocol') != PROTOCOL:
        raise ValueError('unsupported live v3 comparison definition')
    revision = hashlib.sha256()
    def raw(relative: str, optional: bool = False) -> bytes | None:
        path=ROOT+'/'+relative; revision.update(path.encode()+b'\0')
        try: value=read(path)
        except FileNotFoundError:
            revision.update(b'missing\0')
            if optional:return None
            raise
        revision.update(value+b'\0'); return value
    def doc(relative: str, optional: bool = False) -> Any:
        value=raw(relative,optional);return json.loads(value) if value is not None else None
    plan=doc('validation-plan.json');config=doc('experiment-config.json');cohorts=doc('cohorts.json')
    if not all(isinstance(value,dict) for value in (plan,config,cohorts)):
        raise ValueError('invalid v3 frozen metadata')
    ids=_ids(plan.get('ids'),20)
    excluded=set(_ids(cohorts.get('excluded_prior_ids'))) | set(_ids(cohorts.get('incidental_index_preview_exclusions',[])))
    if ids!=_ids(cohorts.get('validation_ids'),20) or set(ids)&excluded or len(excluded)<80:
        raise ValueError('v3 validation cohort overlaps earlier data')
    if plan.get('phase')!='validation' or plan.get('variants')!=list(ARMS) or plan.get('model')!=MODEL or config.get('model_profile')!=MODEL:
        raise ValueError('v3 arm/model mismatch')
    if (config.get('protocol')!=PROTOCOL or config.get('answer_protocol')!=ANSWER_PROTOCOL or
        config.get('baseline_strategy')!='source_retrieval_v2' or config.get('candidate_strategy')!='source_coverage_v3' or
        config.get('candidate_options')!={'focus_evidence':True}):
        raise ValueError('unrecognized v3 strategy configuration')
    if config.get('selection_sha256')!=_digest(cohorts) or plan.get('config_sha256')!=_digest(config) or plan.get('cohort_sha256')!=_digest(ids):
        raise ValueError('v3 plan hashes do not reconcile')
    if plan.get('runtime_revision')!=config.get('runtime_base_revision') or plan.get('source_sha256')!=config.get('source_sha256') or plan.get('dataset_version')!=cohorts.get('dataset_version'):
        raise ValueError('v3 provenance mismatch')
    if any(not isinstance(plan.get(k),str) or not SHA.fullmatch(plan[k]) for k in ('source_sha256','config_sha256','cohort_sha256')):
        raise ValueError('invalid v3 provenance hash')
    groups={item:{} for item in ids}
    selected=[]
    for item in ids:
        for arm in ARMS:
            row=doc(f'records/validation/{item}--{arm}.json',True)
            if row is None:continue
            if not isinstance(row,dict) or row.get('item_id')!=item or row.get('variant')!=arm or row.get('phase')!='validation':
                raise ValueError('v3 selected item/arm mismatch')
            for key in ('source_sha256','config_sha256','runtime_revision'):
                if row.get(key)!=plan[key]:raise ValueError('v3 selected source/config mismatch')
            run=row.get('run_id'); expected=f'lbr3-validation-{arm}-{item}'
            if not isinstance(run,str) or not re.fullmatch(re.escape(expected)+r'(?:-r[1-9][0-9]*)?',run):raise ValueError('invalid v3 run identity')
            valid,answered,correct=row.get('execution_valid'),row.get('answered'),row.get('correct')
            if type(valid) is not bool or type(answered) is not bool or correct is not None and type(correct) is not bool:
                raise ValueError('invalid v3 outcome flags')
            if valid and (not answered or type(correct) is not bool) or correct is True and not(valid and answered):
                raise ValueError('inconsistent v3 outcome flags')
            if type(row.get('answer_format_valid')) is not bool or correct is True and row['answer_format_valid'] is not True:raise ValueError('invalid v3 answer format flag')
            if valid and _model_evidence(row)!={MODEL}:raise ValueError('v3 selected model mismatch')
            if valid:
                audit=row.get('audit_calls')
                if not isinstance(audit,list) or not audit or len(audit)>4096 or any(not isinstance(call,dict) for call in audit):raise ValueError('missing actual v3 response evidence')
                if any(call.get('state') not in ('completed','unresolved') or call.get('state')=='completed' and call.get('served_model_profile')!=MODEL or call.get('served_model_profile') not in (None,MODEL) for call in audit):raise ValueError('v3 actual response model mismatch')
                answer_calls=[call for call in audit if str(call.get('source_node_id','')).endswith('#answer')]
                if len(answer_calls)!=1 or answer_calls[0].get('state')!='completed' or answer_calls[0].get('served_model_profile')!=MODEL:raise ValueError('missing completed v3 final response')

            if not isinstance(row.get('input_sha256'),str) or not SHA.fullmatch(row['input_sha256']):raise ValueError('missing v3 input identity')
            for peer in groups[item].values():
                if peer['input_sha256']!=row['input_sha256']:raise ValueError('v3 paired inputs differ')
            groups[item][arm]=row;selected.append(row)
    pairs=[group for group in groups.values() if set(group)==set(ARMS)]
    complete=len(pairs)==len(ids)
    quality=complete and all(row['execution_valid'] and row['answered'] and row['answer_format_valid'] for pair in pairs for row in pair.values())

    # Index is a discovery aid only. Verify every entry's deterministic identity
    # and compare it with the live usage directory inventory and call journals.
    index=doc('reports/usage-identities.json',True);ledger=doc('reports/ledger.json',True)
    accounting_complete=isinstance(index,list) and isinstance(ledger,list)
    mapped={}; arm_tasks={arm:set() for arm in ARMS}; expected_nodes={arm:set() for arm in ARMS}
    if index is not None and (not isinstance(index,list) or len(index)>2048):raise ValueError('invalid v3 usage identity index')
    for entry in index or []:
        if not isinstance(entry,dict):raise ValueError('invalid v3 usage identity')
        run=entry.get('run_id'); phase=entry.get('phase');arm=entry.get('variant');item=entry.get('item_id')
        if phase not in ('development','validation') or arm not in ARMS or not isinstance(item,str) or not ITEM.fullmatch(item):raise ValueError('invalid indexed v3 run')
        expected=f'lbr3-{phase}-{arm}-{item}'
        if not isinstance(run,str) or not re.fullmatch(re.escape(expected)+r'(?:-r[1-9][0-9]*)?',run) or run in mapped or entry.get('task_id')!=task_identity(run):raise ValueError('v3 usage identity does not reconcile')
        identity=doc(f'usage/{run}/identity.json')
        if identity.get('run_id')!=run or identity.get('task_id')!=entry['task_id'] or identity.get('tenant_id')!='evals':raise ValueError('v3 actual usage identity mismatch')
        mapped[run]=entry
        if phase!='validation':continue
        if item not in groups:raise ValueError('indexed validation item outside fixed cohort')
        arm_tasks[arm].add(entry['task_id'])
        journal=raw(f'usage/{run}/calls.jsonl',True)
        if journal is None:accounting_complete=False;continue
        for line in journal.splitlines():
            call=json.loads(line)
            if call.get('task_id')!=entry['task_id'] or not re.fullmatch(r'eval_call_[0-9]{8}',str(call.get('node_id',''))):raise ValueError('invalid v3 call journal identity')
            expected_nodes[arm].add((entry['task_id'],call['node_id']))
    usage_names=names(ROOT+'/usage')
    if len(usage_names)>2048:raise ValueError('too many v3 usage directories')
    revision.update(json.dumps(sorted(usage_names)).encode()+b'\0')
    if set(usage_names)!=set(mapped) or any(row['run_id'] not in mapped for row in selected):accounting_complete=False
    if ledger is not None and (not isinstance(ledger,list) or len(ledger)>8192):raise ValueError('invalid v3 ledger')
    valid_tasks={task_identity(row['run_id']) for row in selected if row['execution_valid'] and row['answered'] and row['answer_format_valid']}
    indexed_tasks={entry['task_id'] for entry in mapped.values()}
    bills={arm:[] for arm in ARMS};seen=set()
    for bill in ledger or []:
        if not isinstance(bill,dict):raise ValueError('invalid v3 ledger row')
        key=(bill.get('task_id'),bill.get('node_id'))
        if key in seen:raise ValueError('duplicate v3 ledger call')
        seen.add(key)
        if bill.get('task_id') not in indexed_tasks:accounting_complete=False
        if bill.get('task_id') in valid_tasks and bill.get('model_profile_id')!=MODEL:raise ValueError('valid v3 answer differs from actual ledger model')
        for field in ('actual_usd_micros','reserved_usd_micros','input_tokens','output_tokens'):_number(bill.get(field))
        for arm in ARMS:
            if bill.get('task_id') in arm_tasks[arm]:bills[arm].append(bill)
    arm_cost_complete={}; arm_coverage_complete={}
    for arm in ARMS:
        observed={(bill['task_id'],bill['node_id']) for bill in bills[arm]}
        arm_coverage_complete[arm]=accounting_complete and bool(expected_nodes[arm]) and observed==expected_nodes[arm]
        arm_cost_complete[arm]=arm_coverage_complete[arm] and all(bill.get('actual_usd_micros') is not None and bill.get('input_tokens') is not None and bill.get('output_tokens') is not None for bill in bills[arm])
    arms={}
    for arm in ARMS:
        scored=[pair[arm] for pair in pairs];rows=[row for row in selected if row['variant']==arm];valid=[row for row in rows if row['execution_valid'] and row['answered'] and row['answer_format_valid']]
        calls=[call for row in valid for call in row.get('calls',[])]
        focus=[]
        for row in valid:
            strategy=row.get('strategy_stats',{})
            if not isinstance(strategy,dict) or not isinstance(strategy.get('focus',{}),dict):raise ValueError('invalid v3 strategy stats')
            if arm=='candidate' and (strategy.get('focus_enabled') is not True or strategy.get('expand_options') is not False or not isinstance(strategy.get('focus'),dict)):
                raise ValueError('missing expected v3 focus stats')
            stats=strategy.get('focus',{})
            for key in ('accepted_quotes','rejected_quotes','raw_batches_retained'):
                if arm=='candidate' and key not in stats:raise ValueError('missing v3 focus measurement')
                value=stats.get(key,0)
                if type(value) is not int or _number(value) is None:raise ValueError('invalid v3 focus count')
            focus.append(stats)
        input_tokens=_sum([bill.get('input_tokens') for bill in bills[arm]])
        output_tokens=_sum([bill.get('output_tokens') for bill in bills[arm]])
        known=_sum([bill.get('actual_usd_micros') for bill in bills[arm]])
        context=[_number(row.get('context_tokens')) for row in valid if row.get('context_tokens') is not None]
        arms[arm]={'run_id':IDENTITY+'-'+arm,'paired_sample_count':len(pairs),
            'correct_count':sum(row['correct'] is True for row in scored),'infrastructure_error_count':sum(not(row['execution_valid'] and row['answered'] and row['answer_format_valid']) for row in scored),
            'answered_count':sum(row['answered'] for row in rows),'answer_format_failure_count':sum(row['answered'] and row.get('answer_format_valid') is False for row in rows),
            'input_tokens':input_tokens,'output_tokens':output_tokens,'total_tokens':input_tokens+output_tokens if input_tokens is not None and output_tokens is not None else None,
            'answer_input_tokens':_sum([call.get('input_tokens') for call in calls if call.get('phase')=='answer']),
            'helper_input_tokens':_sum([call.get('input_tokens') for call in calls if call.get('phase')!='answer']) if arm=='candidate' else 0 if valid else None,
            'cost_usd':known/1e6 if known is not None and arm_cost_complete[arm] else None,
            'elapsed_seconds':_sum([row.get('elapsed_seconds') for row in valid]),
            'usage_known':len(valid)==len(rows),'elapsed_known':len(valid)==len(rows),
            'summary_calls':sum(call.get('phase')=='summary' for call in calls),
            'evidence_accepted_quotes':_sum([s.get('accepted_quotes',0) for s in focus]),'evidence_rejected_quotes':_sum([s.get('rejected_quotes',0) for s in focus]),
            'fallback_batches':_sum([s.get('raw_batches_retained',0) for s in focus]),
            'context_tokens_mean':sum(context)/len(context) if context else None}
    all_bills=[bill for arm in ARMS for bill in bills[arm]]
    known=_sum([bill.get('actual_usd_micros') for bill in all_bills])
    outstanding=[bill for bill in all_bills if bill.get('actual_usd_micros') is None]
    pending=(math.fsum(bill['reserved_usd_micros'] for bill in outstanding) if outstanding else 0) if all(arm_coverage_complete.values()) and all(bill.get('reserved_usd_micros') is not None for bill in outstanding) else None
    cost_known=complete and all(arm_cost_complete.values())
    result={'schema':SCHEMA,'id':IDENTITY,'comparison_id':IDENTITY,'protocol':PROTOCOL,
        'evaluation_type':'ablation','evaluation_scope':'component','split':'validation','answer_protocol':ANSWER_PROTOCOL,
        'runtime_revision':plan['runtime_revision'],'runtime_source_sha256':plan['source_sha256'],'config_sha256':plan['config_sha256'],'cohort_sha256':plan['cohort_sha256'],
        'dataset_name':'LongBench v2','dataset_version':plan['dataset_version'],'model_profile':MODEL,
        'sample_count':20,'completed_count':len(pairs),'repeats':1,'collection_complete':complete,'status':'completed' if complete else 'running',
        'quality_valid':quality,'validation_cohort_frozen_before_tuning':True,'arms':arms,
        'cost_known':cost_known,'actual_cost_usd':known/1e6 if cost_known and known is not None else None,
        'known_cost_usd':known/1e6 if known is not None else None,'pending_reserved_usd':pending/1e6 if pending is not None else None,
        'updated_at':definition.get('updated_at'),'completed_at':definition.get('completed_at'),
        'source_revision':revision.hexdigest()}
    if quality:
        differences=[int(pair['candidate']['correct'])-int(pair['baseline']['correct']) for pair in pairs]
        gained=differences.count(1);lost=differences.count(-1);discordant=gained+lost
        rng=random.Random(0);bootstrap=sorted(sum(rng.choices(differences,k=20))/20 for _ in range(10000))
        result['paired']={'gained':gained,'lost':lost,'accuracy_delta':sum(differences)/20,
            'accuracy_delta_ci95':[bootstrap[249],bootstrap[9749]],
            'mcnemar_exact_two_sided_p':min(1,2*sum(math.comb(discordant,k) for k in range(min(gained,lost)+1))/2**discordant) if discordant else 1}
    return result
