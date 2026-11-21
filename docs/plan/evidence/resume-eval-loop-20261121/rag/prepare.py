"""Build an isolated, hash-checked experiment overlay. Never modifies the product repository."""
import argparse
import hashlib
import json
from pathlib import Path

HARNESS_SHA = "2e91f9b610e603a53c9504a38853faaa0a25161307dfb17d5d20dd013141a128"
ENGINE_SHA = "f779001730e7327f8aafb7686009faef7b738eb953e1ffa7c5c7cb9e4286dd72"
MANIFEST_SHA = "3639f4f6d66325098d3530f797c74e32a2e36c032c9f75f7e07082eb1dbd2a17"
SPLIT_SEED = "resume-rag-policy-20261121-v1"
hash_bytes = lambda b: hashlib.sha256(b).hexdigest()


def verified(path, sha):
    payload = path.read_bytes()
    if hash_bytes(payload) != sha:
        raise ValueError(f"source hash mismatch for {path}")
    return payload.decode()


def split_ids(manifest):
    topics = sorted({q["topic"] for q in manifest["queries"]},
                    key=lambda i: hash_bytes(f"{SPLIT_SEED}:{i}".encode()))
    development = set(topics[:8])
    return {part: sorted(q["id"] for q in manifest["queries"] if (q["topic"] in development) == dev)
            for part, dev in [("development", True), ("validation", False)]}


def main():
    p = argparse.ArgumentParser()
    for key in ["harness", "engine", "manifest", "output"]:
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--per-channel", type=int, choices=(6, 8), default=8)
    a = p.parse_args()
    h = verified(a.harness, HARNESS_SHA)
    e = verified(a.engine, ENGINE_SHA)
    manifest = json.loads(verified(a.manifest, MANIFEST_SHA))
    target = "top_k=40,"
    for mode in ("dense=dense", "sparse=sparse"):
        original = f"{target}{mode},filter=Filter(must=must)"
        if e.count(original) != 1:
            raise ValueError(f"ambiguous or missing query arm: {original}")
        e = e.replace(original, f"top_k={a.per_channel},{mode},filter=Filter(must=must)")
    # Only retrieval-channel candidate breadth is changed; the original non-reranked path is untouched.
    start = h.index("    for q in manifest['queries']:", h.index("async def retrieve("))
    end = h.index("\ndef comparison(work):", start)
    loop = Path(__file__).with_name("query_loop.py.inc").read_text()
    h = h[:start] + loop + "\n" + h[end:]
    out = a.output
    out.mkdir(parents=True, exist_ok=True)
    (out / "engine.py").write_text(e)
    (out / "benchmark.py").write_text(h)
    split = split_ids(manifest)
    (out / "protocol.json").write_text(json.dumps({
        "split_seed": SPLIT_SEED, "split_unit": "topic (all five question facets travel together)",
        "source_manifest_sha256": MANIFEST_SHA, "source_harness_sha256": HARNESS_SHA, "source_engine_sha256": ENGINE_SHA,
        "arms": {"hybrid": "original E5 hybrid retrieval, no scorer", "capped": f"dense {a.per_channel} + sparse {a.per_channel}, deduplicate, score and return top 8"},
        "acceptance_gate": "on validation: capped Hit@8 >= hybrid + 0.05, Recall@8 >= hybrid, and warm search RPC P95 <= 2000 ms",
        **({"per_channel_candidates": a.per_channel} if a.per_channel != 8 else {}),
        "historical_full_rerank": "dense 40 + sparse 40; reported separately, not timed again",
        "split": split, "effective_engine_sha256": hash_bytes(e.encode()), "effective_harness_sha256": hash_bytes(h.encode()),
    }, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
