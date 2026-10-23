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
{{- printf "%s/%s:%s" $root.Values.image.registry (index . 1) (default $root.Chart.AppVersion $root.Values.image.tag) -}}
{{- end -}}

{{- define "ar.collectorService" -}}
{{- printf "%s-otel-collector" (include "ar.fullname" .) -}}
{{- end -}}

{{- define "ar.modelGatewayService" -}}
{{- printf "%s-model-gateway" (include "ar.fullname" .) -}}
{{- end -}}

{{- define "ar.registryService" -}}
{{- printf "%s-registry" (include "ar.fullname" .) -}}
{{- end -}}

{{/* 各服务共用的环境变量：Telemetry（T-15）与 SecretStore 的文件。 */}}
{{- define "ar.commonEnv" -}}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ printf "http://%s:4318" (include "ar.collectorService" .) | quote }}
- name: OTEL_SERVICE_VERSION
  value: {{ default .Chart.AppVersion .Values.image.tag | quote }}
- name: OTEL_DEPLOYMENT_ENVIRONMENT
  value: {{ .Values.global.environment | quote }}
- name: AR_SECRETS_FILE
  value: {{ printf "/var/run/agentruntime/secrets/%s" .Values.secrets.secretsFileKey | quote }}
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
{{- end -}}
