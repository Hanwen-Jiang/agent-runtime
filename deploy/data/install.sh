#!/usr/bin/env bash
# 装数据层与观测组件（docs/plan/cloud-deploy.md D-03），deploy/cluster/install.sh 与密钥（deploy/cluster/secrets）之后运行：
#   deploy/data/install.sh
# Postgres（CloudNativePG）、Runtime 的 Valkey、Temporal、Qdrant、Redpanda 装在 data；Langfuse、Tempo、VictoriaMetrics、Grafana 装在 observability。
# 版本见 deploy/cluster/versions.conf；资源的预算见 deploy/cluster/README.md。重复运行是安全的。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../cluster/versions.conf
source "$HERE/../cluster/versions.conf"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
log() { printf '==> %s\n' "$*"; }

for s in pg-agentruntime pg-temporal pg-langfuse pg-coding-github r2-backup valkey-agentruntime; do
  kubectl -n data get secret "$s" >/dev/null || { echo "data 命名空间缺 Secret $s（见 deploy/cluster/secrets/README.md）" >&2; exit 1; }
done
for s in grafana-admin grafana-resend; do
  kubectl -n observability get secret "$s" >/dev/null || { echo "observability 命名空间缺 Secret $s（见 deploy/cluster/secrets/README.md）" >&2; exit 1; }
done

log "Postgres"
kubectl apply -f "$HERE/networkpolicy.yaml"
kubectl apply -f "$HERE/postgres.yaml"
kubectl -n data wait --for=condition=Ready cluster/pg --timeout=15m

log "Valkey（Runtime 的 Cache，CACHE-01）"
kubectl apply -f "$HERE/valkey.yaml"
kubectl -n data rollout status deploy/valkey-agentruntime --timeout=5m
# Memory Service 的向量库（V1-MEM）：Milvus 单机版，内嵌 etcd、本地存储。
kubectl apply -f "$HERE/milvus.yaml"
kubectl -n data rollout status sts/milvus --timeout=10m

helm repo add temporal https://go.temporal.io/helm-charts >/dev/null
helm repo add qdrant https://qdrant.github.io/qdrant-helm >/dev/null
helm repo add redpanda https://charts.redpanda.com >/dev/null
helm repo add langfuse https://langfuse.github.io/langfuse-k8s >/dev/null
helm repo add grafana https://grafana-community.github.io/helm-charts >/dev/null
helm repo add vm https://victoriametrics.github.io/helm-charts >/dev/null
helm repo update >/dev/null

log "Temporal"
# Runtime 用的命名空间 agent-runtime（保留期 7 天，短于业务对象的生命周期，S-06）由 Chart 的 namespace Job 建（values/temporal.yaml）。
helm upgrade --install temporal temporal/temporal --version "$TEMPORAL_CHART_VERSION" -n data -f "$HERE/values/temporal.yaml" --wait --timeout 15m

log "Qdrant、Redpanda"
helm upgrade --install qdrant qdrant/qdrant --version "$QDRANT_CHART_VERSION" -n data -f "$HERE/values/qdrant.yaml" --wait
helm upgrade --install redpanda redpanda/redpanda --version "$REDPANDA_CHART_VERSION" -n data -f "$HERE/values/redpanda.yaml" --wait --timeout 15m

log "ClickHouse operator（Langfuse 2.x 的 ClickHouse 由它部署；要 cert-manager，deploy/cluster/install.sh 装过）"
helm upgrade --install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm --version "$CLICKHOUSE_OPERATOR_VERSION" \
  -n clickhouse-operator --create-namespace --wait

log "Langfuse、Tempo、VictoriaMetrics、Grafana"
helm upgrade --install langfuse langfuse/langfuse --version "$LANGFUSE_CHART_VERSION" -n observability -f "$HERE/values/langfuse.yaml" --wait --timeout 15m
helm upgrade --install tempo grafana/tempo --version "$TEMPO_CHART_VERSION" -n observability -f "$HERE/values/tempo.yaml" --wait
# 指标的后端（TMP 计划第 9.3 节）：Collector 经 remote write 写入，Grafana 的趋势面板与告警读它。
helm upgrade --install vm vm/victoria-metrics-single --version "$VM_CHART_VERSION" -n observability -f "$HERE/values/victoria-metrics.yaml" --wait
helm upgrade --install grafana grafana/grafana --version "$GRAFANA_CHART_VERSION" -n observability -f "$HERE/values/grafana.yaml" --wait
kubectl apply -f "$HERE/dashboards/"

log "核对业务库的角色（rolsuper、rolbypassrls 都应为 f）"
primary="$(kubectl -n data get pod -l cnpg.io/cluster=pg,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')"
kubectl -n data exec "$primary" -c postgres -- psql -X -A -t -c \
  "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('agentruntime', 'temporal', 'langfuse') ORDER BY 1"
log "完成。备份恢复演练：deploy/data/restore-drill.sh"
