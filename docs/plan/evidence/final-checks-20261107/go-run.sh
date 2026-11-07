#!/usr/bin/env bash
set -euo pipefail
TOOLS=/root/proj/agent-projs/.goal-tools
original_inotify=$(cat /proc/sys/fs/inotify/max_user_instances)
printf '%s\n' "$original_inotify" > "$TOOLS/go-final-environment/inotify.before"
[ -z "$(docker ps -aq --filter label=io.x-k8s.kind.cluster=agentruntime-test)" ] || { echo 'existing test cluster; stopping'; exit 1; }
cleanup() {
  /root/proj/agent-projs/.goal-tools/go-final-environment/kind delete cluster --name agentruntime-test > "$TOOLS/go-final-environment/cleanup.log" 2>&1 || true
  sysctl -w fs.inotify.max_user_instances="$original_inotify" >> "$TOOLS/go-final-environment/cleanup.log"
}
trap cleanup EXIT
sysctl -w fs.inotify.max_user_instances=1024 > "$TOOLS/go-final-environment/preflight.log"
docker run --rm --name goal-final-go-toolchain --network host --cpus 2 --memory 6g --user 10001:10001 --group-add 110 \
 -e PATH=/venv/bin:/opt/tools/go/bin:/opt/tools/bin:/usr/lib/postgresql/17/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
 -e GOMODCACHE=/gomod -e GOCACHE=/gocache -e GOTOOLCHAIN=local -e GOMAXPROCS=2 \
 -e AR_SANDBOX_AGENT_BIN=/agent/sandbox-agent -e AR_SANDBOX_AGENT_IMAGE=ghcr.io/agentruntime/sandbox-agent:goal-f1388e13 \
 -e AR_DOCKER_BIN=/tools/kind-diagnose-20261107/bin/docker -e AR_KIND_REUSE=1 \
 -e AR_KIND_NODE_IMAGE=kindest/node@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5 \
 -e AR_KIND_WORKSPACES=/tmp/goal-go-final-workspaces -e AR_KIND_GVISOR=/tools/go-final-environment/gvisor-installed.tar \
 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work/tests/contract:/work/libs/py-core/src:/work/proto/gen/python:/work/services/py-worker/src \
 -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/tools/docker-current:ro \
 -v /root/proj/agent-projs/.worktrees/root-integration/agent-runtime:/work:ro \
 -v /root/proj/agent-projs/.worktrees/network/agent-runtime/.venv:/venv:ro \
 -v /root/ar-release/goal-full-20261106/sandbox-agent-f1388e13:/agent/sandbox-agent:ro \
 -v goal-root-gomod:/gomod -v goal-root-gocache:/gocache -v "$TOOLS:/tools" \
 -v /tmp/goal-go-final-workspaces:/tmp/goal-go-final-workspaces \
 -w /work agentruntime-ci:3 /venv/bin/python -m pytest tests/contract/test_toolchain.py::test_go_modules_build_vet_and_test -q -p no:cacheprovider
