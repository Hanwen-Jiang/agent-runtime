#!/usr/bin/env bash
# Invoke only after funding confirmation and successful local reconciliation.
set -euo pipefail
d=/opt/evals/deployments/capability-resume-20261123
case "${1:-}" in
  core) rid=capability-resume-20261123-core; src=core; freeze="$d/core/freeze.json";;
  transform) rid=capability-resume-20261123-transform; src=base; freeze="$d/freeze.json";;
  judge-v2) rid=capability-resume-20261123-judge-v2; src=base; freeze="$d/freeze.json";;
  *) echo 'Expected core, transform or judge-v2' >&2; exit 2;;
esac
out="/opt/evals/mx/$rid"
if [[ -e "$out/user-pause.json" ]]; then
  echo "This matrix run was stopped at the user's request: $rid" >&2; exit 75
fi
if [[ -e "$out/provider-quota-pause.json" || ! -f "$out/resume-reconciliation.json" ]]; then
  echo "Provider quota is paused, or receipts still await reconciliation: $rid" >&2; exit 75
fi
. "$d/capability-env-1123.sh"
export PYTHONPATH="$d/core:$d/quota-recovery/$src/services/py-learning/src:$PYTHONPATH"
exec "$P" -m agentruntime_learning.capability run --plan "$out/plan.json" --out "$out" \
  --profiles "$d/profiles.json" --freeze-config "$freeze"
