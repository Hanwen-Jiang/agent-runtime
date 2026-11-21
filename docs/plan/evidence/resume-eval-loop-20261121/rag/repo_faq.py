"""Evaluate a real-repository FAQ retrieval task. Gold = the published answer paired to a question.

Public source revision is fixed. Benchmark queries are existing FAQ headings, not customer traffic.
The question itself is never included in its retrieval document.
"""
import asyncio
import hashlib
import json
import re
import statistics
import subprocess
from pathlib import Path

from agentruntime_learning.evals.rag import LexicalRetriever

REVISION = "da58416909fc54f02519de95706df01d9bbd1701"
SEED = "resume-rag-developer-faq-20261121-v1"
HERE = Path(__file__).resolve().parent
QUESTION = re.compile(r"^\*\*Q[：:]\s*(.*?)\*\*\s*$")


def git(*args):
    return subprocess.check_output(["git", *args], text=True)


def corpus():
    examples = []
    paths = git("ls-tree", "-r", "--name-only", REVISION, "docs/note").splitlines()
    for path in paths:
        if not path.endswith(".md"):
            continue
        lines = git("show", f"{REVISION}:{path}").splitlines()
        module = next(s[2:].strip() for s in lines if s.startswith("# "))
        for i, text in enumerate(lines):
            q = QUESTION.fullmatch(text)
            if not q:
                continue
            start = i + 1
            while start < len(lines) and not lines[start].strip():
                start += 1
            if start >= len(lines) or not re.match(r"^A[：:]", lines[start]):
                raise ValueError(f"Answer missing: {path}:{i+1}")
            end = start + 1
            while end < len(lines) and lines[end].strip() and not QUESTION.fullmatch(lines[end]) and not lines[end].startswith("## "):
                end += 1
            answer = "\n".join(lines[start:end])[2:].strip()
            if not answer:
                raise ValueError(f"Empty answer: {path}:{start+1}")
            examples.append(dict(id=f"{path}#L{start+1}", doc=path, title=module, question=q[1], answer=answer))
    return examples


def source_split(examples):
    docs = sorted({x["doc"] for x in examples}, key=lambda d:hashlib.sha256((SEED+d).encode()).hexdigest())
    dev = set(docs[:len(docs)//2])
    return {k: [x for x in examples if (x['doc'] in dev) == isdev]
            for k,isdev in [('dev',True),('validation',False)]}


async def evaluate(arm, examples, query_set):
    entries = [{"id":e["id"], "text": (e["title"]+"\n" if arm == "module-title" else "")+e["answer"]} for e in examples]
    retriever = LexicalRetriever(entries)
    output = []
    for e in query_set:
        hits = await retriever.retrieve(e["question"],8)
        ids = [h["id"] for h in hits]
        output.append({"id":e["id"], "question":e["question"],"rank":(ids.index(e["id"])+1) if e["id"] in ids else None, "returned":ids[:3]})
    return output


def summarize(rows):
    return {"queries":len(rows),
            **{f"hit_at_{k}":sum(r["rank"] is not None and r["rank"] <= k for r in rows) for k in [1,3,8]},
            "mrr_at_8":statistics.mean(1/r['rank'] if r['rank'] else 0 for r in rows)}


async def main():
    ex = corpus()
    assert len({x['id'] for x in ex}) == len(ex)
    sets=source_split(ex)
    out=HERE/"run"
    protocol={"revision":REVISION,"split_seed":SEED,"unit":"source document",
              "retrieval_index":"all FAQ answer paragraphs; query headings are excluded from both indices",
              "gold":"paired answer anchor already present in source FAQ; no independent human labels, no real user logs",
              "hypothesis":"mention of the module in a question is sometimes missing from short answers",
              "candidate":"prepend the source module title once; keep production LexicalRetriever tokenizer, TF-IDF, k, ties identical",
              "accept":"validation Hit@3 and MRR@8 both improve over lexical baseline",
              "cohorts":{k:[e['id'] for e in v] for k,v in sets.items()}}
    (out/"faq-protocol.json").write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
    summaries={}
    with (out/"faq-rows.jsonl").open("w") as f:
        for split, cases in sets.items():
            rows={a:await evaluate(a,ex,cases) for a in ["baseline","module-title"]}
            summaries[split]={k:summarize(v) for k,v in rows.items()}
            for i in range(len(cases)):
                f.write(json.dumps({"set":split,"case":cases[i]["id"],"question":cases[i]["question"],
                                    **{a:{"rank":v[i]["rank"],"returned":v[i]["returned"]} for a,v in rows.items()}},ensure_ascii=False)+'\n')
    check=summaries["validation"]
    summaries["passed"]=check["module-title"]["hit_at_3"]>check["baseline"]["hit_at_3"] and check["module-title"]["mrr_at_8"]>check["baseline"]["mrr_at_8"]
    (out/"faq-results.json").write_text(json.dumps(summaries,indent=2)+'\n')
    print(json.dumps(summaries,ensure_ascii=False))


if __name__=="__main__":
    asyncio.run(main())
