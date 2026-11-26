#!/usr/bin/env bash
set -euo pipefail
. /opt/evals/deployments/capability-resume-20261123/capability-env-1123.sh
cd "$D/core"
export PYTHONPATH="$D/core:$D/core/src/services/py-learning/src:$PYTHONPATH"
"$P" preflight.py
exec "$P" -m agentruntime_learning.capability run --plan "$D/core/plan.json" --out /opt/evals/mx/capability-resume-20261123-core --profiles "$D/profiles.json" --freeze-config "$D/core/freeze.json"
