{{/*
Expand the name of the chart.
*/}}
{{- define "odoocluster.name" -}}
{{- default .Release.Name .Values.name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "odoocluster.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "odoocluster.labels" -}}
helm.sh/chart: {{ include "odoocluster.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
simstech-odoo/cluster: {{ include "odoocluster.name" . }}
{{- end }}

{{/*
Namespace for the cluster (from helm release)
*/}}
{{- define "odoocluster.namespace" -}}
{{ .Release.Namespace }}
{{- end }}

{{/*
Odoo Tailscale hostname
*/}}
{{- define "odoocluster.odooHostname" -}}
{{- if .Values.networking.tailscale.odoo.hostname }}
{{- .Values.networking.tailscale.odoo.hostname }}
{{- else }}
{{- include "odoocluster.name" . }}
{{- end }}
{{- end }}

{{/*
BI Tailscale hostname
*/}}
{{- define "odoocluster.biHostname" -}}
{{- if .Values.networking.tailscale.bi.hostname }}
{{- .Values.networking.tailscale.bi.hostname }}
{{- else }}
{{- printf "%s-bi" (include "odoocluster.name" .) }}
{{- end }}
{{- end }}

