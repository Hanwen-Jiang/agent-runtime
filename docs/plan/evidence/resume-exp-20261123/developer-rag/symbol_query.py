"""Compare literal definition lookup with existing full-question recall on new files."""
import argparse
import asyncio
import itertools
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from evaluate import (ACL, HERE, IDENTITY, PARAMS, VERSION, CatalogAuthorizer, HashingEmbedder,
                      Identity, InMemoryVectorStoreProvider, KnowledgeService, RetrievalEngine,
                      SQLiteRelationalStoreProvider, corpus, digest, engine_module, old_ranker, score, summarize)


async def evaluate(split):
    base = json.loads((HERE/"cohort.json").read_bytes())
    reserve = json.loads((HERE/"reserve.json").read_bytes())
    old = {q["gold_file"] for q in base["questions"]}
    assert not old & {q["gold_file"] for q in reserve["questions"]}
    cases = [q for q in base["questions"] if q["split"]=="development"] if split=="development" else reserve["questions"]
    ranking = {"A":old_ranker(), "B":engine_module._prioritize_symbol, "C":engine_module._prioritize_symbol}
    arrangements = list(itertools.permutations("ABC"))
    rows, checks = [], []
    with tempfile.TemporaryDirectory(prefix="code-lookup-") as tmp:
        root = Path(tmp)
        docs, files = corpus(base, root/"src")
        db = SQLiteRelationalStoreProvider(root/"db")
        await db.migrate((ACL,))
        async with db.bind(IDENTITY.tenant_context()).transaction() as tx:
            for v in files:
                await tx.execute("INSERT INTO runtime_files VALUES($1,$2,$3,'active')",(v,IDENTITY.workspace_id,IDENTITY.user_id))
                await tx.execute("INSERT INTO runtime_file_versions VALUES($1,$2)",(v,v))
        service = KnowledgeService(RetrievalEngine(vectors=InMemoryVectorStoreProvider(),
            embedder=HashingEmbedder(version=VERSION),relational=db,authorize=CatalogAuthorizer(db)))
        await service.engine.setup()
        staged = await service.execute(IDENTITY,"index",{**PARAMS,"documents":docs})

        async def fetch(arm,query,user=IDENTITY):
            started = time.perf_counter_ns()
            # Only an explicit definition request in this all-code collection opts into C.
            term = engine_module._definition_symbol(query) if arm=="C" else None
            with patch.object(engine_module,"_prioritize_symbol",ranking[arm]):
                hits = (await service.execute(user,"search",{**PARAMS,"query":term or query,"embedding_version":VERSION,"limit":8}))["hits"]
            return hits, round((time.perf_counter_ns()-started)/1e6,3)

        for arm in ranking:
            await fetch(arm,"startup")
        for idx, case in enumerate(cases):
            row = {"id":case["id"]}
            for rep in range(3):
                for arm in arrangements[(idx+2*rep)%len(arrangements)]:
                    hits, elapsed = await fetch(arm,case["question"])
                    ids = [h["id"] for h in hits]
                    if rep==0:
                        evidence = (await service.execute(IDENTITY,"resolve",{**PARAMS,"node_ids":ids}))["evidence"]
                        assert [e["id"] for e in evidence]==ids
                        top = []
                        for e in evidence:
                            path, blob = files[e["version_id"]]
                            a = e["anchor"]
                            assert digest(blob[a["start_byte"]:a["end_byte"]])==e["quote_hash"]==digest(e["text"].encode())
                            top.append(dict(id=e["id"],file=path,start_line=a["start_line"],end_line=a["end_line"],
                                            quote_sha256=e["quote_hash"]))
                        row[arm] = {"top8":top, "rank":score(top,case), "search_ms":[]}
                    assert ids==[e["id"] for e in row[arm]["top8"]]
                    row[arm]["search_ms"].append(elapsed)
            rows.append(row)
            if idx%4==0:
                symbol=case["symbol"]
                for kind,q in [("bare",symbol),("behaviour",f"调用 `{symbol}` 后，怎样校验返回结果？"),
                               ("multiple",f"找 `{symbol}` 与 `another_fn` 的定义")]:
                    responses = [(await fetch(arm,q))[0] for arm in ranking]
                    checks.append(dict(id=case["id"],kind=kind,identical=responses[0]==responses[1]==responses[2]))
        denied=Identity("rag_eval","unshared_reader","work")
        no_access={arm:len((await fetch(arm,cases[0]["question"],denied))[0]) for arm in ranking}
        assert all(r["identical"] for r in checks) and not any(no_access.values())
        await db.aclose()
    return dict(split=split, cases=len(cases), index=dict(files=len(files),chunks=staged["chunks"]),
                original_manifest=digest((HERE/"cohort.json").read_bytes()),holdout_manifest=digest((HERE/"reserve.json").read_bytes()),
                controls=checks,unauthorized_results=no_access,metrics={arm:summarize(rows,arm) for arm in ranking},rows=rows)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("split",choices=["development","reserve"]);parser.add_argument("out",type=Path)
    args=parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    result=asyncio.run(evaluate(args.split))
    with args.out.open("x") as out:
        json.dump(result,out,ensure_ascii=False,indent=2);out.write("\n")
    print(result["split"],json.dumps(result["metrics"]))
