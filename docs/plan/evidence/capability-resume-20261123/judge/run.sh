#!/usr/bin/env bash
set -euo pipefail
. /opt/evals/deployments/capability-resume-20261123/capability-env-1123.sh
out=/opt/evals/mx/capability-resume-20261123-judge-v2
"$P" -m agentruntime_learning.capability run --plan "$D/judge/formal-v2.json" --out "$out" --profiles "$D/profiles.json" --freeze-config "$D/freeze.json"
