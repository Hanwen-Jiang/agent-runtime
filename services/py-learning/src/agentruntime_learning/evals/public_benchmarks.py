"""Read-only benchmark history from real run manifests and final summaries."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 2 * 1024 * 1024
MAX_DIRECTORIES = 128
MAX_AUDIT_BYTES = 256 * 1024
MAX_AUDIT_RUNS = 256
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any) -> float | None:
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    return float(value)


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def project_benchmark(run: dict[str, Any], report: dict[str, Any], digest: str) -> dict[str, Any] | None:
    if run.get('benchmark') != 'swe-bench' or report.get('benchmark') != 'swe-bench':
        return None
    run_id = run.get('run_id')
    if not isinstance(run_id, str) or not NAME.fullmatch(run_id) or report.get('run_id') != run_id:
        raise ValueError('benchmark identity mismatch')
    ids = run.get('instance_ids')
    if not isinstance(ids, list) or not 0 < len(ids) <= 10000 or any(not isinstance(i, str) or not i or len(i)>256 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid benchmark cohort')
    selected = _count(report.get('selected'))
    resolved = _count(report.get('resolved'))
    if selected != len(ids):
        raise ValueError('selected count differs from cohort')
    # A collection can have finished with execution errors. These remain in the
    # selected denominator; graded_complete is kept separately and never forged.
    rows = report.get('instances')
    if not isinstance(rows, list) or len(rows)>10000:
        raise ValueError('missing benchmark outcome coverage')
    by_id = {r.get('instance_id'):r for r in rows if isinstance(r,dict) and isinstance(r.get('instance_id'),str)}
    if len(by_id) != len(rows) or set(by_id)-set(ids):
        raise ValueError('invalid benchmark outcome identities')
    terminal = sum(by_id.get(i,{}).get('state') in ('finished','error') for i in ids)
    not_run, errors, ran = (_count(report.get(k)) for k in ('not_run','errors','ran'))
    complete = terminal == selected and not_run == 0 and ran is not None and errors is not None and ran + errors == selected
    graded = report.get('graded') is True
    if not graded or resolved is None:
        return None
    if resolved > selected or sum(r.get('grading')=='resolved' for r in by_id.values()) != resolved:
        raise ValueError('resolved count differs from actual grading')
    rate = _number(report.get('resolve_rate'))
    if rate is None or abs(rate-resolved/selected)>1e-6:
        raise ValueError('benchmark rate denominator mismatch')
    at = run.get('created_at')
    if not isinstance(at,str):
        raise ValueError('benchmark has no recorded timestamp')
    stamp = datetime.fromisoformat(at.replace('Z','+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('benchmark timestamp has no timezone')
    settings = run.get('settings') or {}
    if not isinstance(settings,dict):
        raise ValueError('invalid benchmark settings')
    model = settings.get('model_name')
    if not isinstance(model,str) or not model or len(model)>256 or report.get('model_name') != model:
        raise ValueError('benchmark model mismatch')
    selection = run.get('selection') or {}
    if not isinstance(selection,dict):
        raise ValueError('invalid benchmark selection')
    # Only recognized dataset labels are public; filesystem paths never leave the reader.
    dataset = Path(str(selection.get('instances_file') or '')).stem.lower()
    split = selection.get('split')
    if not isinstance(split,str) or not NAME.fullmatch(split):
        split = dataset if dataset in ('verified','lite','test','train','dev') else 'unknown'
    if len(split)>64:
        raise ValueError('invalid benchmark split')
    environment = settings.get('environment')
    hints = settings.get('hints')
    if environment not in ('official','runtime','default') or type(hints) is not bool:
        raise ValueError('benchmark protocol unspecified')
    protocol = ('official-workspace-patch-v1' if environment=='official' else 'runtime-workspace-patch-v1') + (':hints' if hints else ':no-hints')
    dataset_hash = selection.get('instances_sha256')
    if not isinstance(dataset_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',dataset_hash):
        dataset_hash = None
    protocol_hash = _digest({'protocol':protocol,'dataset_sha256':dataset_hash})
    cost = _number(report.get('cost_usd'))
    return {'run_id':run_id,'suite':'swebench','split':split,'model':model,'subject':'agent-runtime' if model.startswith('agent-runtime') else model,
            'created_at':at,'started_at':at,'selected':selected,'sample_count':selected,'resolved':resolved,'rate':rate,'complete':complete,
            'finished':complete,'status':'finished' if complete else 'incomplete','graded_complete':report.get('graded_complete') is True,
            'graded_count':sum(r.get('state')=='finished' for r in by_id.values()),'errors':errors,'not_run':not_run,
            'cohort_sha256':_digest(sorted(ids)),'protocol':protocol,'protocol_sha256':protocol_hash,'dataset_sha256':dataset_hash,
            'source_sha256':digest,'source':'benchmark_artifacts','cost_usd':cost if cost is not None and cost>=0 else None,
            'scores':{'resolved':resolved,'resolved_rate':rate}}


class PublicBenchmarkSource:
    def __init__(self, root: Path | None, *, ttl_s: float=5, audit_root: Path | None=None) -> None:
        self.root=Path(root) if root else None
        self.audit_root=Path(audit_root) if audit_root else None
        self._audit_good:list[dict[str,Any]]|None=None
        self.ttl_s=ttl_s
        self._lock=threading.Lock()
        self._at=float('-inf')
        self._cached:bytes|None=None
        self._good:dict[str,dict[str,Any]]={}

    def _read(self, directory: str, name: str) -> bytes:
        assert self.root is not None
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            child=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
            handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
            with os.fdopen(handle,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES:
                    raise ValueError('invalid benchmark summary file')
                raw=stream.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:
                    raise ValueError('oversized benchmark summary')
                return raw
        finally:
            os.close(fd)

    def _names(self) -> list[str]:
        assert self.root is not None
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            names=[]
            with os.scandir(fd) as entries:
                for entry in entries:
                    if NAME.fullmatch(entry.name) and not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                        if len(names)>MAX_DIRECTORIES:
                            raise ValueError('too many benchmark directories')
            return sorted(names)
        finally:
            os.close(fd)

    def _audits(self) -> list[dict[str,Any]]:
        assert self.audit_root is not None
        fd=os.open(self.audit_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            handle=os.open('public-run-audits.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
            with os.fdopen(handle,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_AUDIT_BYTES:
                    raise ValueError('invalid run audit file')
                raw=stream.read(MAX_AUDIT_BYTES+1)
                if len(raw)>MAX_AUDIT_BYTES:raise ValueError('oversized run audit')
        finally:
            os.close(fd)
        doc=json.loads(raw)
        if not isinstance(doc,dict) or type(doc.get('version')) is not int or doc['version']!=1 or not isinstance(doc.get('runs'),list) or len(doc['runs'])>MAX_AUDIT_RUNS:
            raise ValueError('invalid run audit schema')
        rows=[];seen=set()
        for row in doc['runs']:
            if not isinstance(row,dict):raise ValueError('invalid run audit row')
            suite,run_id,reason=(row.get(k) for k in ('suite','run_id','invalid_reason'))
            if not all(isinstance(value,str) and NAME.fullmatch(value) for value in (suite,run_id,reason)) or row.get('execution_valid') is not False:
                raise ValueError('invalid run audit identity')
            source=row.get('source_sha256')
            if not isinstance(source,str) or not re.fullmatch(r'[a-f0-9]{64}',source):raise ValueError('invalid run audit provenance')
            if (suite,run_id) in seen:raise ValueError('duplicate run audit')
            seen.add((suite,run_id))
            projected={'suite':suite,'run_id':run_id,'execution_valid':False,'invalid_reason':reason,'source_sha256':source}
            for field in ('reason_zh','reason_en'):
                value=row.get(field)
                if value is not None:
                    if not isinstance(value,str) or len(value)>500:raise ValueError('invalid run audit explanation')
                    projected[field]=value
            rows.append(projected)
        return rows

    def get(self) -> bytes:
        with self._lock:
            now=time.monotonic()
            if self._cached is not None and now-self._at<self.ttl_s:
                return self._cached
            errors=set();names=[]
            if self.audit_root is not None:
                try:self._audit_good=self._audits()
                except FileNotFoundError:
                    if self._audit_good is not None:errors.add('run_audit_missing')
                except (OSError,ValueError,TypeError,KeyError,OverflowError):errors.add('run_audit_invalid')
            exclusions=self._audit_good or []
            if self.root is not None:
                try:
                    names=self._names()
                except FileNotFoundError:
                    pass
                except (OSError,ValueError):
                    errors.add('benchmark_artifacts_unavailable')
            for name in names:
                try:
                    a,b=self._read(name,'run.json'),self._read(name,'report.json')
                    run,report=json.loads(a),json.loads(b)
                    if not isinstance(run,dict) or not isinstance(report,dict):
                        raise ValueError('invalid benchmark documents')
                    projected=project_benchmark(run,report,hashlib.sha256(a+b'\0'+b).hexdigest())
                    if projected is not None:
                        self._good[name]=projected
                except FileNotFoundError:
                    if name in self._good:errors.add('benchmark_summary_missing')
                except (OSError,ValueError,TypeError,KeyError,OverflowError):
                    errors.add('benchmark_summary_invalid')
            if set(self._good)-set(names):errors.add('benchmark_summary_missing')
            # Prefer the complete copy if a resumed directory shares an ID.
            unique={}
            for row in self._good.values():
                key=row['run_id'];prior=unique.get(key)
                if prior is None or (row['complete'],row['created_at'])>(prior['complete'],prior['created_at']):unique[key]=row
            runs=sorted(unique.values(),key=lambda r:datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).timestamp())
            audit_by_id={(row['suite'],row['run_id']):row for row in exclusions}
            runs=[{**row,**{key:value for key,value in audit_by_id.get((row['suite'],row['run_id']),{}).items() if key!='source_sha256'},
                   **({'audit_source_sha256':audit_by_id[(row['suite'],row['run_id'])]['source_sha256']} if (row['suite'],row['run_id']) in audit_by_id else {})} for row in runs]
            present={(row['suite'],row['run_id']) for row in runs}
            # A receipt can veto an older live-telemetry identity even if that
            # run is not in the benchmark archive root. It carries no score.
            runs += [{**row,'source':'run_audit'} for row in exclusions if (row['suite'],row['run_id']) not in present]
            value={'source':'artifacts' if runs or exclusions else 'none','status':'stale' if (runs or exclusions) and errors else 'ready' if runs or exclusions else 'unavailable' if errors else 'empty',
                   'stale':bool(errors),'checked_at':datetime.now(UTC).isoformat(),'runs':runs,'exclusions':exclusions,'errors':sorted(errors)}
            self._cached=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode();self._at=now
            return self._cached
