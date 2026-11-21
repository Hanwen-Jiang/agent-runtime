"""Evaluate the committed lexical retriever on two local Git snapshots."""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from agentruntime_learning.evals.rag import LexicalRetriever


HERE = Path(__file__).resolve().parent
COHORT_SHA = "c6c52bc214489c96767ab6743b6631634afe5c972191ea01f80bce59e8bf98ac"


def corpus(repos, revisions):
    chunks = []
    files = 0
    for repo, rev in sorted(revisions.items()):
        cwd = repos / repo
        tree = subprocess.check_output(["git", "ls-tree", "-lr", "-z", rev], cwd=cwd)
        with subprocess.Popen(["git", "cat-file", "--batch"], cwd=cwd, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE) as objects:
            for entry in tree.split(b"\0"):
                if not entry:
                    continue
                meta, path = entry.decode().split("\t", 1)
                mode, kind, oid, size = meta.split()
                parts = Path(path).parts
                if kind != "blob" or mode not in {"100644", "100755"} or int(size) > 131072:
                    continue
                if path != "README.md" and not (
                    parts[0] in {"internal", "ui", "cmd", "services", "libs", "docs"}
                    and Path(path).suffix in {".go", ".py", ".rs", ".ts", ".tsx", ".md"}
                ):
                    continue
                if set(parts) & {"tests", "test", "testdata", "fixtures", "evidence", "site_pages", "vendor", "gen", "node_modules"}:
                    continue
                if Path(path).name.startswith("test_") or path.endswith(("_test.go", ".test.ts", ".gen.go")):
                    continue
                objects.stdin.write((oid + "\n").encode())
                objects.stdin.flush()
                header = objects.stdout.readline().split()
                content = objects.stdout.read(int(header[2])).decode("utf-8", errors="replace")
                objects.stdout.read(1)
                lines = content.splitlines()
                if not lines:
                    continue
                files += 1
                key = f"{repo}/{path}"
                for start in range(0, len(lines), 48):
                    stop = min(start + 64, len(lines))
                    chunks.append({"id": f"{key}:{start + 1}-{stop}", "text": key + "\n" + "\n".join(lines[start:stop]),
                                   "file": key, "start": start + 1, "end": stop})
                    if stop == len(lines):
                        break
            objects.stdin.close()
    return files, chunks


def add_subwords(text):
    words = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text):
        parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token).replace("_", " ").split()
        if len(parts) > 1:
            words.extend(part.lower() for part in parts)
    return text + "\n" + " ".join(words)


async def evaluate(repos, split, cohort, actual_sha, candidate):
    files, chunks = corpus(repos, cohort["repository_revisions"])
    by_id = {c["id"]: c for c in chunks}
    chosen = [q for q in cohort["questions"] if q["split"] == split]
    result = {"split": split, "cohort_sha256": actual_sha, "indexed_files": files, "chunks": len(chunks),
              "cases": len(chosen), "variants": {}}
    for variant in ("baseline", candidate):
        transform = add_subwords if variant == "identifier_subwords" else lambda x: x
        searchable = [c for c in chunks if not c["file"].endswith(".md")] if variant == "implementation_scope" else chunks
        retriever = LexicalRetriever([{"id": c["id"], "text": transform(c["text"])} for c in searchable])
        rows = []
        for q in chosen:
            started = time.perf_counter()
            hits = await retriever.retrieve(transform(q["question"]), 8)
            latency = time.perf_counter() - started
            gold = q["repository"] + "/" + q["gold_file"]
            matches = [(i, by_id[h["id"]]) for i, h in enumerate(hits, 1) if by_id[h["id"]]["file"] == gold]
            ranks = [i for i, h in matches if h["start"] <= q["evidence_line"] <= h["end"]]
            rows.append({"id": q["id"], "file_hit8": int(bool(matches)), "evidence_hit8": int(bool(ranks)),
                         "evidence_rank": min(ranks, default=None), "query_ms": round(1000 * latency, 2),
                         "top8": [h["id"] for h in hits]})
        result["variants"][variant] = {"searchable_chunks": len(searchable), "file_hits": sum(r["file_hit8"] for r in rows),
                                       "evidence_hits": sum(r["evidence_hit8"] for r in rows), "rows": rows}
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", type=Path, required=True)
    ap.add_argument("--split", choices=["development", "validation"], required=True)
    ap.add_argument("--candidate", choices=["identifier_subwords", "implementation_scope"], required=True)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    raw = (HERE / "cohort.json").read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != COHORT_SHA:
        raise SystemExit("cohort differs from frozen manifest")
    if args.out.exists():
        raise SystemExit("use a new output file")
    output = asyncio.run(evaluate(args.repos, args.split, json.loads(raw), sha, args.candidate))
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(args.split, output["indexed_files"], output["chunks"],
          {v: (s["file_hits"], s["evidence_hits"]) for v, s in output["variants"].items()})
