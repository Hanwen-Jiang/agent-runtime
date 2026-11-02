{{/* 名字前缀：<release>-agent-runtime（release 名已含 agent-runtime 时不重复）。 */}}
{{- define "ar.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* 公共标签；用法：include "ar.labels" (list $ "task-service") */}}
{{- define "ar.labels" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: {{ $root.Chart.Name }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/version: {{ $root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ $root.Release.Service }}
app.kubernetes.io/part-of: agent-runtime
app.kubernetes.io/component: {{ index . 1 }}
helm.sh/chart: {{ printf "%s-%s" $root.Chart.Name $root.Chart.Version }}
{{- end -}}

{{/* 选择器标签；用法同上。 */}}
{{- define "ar.selector" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: {{ $root.Chart.Name }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{/* 镜像；用法：include "ar.image" (list $ "worker") */}}
{{- define "ar.image" -}}
{{- $root := index . 0 -}}
{{- $tag := default $root.Chart.AppVersion $root.Values.image.tag -}}
{{- if not (kindIs "string" $tag) -}}
{{- fail "image.tag 要写成字符串：全是数字的提交号要加引号，否则镜像名是错的" -}}
{{- end -}}
{{- printf "%s/%s:%s" $root.Values.image.registry (index . 1) $tag -}}
{{- end -}}

{{- define "ar.collectorService" -}}
{{- printf "%s-otel-collector" (include "ar.fullname" .) -}}
{{- end -}}

{{- define "ar.modelGatewayService" -}}
{{- printf "%s-model-gateway" (include "ar.fullname" .) -}}
{{- end -}}

{{/* 租户模型白名单 ConfigMap：显式外部引用优先，云上默认策略才使用内置 ConfigMap。 */}}
{{- define "ar.modelGatewayTenantPoliciesConfigMap" -}}
{{- if .Values.modelGateway.tenantPoliciesConfigMap -}}
{{- .Values.modelGateway.tenantPoliciesConfigMap -}}
{{- else if .Values.modelGateway.defaultTenantPolicies.enabled -}}
{{- default (printf "%s-default-tenant-policies" (include "ar.fullname" .)) .Values.modelGateway.defaultTenantPolicies.name -}}
{{- end -}}
{{- end -}}

{{- define "ar.registryService" -}}
{{- printf "%s-registry" (include "ar.fullname" .) -}}
{{- end -}}

{{/* 各服务共用的环境变量：Telemetry（T-15）与 SecretStore 的文件。
     secrets.storeClaim 设了时（D-04），会写 kek 的服务（task-service、Worker、Memory Service：用 ar.storeEnv）把密钥文件放在
     那个可写的共用卷上，只读的 Secret 作为种子在启动时并进去；只读的服务（model-gateway、learning：用 ar.commonEnv）直接读 Secret。 */}}
{{- define "ar.telemetryEnv" -}}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ printf "http://%s:4318" (include "ar.collectorService" .) | quote }}
- name: OTEL_SERVICE_VERSION
  value: {{ default .Chart.AppVersion .Values.image.tag | quote }}
- name: OTEL_DEPLOYMENT_ENVIRONMENT
  value: {{ .Values.global.environment | quote }}
{{- end -}}

{{- define "ar.secretsMountFile" -}}
{{- printf "/var/run/agentruntime/secrets/%s" .Values.secrets.secretsFileKey -}}
{{- end -}}

{{- define "ar.commonEnv" -}}
{{ include "ar.telemetryEnv" . }}
- name: AR_SECRETS_FILE
  value: {{ include "ar.secretsMountFile" . | quote }}
{{- end -}}

{{- define "ar.storeEnv" -}}
{{ include "ar.telemetryEnv" . }}
{{- if .Values.secrets.storeClaim }}
- name: AR_SECRETS_FILE
  value: /var/lib/agentruntime/secretstore/secrets.json
- name: AR_SECRETS_SEED_FILE
  value: {{ include "ar.secretsMountFile" . | quote }}
{{- else }}
- name: AR_SECRETS_FILE
  value: {{ include "ar.secretsMountFile" . | quote }}
{{- end }}
{{- end -}}

{{- define "ar.signingKeyEnv" -}}
- name: AR_DEV_SIGNING_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.existingSecret }}
      key: {{ .Values.secrets.signingKeyKey }}
{{- end -}}

{{- define "ar.dsnEnv" -}}
{{- if .Values.secrets.relationalDsn.name }}
- name: AR_RELATIONAL_DSN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.relationalDsn.name }}
      key: {{ .Values.secrets.relationalDsn.key }}
{{- end }}
{{- end -}}

{{/* ObjectStore 的环境变量（task-service 与 Worker 相同）：s3.endpoint 设了用 S3 兼容的对象存储（D-09），否则是 /objects 目录。 */}}
{{- define "ar.objectStoreEnv" -}}
{{- $s3 := .Values.objectStore.s3 -}}
{{- if $s3.endpoint }}
{{- if not (and $s3.bucket $s3.accessKeyId $s3.secretAccessKey.name) }}
{{- fail "objectStore.s3.endpoint 设了还要设 bucket、accessKeyId 与 secretAccessKey.name" }}
{{- end }}
{{- if not (hasPrefix "https://" $s3.endpoint) }}
{{- fail "objectStore.s3.endpoint 要是 https 地址" }}
{{- end }}
- name: AR_S3_ENDPOINT
  value: {{ $s3.endpoint | quote }}
- name: AR_S3_BUCKET
  value: {{ $s3.bucket | quote }}
- name: AR_S3_REGION
  value: {{ $s3.region | default "auto" | quote }}
{{- with $s3.prefix }}
- name: AR_S3_PREFIX
  value: {{ . | quote }}
{{- end }}
- name: AR_S3_ACCESS_KEY_ID
  value: {{ $s3.accessKeyId | quote }}
- name: AR_S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $s3.secretAccessKey.name }}
      key: {{ $s3.secretAccessKey.key }}
{{- else }}
- name: AR_OBJECT_STORE_DIR
  value: /objects
{{- end }}
{{- end -}}

