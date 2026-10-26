#!/usr/bin/env bash
# 恢复演练（docs/plan/cloud-deploy.md D-03 验收 3）：先做一次即时备份，从 R2 恢复出 pg-restore，逐库逐表比对行数与内容的校验和，
# 一致才算过；最后删掉 pg-restore。用法：deploy/data/restore-drill.sh（kubectl 指向集群，CloudNativePG 与 Barman Cloud 插件已装好）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NS=data
DATABASES=(agentruntime temporal temporal_visibility langfuse)

psql_in() {  # psql_in <集群> <库> <SQL>：在集群的主实例里以 postgres 身份执行，只读查询。
  local pod
  pod="$(kubectl -n "$NS" get pod -l "cnpg.io/cluster=$1,cnpg.io/instanceRole=primary" -o jsonpath='{.items[0].metadata.name}')"
  kubectl -n "$NS" exec "$pod" -c postgres -- psql -X -A -t -v ON_ERROR_STOP=1 -d "$2" -c "$3"
}

# 每张表一行：表名、行数、按全部列排序后的 md5。
FINGERPRINT_SQL="
SELECT format('%I.%I', n.nspname, c.relname) || ' ' ||
       (xpath('/row/c/text()', query_to_xml(format(
          'SELECT count(*) AS c FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text || ' ' ||
       (xpath('/row/m/text()', query_to_xml(format(
          'SELECT coalesce(md5(string_agg(t::text, %L ORDER BY t::text)), %L) AS m FROM %I.%I t', E'\\n', '-', n.nspname, c.relname),
          false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;"

echo "==> 即时备份"
backup="drill-$(date -u +%Y%m%d%H%M%S)"
kubectl -n "$NS" apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: {name: $backup, namespace: $NS}
spec:
  cluster: {name: pg}
  method: plugin
  pluginConfiguration: {name: barman-cloud.cloudnative-pg.io}
EOF
kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=completed "backup/$backup" --timeout=30m

echo "==> 记下原库的指纹（备份完成之后不再写入的表才比得上；演练时先停掉写入方或在维护窗口做）"
for db in "${DATABASES[@]}"; do psql_in pg "$db" "$FINGERPRINT_SQL" > "/tmp/drill-$db.expected"; done

echo "==> 从 R2 恢复出 pg-restore"
kubectl apply -f "$HERE/restore-drill.yaml"
trap 'kubectl -n "$NS" delete cluster pg-restore --ignore-not-found --wait=false' EXIT
kubectl -n "$NS" wait --for=condition=Ready cluster/pg-restore --timeout=60m

failed=0
for db in "${DATABASES[@]}"; do
  psql_in pg-restore "$db" "$FINGERPRINT_SQL" > "/tmp/drill-$db.actual"
  if diff -u "/tmp/drill-$db.expected" "/tmp/drill-$db.actual"; then
    echo "一致：$db（$(wc -l < "/tmp/drill-$db.expected") 张表）"
  else
    echo "不一致：$db" >&2
    failed=1
  fi
done
exit "$failed"
