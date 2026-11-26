"""Replay frozen B to locate first-stage misses without fitting on validation."""
import asyncio
from collections import Counter
import json
from unittest.mock import patch

from evaluate import HERE, digest, engine_module, run

cohort = json.loads((HERE / "cohort.json").read_bytes())
questions = {q['question']:q for q in cohort['questions'] if q['split']=='validation'}
initial = {row['id']:row for row in json.loads((HERE / "validation.json").read_bytes())['rows']}
observed = {}
candidate = engine_module._prioritize_symbol


def trace(hits, query):
    if query not in questions:
        return candidate(hits,query)
    q = questions[query]
    version = 'v_'+digest(q['gold_file'].encode())
    def covers(hit):
        anchor=hit['anchor']
        return hit['version_id']==version and anchor['start_line'] <= q['gold_line'] <= anchor['end_line']
    pool_ranks=[i for i,h in enumerate(hits,1) if covers(h)]
    result=candidate(hits,query)
    top_ranks=[i for i,h in enumerate(result[:8],1) if covers(h)]
    assert min(top_ranks,default=None)==initial[q['id']]['B']['rank']
    row={'pool_size':len(hits),'pool_evidence_rank':min(pool_ranks,default=None),
         'source_file_in_pool':any(h['version_id']==version for h in hits),'B_evidence_rank':min(top_ranks,default=None)}
    assert q['id'] not in observed or observed[q['id']]==row
    observed[q['id']]=row
    return result


if __name__ == "__main__":
    with patch.object(engine_module,"_prioritize_symbol",trace):
        asyncio.run(run("validation"))
    assert len(observed)==len(questions)
    counts=Counter()
    for row in observed.values():
        counts['gold_file_recalled']+=row['source_file_in_pool']
        counts['gold_line_recalled']+=row['pool_evidence_rank'] is not None
        counts['gold_line_in_top8']+=row['B_evidence_rank'] is not None
    report={"method":"post-hoc replay of the identical frozen B rankings; observed candidates before priority",
            "counts":dict(counts),"cases":observed}
    with (HERE/"diagnostics.json").open("x") as out:
        json.dump(report,out,ensure_ascii=False,indent=2);out.write("\n")
    print(counts)
