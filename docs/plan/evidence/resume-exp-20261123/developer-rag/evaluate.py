"""Replay the committed Knowledge service against an isolated repository index."""
import argparse
import ast
import asyncio
import hashlib
import json
import math
import platform
import re
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
from agentruntime_core.providers.relational_store import Migration
from agentruntime_memory.embedding import HashingEmbedder
from agentruntime_memory.identity import Identity
from agentruntime_memory.knowledge import CatalogAuthorizer, KnowledgeService
from agentruntime_memory.retrieval import RetrievalEngine
from agentruntime_memory.retrieval import engine as engine_module
from agentruntime_worker.files.code import code_read
from agentruntime_worker.files.models import Context
from cohort import HERE, REV, read_git

ENGINE = 'services/py-memory/src/agentruntime_memory/retrieval/engine.py'
ACL = Migration(719, "developer_rag_catalog_fixture", (
    "CREATE TABLE runtime_files(file_id TEXT PRIMARY KEY,workspace_id TEXT,created_by TEXT,lifecycle TEXT)",
    "CREATE TABLE runtime_file_versions(version_id TEXT PRIMARY KEY,file_id TEXT)",
    "CREATE TABLE runtime_file_grants(file_id TEXT,workspace_id TEXT,subject_type TEXT,subject_id TEXT,permission TEXT)",
))
IDENTITY = Identity("rag_eval", "owner", "work")
PARAMS = {"collection_id":"repository", "generation":"snapshot"}
VERSION = "offline-hash-64"


def digest(b):
    return hashlib.sha256(b).hexdigest()


def old_ranker():
    # Run the original function from its frozen Git blob, independent of the candidate.
    tree = ast.parse(read_git(ENGINE))
    selected = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_prioritize_symbol')
    ns = {"re":re, "Any":object}
    exec(compile(ast.Module(body=[selected], type_ignores=[]), ENGINE, "exec"),ns)
    return ns["_prioritize_symbol"]


def corpus(cohort, root):
    docs, files = [], {}
    for path in cohort["source_paths"]:
        content = read_git(path)
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        parsed = code_read(Context(p, root, root.parent, {}, {}, "read"), lambda ctx: None)
        if not parsed or parsed["status"] != "ready":
            raise ValueError(f"Expected complete structural parse for {path}")
        if not any(b["text"].strip() for b in parsed["blocks"]):
            continue
        version = "v_" + digest(path.encode())
        docs.append({"version_id":version, "representation_id":"rep_"+digest(content), "blocks":parsed["blocks"]})
        files[version] = (path, content)
    return docs, files


def score(top, case):
    ranks = [i for i, hit in enumerate(top, 1) if hit["file"]==case["gold_file"] and
             hit["start_line"] <= case["gold_line"] <= hit["end_line"]]
    return min(ranks,default=None)


def summarize(rows, arm):
    ranks = [r[arm]["rank"] for r in rows]
    elapsed = sorted(t for r in rows for t in r[arm]["search_ms"])
    return {"hit1":ranks.count(1), "hit8":sum(x is not None for x in ranks),
            "mrr8":round(sum(1/x for x in ranks if x)/len(ranks),6),
            "p50_ms":round(statistics.median(elapsed),3),
            "p95_ms":elapsed[math.ceil(len(elapsed)*0.95)-1], "timed_searches":len(elapsed)}


