{{/*
Shared helpers every module chart gets from `platform-cli module scaffold`. ARCHITECTURE.md §7's
chart-wrapper node-placement mechanism lives here: platform.nodeAffinity/platform.tolerations
render the actual affinity/tolerations block from .Values.placement, which
`platform module install` computes from this module's own module.yaml (see manifest.py's
render_application_manifest). Callers guard these with an `if` at the call site (see
templates/deployment.yaml) rather than inside the define itself — an unguarded `if` inside a
`define` that renders to nothing still leaves stray blank lines behind after `nindent`, which is
exactly the kind of subtle whitespace bug worth avoiding by keeping the guard outside.

platform.nodeAffinity renders a PREFERRED (soft) nodeAffinity, not a hard nodeSelector — see
ARCHITECTURE.md §12's "Single-node placement fallback" decision. A hard nodeSelector requires an
exact-matching label to exist on some node or the pod stays Pending forever (hit for real,
2026-09-09: hello-module on a single-node homelab cluster whose only node is labeled `control`,
not `compute` — §7's join-time label can only ever hold one value). A soft preference asks the
scheduler to prefer a role-labeled node when one exists — the real placement behavior a multi-node
cluster still gets — but lets the pod land anywhere when nothing matches, which is exactly what a
single-node cluster needs. Tolerations are unchanged and still required: they're what lets the pod
land on an actual NoSchedule-tainted compute node in a real multi-node cluster — a soft affinity
preference doesn't by itself get past a taint.
*/}}

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
