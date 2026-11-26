#!/usr/bin/env bash
# Launch on the evaluation host. The native adapter resumes saved Task IDs.
set -euo pipefail
umask 077
root=/opt/swebench/cache/swe-tuning-20261123
previous=/opt/swebench/cache/mini50-20261121-runtime-arm
case "${1:-}" in
    mini-50) run=mini50-tuned-c-gpt62-20261123; ids="$previous/mini50.ids" ;;
    init-50) run=init50-gpt62-tuned-20261123; ids="$root/init-50/ids.txt" ;;
    *) echo "Expected init-50 or mini-50" >&2; exit 2 ;;
esac
cohort=$1
out=/opt/swebench/runs/$run
mkdir -p "$out"
exec 9>"$out/.launcher.lock"
flock -n 9 || { echo "A launcher already owns $out" >&2; exit 1; }
authfile=$(mktemp /run/swe-tuned-1123.XXXXXX)
trap 'rm -f "$authfile"' EXIT
/root/ar-release/ops-token.sh swebench mini50-eval 12h >"$authfile"
export AR_TASK_API_TOKEN_FILE="$authfile"
src=/root/ar-release/rev45-reconcile/src
export PYTHONPATH="$previous/quota-guard-20261122/src:$root/src:$previous:$src/services/py-learning/src:$src/libs/py-core/src:$src/proto/gen/python"
bin=/opt/evals/venv-5b0b26a/bin/python
rc=0
"$bin" "$root/run_tuned.py" --selection "$root/$cohort/selection.json" --ids "$ids" --freeze "$root/freeze.json" \
    --out "$out" --prewarm "$root/prewarm-tuned.py" --concurrency 2 || rc=$?
if [ "$rc" -eq 75 ]; then exit 75; fi
# The generator uses historical helper scripts, including a file named swebench.py.
# Official scoring must resolve swebench from its own installed harness.
export PYTHONPATH="$previous/quota-guard-20261122/src:$root/src:$src/services/py-learning/src:$src/libs/py-core/src:$src/proto/gen/python"
if [ -s "$out/predictions.jsonl" ]; then
    touch "$out/.final-grading"
    export DOCKER_HOST=unix:///var/run/docker.sock
    "$bin" "$root/stream_grade.py" "$out" --drain
    flock "$root/grader-host.lock" "$bin" "$root/stream_grade.py" "$out" --verify
    flock "$root/grader-host.lock" "$bin" -m agentruntime_learning.benchmarks swebench grade --out "$out" --python /opt/swebench/venv/bin/python --max-workers 1
    "$bin" -m agentruntime_learning.benchmarks swebench report --out "$out"
fi
exit "$rc"
