    # Uses the original authorized gRPC index/search and exact relevance function above.
    plan = load(work / "protocol.json")
    lookup = {q["id"]: q for q in manifest["queries"]}
    learned = engine.rerank
    assert learned is not None
    async def measure(q, arm):
        engine.rerank = learned if arm == "capped" else None
        before = gateway.score_pairs
        started = time.perf_counter()
        response = await rpc("search", {**params, "query": q["query"], "embedding_version": model_info["version"], "limit": 8})
        elapsed = 1000 * (time.perf_counter() - started)
        hits = response["hits"]
        return {**metrics(hits, q), "latency_ms": elapsed, "pairs": gateway.score_pairs - before,
                "hit_ids": [h["id"] for h in hits]}
    warmup = lookup[plan["split"]["development"][0]]
    for arm in ["hybrid", "capped"]:
        await measure(warmup, arm)
    output = []
    for phase, ids in plan["split"].items():
        floor = time.perf_counter()
        for n, qid in enumerate(ids):
            q = lookup[qid]
            row = {"id": qid, "phase": phase}
            for arm in (["hybrid", "capped"] if n % 2 == 0 else ["capped", "hybrid"]):
                row[arm] = await measure(q, arm)
            output.append(row)
            with (work / "rows.jsonl").open("a") as writer:
                writer.write(json.dumps(row, ensure_ascii=False) + "\n")
            if (n+1) % 5 == 0: print(phase, n+1, "/", len(ids), flush=True)
        print(phase, "seconds", round(time.perf_counter()-floor, 2), flush=True)
    dump(work / "environment.json", {"e5": model_info["revision"], "reranker": reranker_info["revision"],
         "index_seconds": index_seconds, "index_document_count": len(docs), "search_pairs_total_including_warmup": gateway.score_pairs,
         "paid_api_requests": 0, "environment": environment()})
    await channel.close()
    await server.stop(0)