async def run(split):
    manifest = (HERE/"cohort.json").read_bytes()
    protocol = json.loads((HERE/"protocol.json").read_text())
    assert digest(manifest) == protocol["cohort_sha256"]
    cohort = json.loads(manifest)
    cases = [c for c in cohort["questions"] if c["split"]==split]
    assert len({c["gold_file"] for c in cohort["questions"]}) == len(cohort["questions"])
    rankers = {"A":old_ranker(),"B":engine_module._prioritize_symbol}
    with tempfile.TemporaryDirectory(prefix="dev-rag-") as tmp:
        root = Path(tmp)
        docs, files = corpus(cohort,root/"src")
        db = SQLiteRelationalStoreProvider(root/"db")
        await db.migrate((ACL,))
        async with db.bind(IDENTITY.tenant_context()).transaction() as tx:
            for version in files:
                await tx.execute("INSERT INTO runtime_files VALUES($1,$2,$3,'active')",(version,IDENTITY.workspace_id,IDENTITY.user_id))
                await tx.execute("INSERT INTO runtime_file_versions VALUES($1,$2)",(version,version))
        engine = RetrievalEngine(vectors=InMemoryVectorStoreProvider(), embedder=HashingEmbedder(version=VERSION),
                                 relational=db, authorize=CatalogAuthorizer(db))
        service = KnowledgeService(engine)
        await engine.setup()
        staged = await service.execute(IDENTITY,"index",{**PARAMS,"documents":docs})

        async def search(arm, question, who=IDENTITY):
            with patch.object(engine_module, "_prioritize_symbol",rankers[arm]):
                start = time.perf_counter_ns()
                hits = (await service.execute(who,"search",{**PARAMS,"query":question,"embedding_version":VERSION,"limit":8}))["hits"]
                elapsed = round((time.perf_counter_ns()-start)/1e6,3)
            return hits, elapsed

        async def resolved(hits):
            evidence = (await service.execute(IDENTITY,"resolve",{**PARAMS,"node_ids":[h['id'] for h in hits]}))["evidence"]
            assert [e["id"] for e in evidence] == [h["id"] for h in hits]
            result = []
            for e in evidence:
                path, blob = files[e["version_id"]]
                anchor = e["anchor"]
                span = blob[anchor["start_byte"]:anchor["end_byte"]]
                assert span == e["text"].encode() and digest(span) == e["quote_hash"]
                result.append(dict(id=e["id"],file=path,start_line=anchor["start_line"],end_line=anchor["end_line"],
                                   quote_sha256=e["quote_hash"]))
            return result

        for arm in rankers:
            await search(arm,"startup")
        rows, controls = [], []
        for i, case in enumerate(cases):
            rec = {"id":case["id"]}
            for rep in range(3):
                for arm in ("AB" if (rep+i)%2==0 else "BA"):
                    hits, latency = await search(arm,case["question"])
                    if rep==0:
                        top = await resolved(hits)
                        rec[arm] = {"rank":score(top,case),"top8":top,"search_ms":[]}
                    else:
                        assert [r["id"] for r in rec[arm]["top8"]] == [h["id"] for h in hits]
                    rec[arm]["search_ms"].append(latency)
            rows.append(rec)
            if i % 4 == 0:
                for kind, q in [("bare",case["symbol"]),("behaviour",f"调用 `{case['symbol']}` 后，怎样校验返回结果？"),
                                ("multiple",f"找 `{case['symbol']}` 与 `other_call` 的定义")]:
                    a,_ = await search("A",q)
                    b,_ = await search("B",q)
                    controls.append({"id":case["id"],"kind":kind,"identical":a==b})
        denied = Identity("rag_eval","unshared_reader","work")
        blocked = [(await search(arm,cases[0]["question"],denied))[0] for arm in rankers]
        assert not any(blocked) and all(c["identical"] for c in controls)
        await db.aclose()
        return {"split":split,"cases":len(rows),"revision":REV,"manifest_sha256":digest(manifest),
                "measured_on":[platform.machine(),platform.system(),platform.python_version()],
                "engine_sha256":digest(Path(engine_module.__file__).read_bytes()),
                "index":{"indexed_files":len(files),"chunks":staged["chunks"],"provider":VERSION},
                "controls":controls,"unauthorized_results":{"A":len(blocked[0]),"B":len(blocked[1])},
                "metrics":{arm:summarize(rows,arm) for arm in rankers}, "rows":rows}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("split",choices=["development","validation"])
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    with args.output.open("x") as out:
        result=asyncio.run(run(args.split))
        json.dump(result,out,indent=2,ensure_ascii=False)
        out.write("\n")
    print(json.dumps({k:result[k] for k in ("split","index","metrics")},ensure_ascii=False))
