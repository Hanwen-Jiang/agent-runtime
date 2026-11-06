"""Read-only audit of original benchmark artifacts before any paid continuation."""
from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from agentruntime_learning.benchmarks import provenance as pv
from agentruntime_learning.benchmarks import swebench as sb
from agentruntime_learning.benchmarks import terminalbench as tb


def audit(runs: list[Path], *, charged_runs: list[Path] | None = None, cap_usd: float = 80.0) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    versions = []
    for root in runs:
        manifest = json.loads((root / 'run.json').read_text())
        settings = manifest.get('settings') or {}
        recs = sb.read_records(root)
        versions.append({'path': str(root), 'run_id': manifest['run_id'], 'settings': settings,
                         'dataset_sha256': manifest.get('dataset_sha256')})
        for iid in manifest['instance_ids']:
            rec = recs.get(iid, {})
            gpath = root / 'grading' / f'{iid}.json'
            grading = json.loads(gpath.read_text()) if gpath.exists() else {}
            rows.append({'instance_id': iid, 'run': str(root), 'task_id': rec.get('task_id'), 'state': rec.get('state', 'not_run'),
                         'cost_usd': rec.get('cost_usd'), 'environment_used': rec.get('environment_used'),
                         'budget_exhausted': tb.budget_exhausted(rec),
                         'execution': 'finished' if rec.get('state') == 'finished' else 'executed_error' if rec.get('task_id') else 'not_run',
                         'outcome': tb.classify(rec, grading, official=settings.get('environment', 'official') == 'official')})
    duplicates = sorted(k for k, n in Counter(r['instance_id'] for r in rows).items() if n > 1)
    history = pv.historical_cost(charged_runs if charged_runs is not None else runs)
    frozen = all(v['settings'].get('provenance') and v['dataset_sha256'] for v in versions)
    comparable = frozen and not duplicates
    if comparable:
        first = versions[0]['settings']
        comparable = all(v['settings'] == first for v in versions)
    finished = [r for r in rows if r['execution'] == 'finished']
    # This estimate includes failed/executed attempts, not just successful records.
    observed_cost = sum(float(r['cost_usd'] or 0) for r in rows)
    rate = observed_cost / sum(bool(r['task_id']) for r in rows) if any(r['task_id'] for r in rows) else None
    return {'runs': versions, 'selected': len(rows), 'duplicate_instance_ids': duplicates,
            'execution_counts': dict(Counter(r['execution'] for r in rows)),
            'outcome_counts': dict(Counter(r['outcome'] for r in rows)), 'finished_with_official_environment': sum(r['environment_used'] is True for r in finished),
            'budget_exhausted_ids': [r['instance_id'] for r in rows if r['budget_exhausted']],
            'comparable_frozen_baseline': bool(comparable), 'historical_cost': history, 'approved_cap_usd': cap_usd,
            'remaining_usd': max(0, round(cap_usd - history['known_usd'], 6)) if not history['unknown_task_ids'] else None,
            'observed_usd_per_started_task': rate, 'estimated_new_40_usd': rate * 40 if rate is not None else None,
            'next_action': 'reuse completed records only with matching live provenance' if comparable else 'retain old artifacts; freeze current configuration and run all selected 40',
            'records': rows}


def prepare_terminalbench(runs: list[Path], source_policy: dict[str, Any], out: Path, *, tenant: str = 'evaltb20261106') -> dict[str, Any]:
    """Prepare a concrete isolated-tenant policy and all 40 ids; never publish."""
    if not sb._RUN_ID.fullmatch(tenant) or tenant in ('swebench', 'evals', 'default'):
        raise ValueError('use a distinct evaluation tenant')
    manifests = [json.loads((root / 'run.json').read_text()) for root in runs]
    ids = [iid for manifest in manifests for iid in manifest['instance_ids']]
    if len(ids) != 40 or len(set(ids)) != 40:
        raise ValueError('the fixed baseline must contain exactly 40 distinct original cases')
    policy = copy.deepcopy(source_policy)
    policy.setdefault('budget', {})['usd'] = 1.0
    policy['model_budget'] = {'daily_usd': 40.0, 'monthly_usd': 40.0}
    hosts = ['github.com', 'codeload.github.com', 'objects.githubusercontent.com', 'raw.githubusercontent.com', 'download.pytorch.org']
    execution = {'run_id': 'goal-tb40-20261106', 'tenant_id': tenant, 'count': 40, 'seed': 0, 'concurrency': 1,
                 'budget_usd': 40.0, 'task_usd_limit': 1.0, 'task_timeout_min': 60, 'network_hosts': hosts, 'network_minutes': 60,
                 'policy_sha256': pv.digest(policy), 'source_policy_sha256': pv.digest(source_policy),
                 'policy_source_observed': False, 'require_observed_live_provenance': True,
                 'budget_semantics': {'gateway_daily_and_monthly': 'hard total admission/reservation cap',
                                      'task_workflow_and_client': 'soft per-task target; a model call or parallel work can overshoot'},
                 'stop_on_total_budget': True, 'legacy_per_task_usd': 3.0, 'merge_legacy_results': False,
                 'price_estimate_required': 'recompute from currently loaded immutable Registry prices before execution',
                 'instance_ids': ids}
    out.mkdir(parents=True, exist_ok=True)
    sb._write_text(out / 'tenant-policy-seed.json', json.dumps({'entries': [{'kind': 'tenant_policy', 'tenant': tenant, 'name': '', 'document': policy}]}, indent=2) + '\n')
    sb._write_text(out / 'execution.json', json.dumps(execution, indent=2) + '\n')
    sb._write_text(out / 'cases.ids', '\n'.join(ids) + '\n')
    return execution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--charged-run', type=Path, action='append')
    parser.add_argument('--cap-usd', type=float, default=80)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--source-policy', type=Path, help='also prepare policy/40-case execution files from this tenant-policy document')
    parser.add_argument('--execution-out', type=Path)
    args = parser.parse_args()
    result = audit(args.run, charged_runs=args.charged_run, cap_usd=args.cap_usd)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sb._write_text(args.out, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if args.source_policy:
        if not args.execution_out:
            parser.error('--source-policy requires --execution-out')
        source = json.loads(args.source_policy.read_text())
        if 'entries' in source:
            [entry] = source['entries']
            source = entry['document']
        prepare_terminalbench(args.run, source, args.execution_out)
    print(json.dumps({k: v for k, v in result.items() if k not in ('runs', 'records')}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
