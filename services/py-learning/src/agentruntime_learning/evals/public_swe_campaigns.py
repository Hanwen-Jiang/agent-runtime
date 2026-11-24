"""Join public SWE run stages by an explicit campaign manifest and official per-case verdicts."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from .public_swe_progress import VERIFIED_EXPORT

MANIFEST = json.loads(Path(__file__).with_name('swe_campaign_manifest.json').read_text(encoding='utf-8'))
VERDICTS = frozenset({'resolved', 'unresolved'})
GRADE_LABELS = VERDICTS | {'empty_patch', 'run_error', 'error', 'infra_failure', 'ambiguous_failure', 'not_graded'}
SCORED_STAGES = {arm[k] for arm in MANIFEST['arms'].values()
                 for k in ('initial_run_id', 'recovery_run_id', 'infra_run_id')}


def public_case_grades(row, report):
    """Project labels only for declared stages on verified public issue IDs."""
    if row['run_id'] not in SCORED_STAGES or row.get('dataset_sha256') != VERIFIED_EXPORT:
        return {}
    ids = row['selection_info'].get('selection_ids', [])
    if len(ids) != row['selected']:
        return {}
    allowed_ids = set(ids)
    return {r['instance_id']: r['grading'] for r in report['instances']
            if r['instance_id'] in allowed_ids and r.get('grading') in GRADE_LABELS}


def matching_run(by_id, run_id, ids):
    row = by_id.get(run_id, {})
    info = row.get('selection_info', {})
    if (info.get('dataset_sha256') == VERIFIED_EXPORT and info.get('split') == 'test'
            and set(info.get('selection_ids', [])) == set(ids) and row.get('execution_valid') is not False):
        return row
    return {}


def matching_recovery(by_id, spec, arm, infra=False):
    mode = 'missing_verdict_recovery' if infra else 'second_attempt_quota_recovery'
    stage = 'infra' if infra else 'recovery'
    row = matching_run(by_id, spec[stage+'_run_id'], spec[stage+'_ids'])
    if (row.get('model') != spec[stage+'_model'] or row.get('protocol') != 'official-workspace-patch-v1:no-hints'
            or row.get('recovery_source_status')):
        return {}
    source = row.get('recovery_source')
    if source is not None or row.get('runtime_tree') is not None:
        if (row.get('runtime_tree') == spec['runtime_tree']
                and source == {'parent_run_id':spec['initial_run_id'], 'arm':arm, 'mode':mode}):
            return row
        return {}
    # Older public projections did not retain the recovery-protocol metadata. Only
    # the exact previously checked run+report bytes may stand in for that binding.
    if not infra and row.get('source_sha256') == spec['recovery_archive_sha256']:
        return row
    return {}


def swe_campaigns(runs):
    by_id = {r['run_id']: r for r in runs if r.get('suite') == 'swebench'}
    initial = {arm: by_id.get(spec['initial_run_id'], {}) for arm, spec in MANIFEST['arms'].items()}
    selection = initial['a'].get('selection_info', {})
    ids = selection.get('selection_ids', [])
    if len(ids) != 50 or sha256(('\n'.join(ids) + '\n').encode()).hexdigest() != MANIFEST['ordered_ids_sha256']:
        return []

    cases = {iid: {'instance_id': iid} for iid in ids}
    arms = []
    members = []
    ignored = []
    for arm, spec in MANIFEST['arms'].items():
        base = matching_run(by_id, spec['initial_run_id'], ids)
        base_grades = base.get('case_verdicts', {})
        if (len(base_grades) != len(ids) or base.get('model') != spec['initial_model']
                or base['selection_info']['selection_ids'] != ids or base.get('runtime_tree') != spec['runtime_tree']
                or base.get('protocol') != 'official-workspace-patch-v1:no-hints'):
            return []
        # These are official regrades of the original B patches, each attached to a
        # previously ungraded issue. A paid generation retry has a different run ID.
        regrades = []
        trusted_original = base.get('report_sha256') == spec['initial_report_sha256']
        if spec['regrades'] and not trusted_original:
            ignored.append({'run_id': spec['initial_run_id'], 'reason': 'same_patch_regrades_original_report_mismatch'})
        for receipt in spec['regrades'] if trusted_original else []:
            iid, = receipt['completed_ids']
            if (iid not in cases or set(receipt['resolved_ids']) | set(receipt['unresolved_ids']) != {iid}
                    or set(receipt['resolved_ids']) & set(receipt['unresolved_ids'])):
                raise ValueError('invalid same-patch regrade receipt')
            regrades.append({'instance_id': iid, 'grade': 'resolved' if receipt['resolved_ids'] else 'unresolved',
                             'source_sha256': receipt['sha256'], 'artifact': receipt['file']})
        corrected = {r['instance_id']: r['grade'] for r in regrades}
        retry = matching_recovery(by_id, spec, arm)
        infra = matching_recovery(by_id, spec, arm, infra=True)
        for rid, matched in ((spec['recovery_run_id'],retry), (spec['infra_run_id'],infra)):
            if not matched and by_id.get(rid, {}).get('source') == 'benchmark_artifacts':
                ignored.append({'run_id':rid, 'reason':'recovery_provenance_mismatch'})
        counts = Counter()
        for iid in ids:
            labels = {'initial': base_grades[iid], 'regrade': corrected.get(iid),
                      'retry': retry.get('case_verdicts', {}).get(iid),
                      'infra': infra.get('case_verdicts', {}).get(iid)}
            # Supplement only issues with no official test verdict. A later generation
            # cannot upgrade an already judged miss or invalidate an earlier success.
            verdicts = [(stage,label) for stage,label in labels.items() if label in VERDICTS]
            stage, effective = verdicts[0] if verdicts else (None,None)
            if effective == 'resolved':
                counts[stage] += 1
            for other_stage, other_label in verdicts[1:]:
                if other_label != effective:
                    ignored.append({'instance_id':iid, 'arm':arm, 'stage':other_stage,
                                    'reason':'already_graded', 'retained':effective})
            counts['graded'] += effective is not None
            cases[iid][arm] = {**labels, 'resolved': effective == 'resolved', 'graded': effective is not None}

        members.extend([spec['initial_run_id'], spec['recovery_run_id'], spec['infra_run_id']])
        arms.append({'arm': arm, 'runtime_tree': spec['runtime_tree'], 'initial_run_id': spec['initial_run_id'],
                     'recovery_run_id': spec['recovery_run_id'], 'infra_run_id': spec['infra_run_id'],
                     'initial_resolved': counts['initial'], 'regraded_resolved': counts['regrade'], 'regrades': regrades,
                     'retry_selected': len(spec['recovery_ids']), 'retry_resolved': counts['retry'] if retry.get('case_verdicts') else None,
                     'infra_selected': len(spec['infra_ids']), 'infra_resolved': counts['infra'] if infra.get('case_verdicts') else None,
                     'infra_graded': sum(v in VERDICTS for v in infra.get('case_verdicts', {}).values()),
                     'graded_count': counts['graded'], 'cumulative_resolved': sum(counts[k] for k in ('initial','regrade','retry','infra'))})

    diagnostics = []
    for spec in MANIFEST['diagnostics']:
        results = []
        diagnostic_ids = spec.get('ids')
        for rid in spec['run_ids']:
            if diagnostic_ids is None:
                continue
            row = matching_run(by_id, rid, diagnostic_ids)
            if row.get('source_sha256') != spec['archives'][rid]:
                row = {}
            tested = row.get('graded_count')
            results.append({'run_id': rid, 'resolved': row.get('resolved') if tested else None, 'graded_count': tested})
        members.extend(spec['run_ids'])
        diagnostics.append({'kind': spec['kind'], 'run_ids': spec['run_ids'], 'selected': len(diagnostic_ids) if diagnostic_ids else None,
                            'results': results})
    solved_a, solved_b = ({i for i in ids if cases[i][arm]['resolved']} for arm in ('a', 'b'))
    paired = {'both_resolved': len(solved_a & solved_b), 'only_a_ids': [i for i in ids if i in solved_a - solved_b],
              'only_b_ids': [i for i in ids if i in solved_b - solved_a],
              'neither_resolved': len(ids) - len(solved_a | solved_b),
              'both_graded': sum(c['a']['graded'] and c['b']['graded'] for c in cases.values())}
    return [{'campaign_id': MANIFEST['campaign_id'], 'title': MANIFEST['title'], 'suite': 'swebench', 'split': 'verified',
             'selected': len(ids), 'arms': arms, 'paired': paired, 'cases': list(cases.values()),
             'selection_info': {**selection, 'reference_url': MANIFEST['reference_url']},
             'diagnostics': diagnostics, 'member_run_ids': members, 'ignored_sources': ignored}]
