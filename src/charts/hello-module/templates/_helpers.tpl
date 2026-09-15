{{- define "platform.fullname" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "platform.nodeAffinity" -}}
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: platform.io/role
              operator: In
              values:
                - {{ .Values.placement.role }}
{{- end -}}

{{- define "platform.tolerations" -}}
tolerations:
{{ toYaml .Values.placement.tolerations | indent 2 }}
{{- end -}}
