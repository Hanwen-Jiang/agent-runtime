#!/usr/bin/env bash
set -euo pipefail
d=/opt/evals/deployments/capability-resume-20261123
case "$1" in core) src=core;; transform|judge-v2) src=base;; *) exit 2;; esac
. "$d/capability-env-1123.sh"
export PYTHONPATH="$d/core:$d/quota-recovery/$src/services/py-learning/src:$PYTHONPATH"
exec "$P" "$d/supplemental-code/supplemental.py" "$@"
