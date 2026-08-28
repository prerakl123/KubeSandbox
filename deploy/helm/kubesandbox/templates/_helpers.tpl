{{/*
Shared naming, labels, and — the important part — the env-var block that both the API and
the reconciler Deployments consume. Those two processes must see *identical*
configuration: they share one Postgres, one provisioner backend, and one component
registry, and a setting that differs between them (a different pool size, a different
provisioner) produces a control plane that fights itself. Defining the env once here is
what makes that structural rather than a thing to remember.
*/}}

{{- define "kubesandbox.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kubesandbox.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "kubesandbox.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kubesandbox.labels" -}}
helm.sh/chart: {{ include "kubesandbox.chart" . }}
{{ include "kubesandbox.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "kubesandbox.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubesandbox.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "kubesandbox.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "kubesandbox.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The name of the Secret carrying DSNs and the JWT signing key. Either a caller-supplied
existing Secret, or the one the Key Vault CSI driver syncs.
*/}}
{{- define "kubesandbox.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "kubesandbox.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Non-secret configuration, as KUBESANDBOX_* env vars. `__` is the nesting delimiter
app/core/config.py's pydantic-settings uses, and env vars sit above the YAML profile in
precedence — so anything set here overrides config/settings/aks-prod.yaml.

List-valued settings (cors.allowOrigins) are emitted as JSON, which is how
pydantic-settings parses a complex type from a single env var.
*/}}
{{- define "kubesandbox.env" -}}
- name: KUBESANDBOX_APP_ENV
  value: {{ .Values.config.appEnv | quote }}
- name: KUBESANDBOX_CORS__ENABLED
  value: {{ .Values.config.cors.enabled | quote }}
{{- if .Values.config.cors.enabled }}
- name: KUBESANDBOX_CORS__ALLOW_ORIGINS
  value: {{ toJson .Values.config.cors.allowOrigins | quote }}
{{- end }}
{{- with .Values.config.auth.oidcIssuer }}
- name: KUBESANDBOX_AUTH__OIDC_ISSUER
  value: {{ . | quote }}
{{- end }}
{{- with .Values.config.auth.oidcAudience }}
- name: KUBESANDBOX_AUTH__OIDC_AUDIENCE
  value: {{ . | quote }}
{{- end }}
{{- with .Values.config.auth.oidcClientId }}
- name: KUBESANDBOX_AUTH__OIDC_CLIENT_ID
  value: {{ . | quote }}
{{- end }}
- name: KUBESANDBOX_AUTH__SESSION_TTL_SECONDS
  value: {{ .Values.config.auth.sessionTtlSeconds | quote }}
- name: KUBESANDBOX_OBSERVABILITY__METRICS_ENABLED
  value: {{ .Values.config.observability.metricsEnabled | quote }}
- name: KUBESANDBOX_OBSERVABILITY__TRACING_ENABLED
  value: {{ .Values.config.observability.tracingEnabled | quote }}
{{- if .Values.config.observability.tracingEnabled }}
- name: KUBESANDBOX_OBSERVABILITY__OTLP_ENDPOINT
  value: {{ required "observability.otlpEndpoint is required when tracingEnabled (Settings refuses the pair otherwise)" .Values.config.observability.otlpEndpoint | quote }}
{{- end }}
- name: KUBESANDBOX_OBSERVABILITY__TRACE_SAMPLE_RATIO
  value: {{ .Values.config.observability.traceSampleRatio | quote }}
- name: KUBESANDBOX_POOL__ENABLED
  value: {{ .Values.config.pool.enabled | quote }}
- name: KUBESANDBOX_WORKSPACE__PERSISTENCE_ENABLED
  value: {{ .Values.config.workspace.persistenceEnabled | quote }}
- name: KUBESANDBOX_BILLING__ENABLED
  value: {{ .Values.config.billing.enabled | quote }}
{{- range $key, $value := .Values.config.extraSettings | default dict }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end -}}

{{/*
Secret-sourced env. Every key is marked optional so a release using the Key Vault CSI
path (where the synced Secret only appears once a pod mounts the volume) doesn't
deadlock: the pod would otherwise refuse to start waiting for a Secret whose creation
depends on that same pod starting.
*/}}
{{- define "kubesandbox.secretEnv" -}}
- name: KUBESANDBOX_DATABASE__DSN
  valueFrom:
    secretKeyRef:
      name: {{ include "kubesandbox.secretName" . }}
      key: KUBESANDBOX_DATABASE__DSN
      optional: true
- name: KUBESANDBOX_REDIS__URL
  valueFrom:
    secretKeyRef:
      name: {{ include "kubesandbox.secretName" . }}
      key: KUBESANDBOX_REDIS__URL
      optional: true
- name: KUBESANDBOX_AUTH__JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "kubesandbox.secretName" . }}
      key: KUBESANDBOX_AUTH__JWT_SECRET
      optional: true
{{- end -}}

{{/*
Writable scratch volumes. `readOnlyRootFilesystem: true` (doc §6 Layer 1, applied to the
control plane itself) means anything that writes needs an explicit emptyDir — and both
processes do: `/tmp` for tarball staging during file upload/download and build contexts,
and uv's cache directory.
*/}}
{{- define "kubesandbox.scratchVolumes" -}}
- name: tmp
  emptyDir: {}
- name: uv-cache
  emptyDir: {}
{{- end -}}

{{- define "kubesandbox.scratchVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: uv-cache
  mountPath: /home/kubesandbox/.cache/uv
{{- end -}}
