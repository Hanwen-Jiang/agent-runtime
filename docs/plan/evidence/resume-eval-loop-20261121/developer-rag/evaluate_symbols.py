"""Frozen round 2: coarse source-file recall followed by definition-aware BM25."""

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import math
import re

from evaluate import COHORT_SHA, HERE, LexicalRetriever, Path, corpus


NEW_SHA = "f46ec22f1d2efbd4470d3a5107cc2a27d599dd6fed6917eee159e1b6c850f968"
DEFINITION = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|func|function|fn|struct|impl|interface|type)\s")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[一-鿿]+|\d+")


def tokens(text):
    result = []
    for word in TOKEN.findall(text):
        if "一" <= word[0] <= "鿿":
            result.extend(word)
            result.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            result.append(word.lower())
            parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word).replace("_", " ").lower().split()
            if len(parts) > 1:
                result.extend(parts)
    return result


def passages(chunks):
    files = defaultdict(dict)
    for c in chunks:
        code = c["text"].split("\n", 1)[1].split("\n")
        for n, line in enumerate(code, c["start"]):
            files[c["file"]][n] = line
    grouped = defaultdict(list)
    df = Counter()
    length_sum = count = 0
    for path, source in files.items():
        lines = [""] + [source[i] for i in range(1, max(source)+1)]
        starts = sorted({1, *(i for i in range(1, len(lines)) if DEFINITION.match(lines[i]))})
        for index, origin in enumerate(starts):
            end = starts[index+1]-1 if index+1 < len(starts) else len(lines)-1
            for offset in range(origin, end+1, 56):
                start = max(1, offset - 4)
                stop = min(end, start + 63)
                region = "\n".join(lines[start:stop+1])
                counts = Counter(tokens(path + "\n" + lines[origin] + "\n" + region))
                length = sum(counts.values())
                df.update(counts.keys()); length_sum += length; count += 1
                grouped[path].append((start, stop, counts, length))
    idf = {t: math.log1p((count-freq+0.5)/(freq+0.5)) for t,freq in df.items()}
    return grouped, idf, length_sum/count


def rerank(question, paths, grouped, idf, length_avg):
    qt = set(tokens(question))
    scored = []
    for place, path in enumerate(paths, 1):
        spans = []
        for lo, hi, counts, length in grouped[path]:
            k = 1.2 * (0.25+0.75*length/length_avg)
            score = sum(idf.get(t, 0)*counts[t]*2.2/(counts[t]+k) for t in qt if t in counts)
            spans.append((score,lo,hi))
        # Limit each file to two passages so one implementation cannot crowd out all other evidence.
        for score,lo,hi in sorted(spans, key=lambda s:(-s[0],s[1],s[2]))[:2]:
            scored.append((score,place,path,lo,hi))
    return [dict(file=path,start=lo,end=hi) for _,_,path,lo,hi in sorted(scored,key=lambda s:(-s[0],s[1],s[2],s[3]))[:8]]


def grade(question, candidates):
    key=question["repository"]+"/"+question["gold_file"]
    match=[(rank,c) for rank,c in enumerate(candidates,1) if c["file"]==key]
    ranks=[rank for rank,c in match if c["start"]<=question["evidence_line"]<=c["end"]]
    return dict(id=question["id"],file_hit8=int(bool(match)),evidence_hit8=int(bool(ranks)),evidence_rank=min(ranks,default=None),
                top8=[f'{c["file"]}:{c["start"]}-{c["end"]}' for c in candidates])


async def run(repos, split, pool_sizes):
    old=(HERE/"cohort.json").read_bytes();new=(HERE/"cohort-round2.json").read_bytes()
    assert hashlib.sha256(old).hexdigest()==COHORT_SHA and hashlib.sha256(new).hexdigest()==NEW_SHA
    source=json.loads(new if split=="fresh" else old)
    questions=[q for q in source["questions"] if split=="fresh" or q["split"]==split]
    _,all_chunks=corpus(repos,source["repository_revisions"])
    chunks=[c for c in all_chunks if not c["file"].endswith(".md")]
    retriever=LexicalRetriever(chunks); lookup={c["id"]:c for c in chunks}
    grouped,idf,length_avg=passages(chunks)
    out=defaultdict(list)
    for q in questions:
        # Only the query is used until retrieving both arms is complete.
        hits=await retriever.retrieve(q["question"],len(chunks))
        ranking=[lookup[h["id"]] for h in hits]
        shortlist=list(dict.fromkeys(c["file"] for c in ranking))
        variants={f"symbols_top{n}":rerank(q["question"],shortlist[:n],grouped,idf,length_avg) for n in pool_sizes}
        for name,candidates in {"round1_source_scope":ranking[:8],**variants}.items():
            out[name].append(grade(q,candidates))
    return dict(split=split,cohort_sha256=NEW_SHA if split=="fresh" else COHORT_SHA,
                source_files=len(grouped),source_chunks=len(chunks),symbol_spans=sum(map(len,grouped.values())),
                cases=len(questions),variants={k:dict(file_hits=sum(r["file_hit8"] for r in v),
                evidence_hits=sum(r["evidence_hit8"] for r in v),rows=v) for k,v in out.items()})


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--repos",type=Path,required=True); ap.add_argument("--split",choices=["development","validation","fresh"],required=True)
    ap.add_argument("--file-pool",type=int,action="append",choices=[6,16],required=True);ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    if args.out.exists() or (args.split!="development" and len(args.file_pool)!=1):raise SystemExit("use a fresh path and a single frozen candidate for verification")
    r=asyncio.run(run(args.repos,args.split,args.file_pool))
    args.out.write_text(json.dumps(r,ensure_ascii=False,indent=2)+"\n")
    print(args.split,r["source_files"],r["symbol_spans"],{k:(v["file_hits"],v["evidence_hits"])for k,v in r["variants"].items()})
