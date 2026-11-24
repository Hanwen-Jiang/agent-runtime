"""Publish aggregate SWE progress for explicitly selected run directories on the evaluator."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import UTC, datetime, timedelta

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_@./+-]{0,127}')
LIMIT = 2 * 1024 * 1024
PREFIX = 'dashboard-progress-'
STATES = {'running', 'paused', 'stalled', 'awaiting_grade', 'partial'}


VERIFIED_EXPORT = '2d336d3f60d2a6496b21cf04ab73ed6cdd016eb52f19e1e4bb0328a278145d52'
PUBLIC_SWE_REPOS = frozenset({
    'astropy/astropy', 'django/django', 'matplotlib/matplotlib', 'mwaskom/seaborn',
    'pallets/flask', 'psf/requests', 'pydata/xarray', 'pylint-dev/pylint',
    'pytest-dev/pytest', 'scikit-learn/scikit-learn', 'sphinx-doc/sphinx', 'sympy/sympy',
})


def _public_ids(ids, selected):
    return (isinstance(ids, list) and len(ids) == selected
            and all(isinstance(i, str) and i.rpartition('-')[0].replace('__', '/') in PUBLIC_SWE_REPOS
                    and re.fullmatch(r'[a-z0-9-]+__[a-z0-9-]+-[0-9]{1,10}', i) for i in ids)
            and len(set(ids)) == selected)


def _time(value):
    if not isinstance(value,str) or len(value) > 64:
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('missing timezone')
    return parsed


def read_progress(doc, directory, now=None):
    """Rebuild the public row solely from validated scalars, with no inferred score."""
    rid, model = doc['run_id'], doc['model']
    if (doc.get('schema') != 'swe-progress-v1' or not NAME.fullmatch(rid) or directory != PREFIX + rid
            or not MODEL.fullmatch(model) or doc['status'] not in STATES):
        raise ValueError('invalid SWE progress identity')
    selected, submitted, collected, terminal = (doc[k] for k in ('selected', 'submitted', 'patches', 'terminal'))
    if (any(type(v) is not int for v in (selected, submitted, collected, terminal)) or not 1 <= selected <= 1000
            or not 0 <= collected <= submitted <= selected or not collected <= terminal <= selected):
        raise ValueError('invalid SWE progress counters')
    created, checked = (_time(doc[k]) for k in ('created_at','checked_at'))
    now = now or datetime.now(UTC)
    if checked < created or checked > now + timedelta(seconds=30):
        raise ValueError('invalid SWE progress timestamp')
    status = doc['status']
    if status == 'running' and now - checked > timedelta(minutes=2):
        status = 'stalled'
    selection = {'dataset': 'SWE-bench Verified', 'split': 'test', 'selected_count': selected,
                 'method': 'explicit_ids', 'source': 'run.json',
                 'dataset_sha256': VERIFIED_EXPORT, 'population_count': 500}
    if _public_ids(doc.get('case_ids'), selected):
        selection['selection_ids'] = doc['case_ids']
    row = {'run_id': rid, 'model':model, 'suite':'swebench', 'subject':'agent-runtime', 'split':'verified',
            'source':'swe_progress', 'status':status, 'started_at':doc['created_at'], 'updated_at':doc['checked_at'],
            'selected':selected, 'planned':selected, 'done':terminal, 'submitted':submitted, 'patches':collected,
            'graded_count':None, 'scores':{}, 'complete':False, 'finished':False, 'cost_usd':None, 'cost_known':False,
            'selection_info': selection}
    if 'graded_count' in doc or 'resolved' in doc:
        graded, resolved = doc['graded_count'], doc['resolved']
        if type(graded) is not int or type(resolved) is not int or not 0 <= resolved <= graded <= submitted:
            raise ValueError('invalid partial SWE grading')
        row.update(graded_count=graded)
        if graded:
            row.update(resolved=resolved, scores={'resolved_rate': resolved / selected, 'resolved': resolved})
    if status == 'partial':
        if terminal != selected or row['graded_count'] is None or row['graded_count'] >= selected:
            raise ValueError('invalid partial SWE archive')
        row.update(finished=True, graded_complete=False)
    return row


def _safe_json(fd, name, *, expected_sha256=None):
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file_fd, 'rb') as stream:
        meta = os.fstat(file_fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > LIMIT:
            raise ValueError('invalid SWE archive file')
        raw = stream.read(LIMIT+1)
        if len(raw) > LIMIT:
            raise ValueError('oversized SWE archive file')
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        return {}
    return json.loads(raw)


def _maybe(fd, name):
    try:
        return _safe_json(fd, name)
    except FileNotFoundError:
        return {}


def _stream_grade(stream_fd, iid, rid, model, patch):
    # Only count an independently scored, final patch for this exact case.
    try:
        case_fd = os.open(iid, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stream_fd)
    except FileNotFoundError:
        return None
    try:
        receipt = _maybe(case_fd, 'receipt.json')
        if (receipt.get('instance_id') != iid or receipt.get('run_id') != rid or receipt.get('model') != model
                or receipt.get('patch_sha256') != hashlib.sha256(patch.encode()).hexdigest()):
            return None
        digest = receipt.get('official_report_sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            return None
        try:
            grade = _safe_json(case_fd, f'{model}.{rid}.json', expected_sha256=digest)
        except FileNotFoundError:
            return None
        if grade.get('submitted_ids') != [iid] or grade.get('completed_ids') != [iid]:
            return None
        if grade.get('resolved_ids') == [iid] and grade.get('unresolved_ids') == []:
            return 'resolved'
        if any(iid in (grade.get(k) or []) for k in ('infra_failure_ids','ambiguous_failure_ids','error_ids')):
            return None
        if grade.get('unresolved_ids') == [iid] and grade.get('resolved_ids') == []:
            return 'unresolved'
        return None
    finally:
        os.close(case_fd)


def _active(fd):
    # The evaluator holds an exclusive flock for the lifetime of the runner.
    import fcntl
    try:
        lock = os.open('.runner.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError('invalid runner lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(lock)


def _atomic(directory, name, data):
    with tempfile.NamedTemporaryFile('w', dir=directory, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, separators=(',',':'))
            f.flush()
            tmp.chmod(0o644)
            tmp.replace(directory / name)
        finally:
            tmp.unlink(missing_ok=True)


def recovery_identity(run, protocol):
    """Expose the parent, arm and code tree only when the recovery and run manifests agree."""
    if not isinstance(protocol, dict):
        return {}
    mode, arm, tree = (protocol.get(k) for k in ("mode", "arm", "runtime_tree"))
    parent = run.get("selection", {}).get("source_mini_run")
    if (arm not in ("a", "b") or mode not in ("missing_verdict_recovery", "second_attempt_quota_recovery")
            or not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", tree)
            or not isinstance(parent, str) or not NAME.fullmatch(parent)
            or protocol.get("ids") != run.get("instance_ids")
            or protocol.get("profile") != "a6api/gpt-6.2-sol@2026-11" or protocol.get("tier") != "strong@medium"):
        return {}
    return {"runtime_tree": tree, "recovery_source": {"parent_run_id": parent, "arm": arm, "mode": mode}}


def publish(root, rid):
    if not NAME.fullmatch(rid) or len(PREFIX + rid) > 96:
        raise ValueError('invalid run name')
    flags = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    try:
        fd = os.open(rid, flags, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        run = _safe_json(fd,'run.json')
        ids = run['instance_ids']
        if (run.get('benchmark') != 'swe-bench' or rid != run['run_id'] or not isinstance(ids,list)
                or not 1 <= len(ids) <= 1000 or not all(isinstance(i,str) and NAME.fullmatch(i) for i in ids)
                or len(set(ids)) != len(ids)
                or run.get('selection',{}).get('instances_sha256') != VERIFIED_EXPORT):
            raise ValueError('unexpected SWE manifest')
        terminal = submitted = patches = 0
        task_ids = set()
        case_jobs = {}
        case_states = {}
        stream_grades = []
        model = run['settings']['model_name']
        stream_fd = None
        records_fd = os.open('records', flags, dir_fd=fd)
        try:
            if isinstance(model, str) and NAME.fullmatch(model):
                try:
                    stream_fd = os.open('stream', flags, dir_fd=fd)
                except FileNotFoundError:
                    pass
            for i in ids:
                r = _maybe(records_fd, i + '.json')
                if not r: continue
                if r.get('instance_id') != i: raise ValueError('checkpoint identity mismatch')
                tid = r.get('task_id')
                if tid:
                    if not isinstance(tid,str) or not NAME.fullmatch(tid) or tid in task_ids:
                        raise ValueError('invalid or duplicate task identity')
                    task_ids.add(tid)
                    case_jobs[i] = tid
                    submitted += 1
                case_states[i] = r.get('state')
                terminal += r.get('state') in ('finished','error','not_run')
                patches += bool(tid and r.get('state') == 'finished' and r.get('patch'))
                if stream_fd is not None and tid and r.get('state') == 'finished' and isinstance(r.get('patch'), str) and r['patch']:
                    grade = _stream_grade(stream_fd, i, rid, model, r['patch'])
                    if grade:
                        stream_grades.append(grade)
        finally:
            os.close(records_fd)
            if stream_fd is not None:
                os.close(stream_fd)
        ps = _maybe(fd, 'provider-quota-pause.json')
        guard = _maybe(fd, 'quota-stop.json')
        paused = (bool(ps) and ps.get('reason') == 'insufficient_user_quota' and ps.get('run_id') == rid
                  and ps.get('instance_id') in ids and ps.get('task_id') == case_jobs.get(ps.get('instance_id')))
        paused |= (bool(guard) and guard.get('code') == 'insufficient_user_quota'
                   and type(guard.get('consecutive_refusals')) is int and guard['consecutive_refusals'] >= 3
                   and isinstance(guard.get('active_in_arm'),list) and bool(task_ids.intersection(guard['active_in_arm'])))
        state = 'paused' if paused else 'awaiting_grade' if terminal == len(ids) else 'running' if _active(fd) else 'stalled'
        now = datetime.now(UTC)
        doc = {'schema':'swe-progress-v1','run_id':rid,'model':run['settings']['model_name'],'selected':len(ids),
               'created_at':run['created_at'],'checked_at':now.isoformat(),'status':state,
               'terminal':terminal,'submitted':submitted,'patches':patches}
        if _public_ids(ids, len(ids)) and run['selection'].get('dataset_split', 'test') == 'test':
            doc['case_ids'] = ids
        if stream_grades:
            doc.update(graded_count=len(stream_grades), resolved=stream_grades.count('resolved'))
        report = _maybe(fd, 'report.json')
        if report.get('graded') is True and report.get('run_id') == rid and report.get('model_name') == doc['model']:
            rows = report.get('instances') or []
            if (len(rows) == len(ids) and {r.get('instance_id') for r in rows} == set(ids)
                    and report.get('selected') == len(ids)):
                graded = [r for r in rows if r.get('grading') in ('resolved', 'unresolved')]
                resolved = sum(r['grading'] == 'resolved' for r in graded)
                if (resolved == report.get('resolved') and all(r['instance_id'] in case_jobs for r in graded)):
                    doc.update(graded_count=len(graded), resolved=resolved)
        archived = (report.get('graded') is True and terminal == len(ids) and report.get('not_run') == 0
                    and report.get('ran', 0) + report.get('errors', 0) == len(ids) and not paused
                    and isinstance(report.get('instances'), list) and len(report['instances']) == len(ids)
                    and {r['instance_id'] for r in report['instances']} == set(ids)
                    and all(case_states.get(r['instance_id']) == r.get('state') for r in report['instances']))
        if archived and 'graded_count' in doc and doc['graded_count'] < len(ids):
            doc['status'] = 'partial'
        read_progress(doc, PREFIX+rid, now)
        dest = root / (PREFIX+rid)
        dest.mkdir(mode=0o755,exist_ok=True)
        info = dest.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('unsafe projection directory')
        _atomic(dest,'public-progress.json',doc)
        # Publish only the fields that construct a validated official aggregate once grading is final.
        if not archived:
            return False
        if report.get('run_id') != rid or report.get('model_name') != doc['model']:
            raise ValueError('final report identity mismatch')
        final_run = {k:run[k] for k in ('benchmark','run_id','instance_ids','created_at')}
        final_run['selection'] = {'instances_file':'verified.jsonl','instances_sha256':run['selection']['instances_sha256'],'ids':True}
        # Replaying explicit IDs can retain the original repository exclusions.
        # Only public repository names may pass into the downloadable manifest.
        excluded = run['selection'].get('exclude_repos')
        if isinstance(excluded, list) and all(isinstance(repo, str) and repo in PUBLIC_SWE_REPOS for repo in excluded):
            final_run['selection']['exclude_repos'] = excluded
        final_run['settings'] = {k:run['settings'][k] for k in ('environment','hints','model_name')}
        summary = {k:report[k] for k in ('run_id','benchmark','selected','resolved','resolve_rate','graded','graded_complete',
                                         'ran','not_run','errors','model_name','cost_usd') if k in report}
        summary['instances'] = [{k:r[k] for k in ('instance_id','state','status','grading') if k in r} for r in report['instances']]
        if ({r['instance_id'] for r in summary['instances']} != set(ids) or len(summary['instances']) != len(ids)
                or any(not all(v is None or isinstance(v,str) and NAME.fullmatch(v) for v in row.values()) for row in summary['instances'])):
            raise ValueError('invalid final case summaries')
        summary['graded_complete'] = doc.get('graded_count') == len(ids)
        if run['selection'].get('source_mini_run'):
            protocol = _maybe(fd, 'recovery-protocol.json')
            identity = recovery_identity(run, protocol)
            if identity:
                final_run['selection']['source_mini_run'] = identity['recovery_source']['parent_run_id']
                final_run['recovery_protocol'] = {k:protocol[k] for k in ('mode','arm','runtime_tree','ids','profile','tier')}
        _atomic(dest,'run.json',final_run)
        _atomic(dest,'report.json',summary)
        return True
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    parser.add_argument('runs',nargs='+')
    args = parser.parse_args()
    complete = [publish(args.root,rid) for rid in args.runs]
    raise SystemExit(0 if all(complete) else 75)
