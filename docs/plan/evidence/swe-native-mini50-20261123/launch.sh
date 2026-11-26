#!/usr/bin/env bash
# Evaluator host only. Both arms use the upstream daemon and common grader image digests.
set -euo pipefail
umask 077
arm=$1
case "$arm" in b|d) ;; *) exit 2;; esac
root=/opt/swebench/cache/swe-native-mini50-20261123
prev=/opt/swebench/cache/mini50-20261121-runtime-arm
old=/opt/swebench/cache/swe-tuning-20261123
src=/root/ar-release/rev45-reconcile/src
out=/opt/swebench/runs/mini50-native-20261123-$arm
bin=/opt/evals/venv-5b0b26a/bin/python
export PYTHONPATH="$root:$prev/quota-guard-20261122/src:$old/src:$prev:$src/services/py-learning/src:$src/libs/py-core/src:$src/proto/gen/python"
"$bin" -c 'from deploy_native import HOME_DIR,validate_matrix; validate_matrix(HOME_DIR/"matrix-release.json")'
mkdir -p "$out"
exec 9>"$out/.launcher.lock"
flock -n 9 || { echo 'This run is already active' >&2; exit 1; }
auth=$(mktemp /run/native-mini50.XXXXXX)
trap 'rm -f "$auth"' EXIT
/root/ar-release/ops-token.sh swebench mini50-eval 12h >"$auth"
export AR_TASK_API_TOKEN_FILE="$auth"
more=()
freeze="$prev/freeze.json"
if [ "$arm" = d ]; then
    more=(--reference /opt/swebench/runs/mini50-native-20261123-b/route-protocol.json)
    freeze="$root/candidate/freeze.json"
fi
export DOCKER_HOST=unix:///var/run/docker.sock
rc=0
"$bin" "$root/run_native.py" "$arm" --out "$out" --freeze "$freeze" --prewarm /opt/swebench/cache/mini50-preflight/prewarm.py "${more[@]}" || rc=$?
if [ "$rc" = 75 ]; then exit 75; fi
if [ -s "$out/predictions.jsonl" ]; then
    export PYTHONPATH="$prev/quota-guard-20261122/src:$old/src:$src/services/py-learning/src:$src/libs/py-core/src:$src/proto/gen/python"
    touch "$out/.final-grading"
    "$bin" "$old/stream_grade.py" "$out" --drain
    flock "$old/grader-host.lock" "$bin" "$old/stream_grade.py" "$out" --verify
    flock "$old/grader-host.lock" "$bin" -m agentruntime_learning.benchmarks swebench grade --out "$out" --python /opt/swebench/venv/bin/python --max-workers 1
    "$bin" -m agentruntime_learning.benchmarks swebench report --out "$out"
fi
exit "$rc"