{{/* Worker 要不要 Kubernetes 的权限：shell_run 经 Sandbox 接口、且登记了 kind=k8s 的后端时要（在沙箱命名空间里管 Pod）。 */}}
{{- define "ar.workerNeedsKube" -}}
{{- $shell := .Values.worker.shell -}}
{{- if eq $shell.mode "sandbox" -}}
{{- range $name, $b := $shell.backends -}}
{{- if eq $b.kind "k8s" -}}true{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "ar.workerServiceAccount" -}}
{{- printf "%s-worker" (include "ar.fullname" .) -}}
{{- end -}}

{{/* 公共的卷：SecretStore 的文件（只读）、/tmp、数据目录与业务对象目录。 */}}
{{- define "ar.volumes" -}}
- name: secrets
  secret:
    secretName: {{ .Values.secrets.existingSecret }}
    items:
      - key: {{ .Values.secrets.secretsFileKey }}
        path: {{ .Values.secrets.secretsFileKey }}
- name: tmp
  emptyDir: {}
- name: data
  emptyDir: {}
- name: objects
{{- if .Values.objectStore.claimName }}
  persistentVolumeClaim:
    claimName: {{ .Values.objectStore.claimName }}
{{- else }}
  emptyDir: {}
{{- end }}
{{- with .Values.secrets.storeClaim }}
- name: secretstore
  persistentVolumeClaim:
    claimName: {{ . }}
{{- end }}
{{- end -}}

{{- define "ar.volumeMounts" -}}
- name: secrets
  mountPath: /var/run/agentruntime/secrets
  readOnly: true
- name: tmp
  mountPath: /tmp
- name: data
  mountPath: /data
- name: objects
  mountPath: /objects
{{- if .Values.secrets.storeClaim }}
- name: secretstore
  mountPath: /var/lib/agentruntime/secretstore
{{- end }}
{{- end -}}
