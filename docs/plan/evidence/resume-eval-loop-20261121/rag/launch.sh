#!/bin/sh
# Run on the benchmark host. Supply the immutable original harness and a new run directory.
set -eu
d=${1:?new run directory}
src=${2:?frozen runtime source root}
frozen=${3:?existing benchmark evidence root}
harness=${4:?immutable previously executed benchmark.py}
scripts=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test ! -e "$d/run/rows.jsonl"
mkdir -p "$d/run"
python3 "$scripts/prepare.py" --harness "$harness" --engine "$src/services/py-memory/src/agentruntime_memory/retrieval/engine.py" --manifest "$frozen/run/manifest.json" --output "$d/overlay" --per-channel "${5:-8}"
cp "$d/overlay/protocol.json" "$d/run/"
# The pinned image runs as 10001. Only change our new output directory.
chown 10001:10001 "$d/run"
tag=$(basename "$d" | tr '[:upper:]' '[:lower:]')
docker run -d --name "$tag" --network none --cpus 4 --memory 8g --pids-limit 256 --cap-drop=ALL --security-opt=no-new-privileges \
 -e TRANSFORMERS_OFFLINE=1 -e HF_HUB_OFFLINE=1 -e OMP_NUM_THREADS=4 -e PYTHONDONTWRITEBYTECODE=1 \
 -v "$src:/src:ro" -v "$d/overlay/engine.py:/src/services/py-memory/src/agentruntime_memory/retrieval/engine.py:ro" \
 -v "$d/overlay/benchmark.py:/src/scripts/file-engines/benchmark.py:ro" -v "$frozen/models:/models:ro" \
 -v "$d/run:/workspace/run" -v "$frozen/run/parse:/workspace/run/parse:ro" -v "$frozen/run/manifest.json:/workspace/run/manifest.json:ro" \
 --entrypoint /opt/file-engines/bin/python sha256:984b4bfbcaa8765d14412e7d6de24d078cf0e30279226cec18161fd2c94015cf \
 /src/scripts/file-engines/benchmark.py retrieve --work /workspace/run --model-dir /models/e5 --reranker-dir /models/reranker
echo "$tag" > "$d/container-name"
