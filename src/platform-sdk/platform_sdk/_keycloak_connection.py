"""Shared low-level Keycloak connection plumbing — a single function,
`extract_platform_ca_cert()`, used by both `KeycloakAdminClient` and
`KeycloakLoginFlow` to build a CA-pinned `httpx.Client` pointed straight at
Keycloak's real, resolvable hostname.

Until 2026-09-02 this module also held `_PortForward` and `_ResolvePatch` —
a background `kubectl port-forward` plus a process-wide `socket.getaddrinfo`
patch reproducing curl's `--resolve HOST:PORT:IP` trick, both built because
Keycloak's `hostname-strict` enforcement gave a bare port-forward to
`localhost` no way to satisfy `spec.hostname.hostname:
keycloak.platform.local`. The platform-ingress branch removed the reason
for either to exist: `keycloak.platform.local` (and `gateway.platform.local`)
now resolve for real, off-cluster, via a real `Ingress`
(`src/core/argocd/manifests/keycloak-instance.yaml`) plus a per-device
`/etc/hosts` entry — the same way any other website's hostname resolves —
so a caller just connects to `https://{host}` directly. Nothing here needs
to fake a resolution or tunnel a raw TCP connection anymore.

What's left — reading `platform-ca-secret`'s `ca.crt` via `kubectl` so that
connection can be verified against this cluster's actual CA (self-signed,
not in any public trust store) — has nothing to do with port-forwarding and
never did; it's just the one piece of the old `_PortForward` class that
still earns its keep.
"""
from __future__ import annotations

import base64
import subprocess
import tempfile
from pathlib import Path

from platform_sdk.exceptions import KeycloakAdminError


def extract_platform_ca_cert(kubectl_cmd: str) -> str:
    """Reads `platform-ca-secret`'s `ca.crt` out of the cluster (the same
    Secret `bootstrap/keycloak-bootstrap-cli-client.sh` reads — see that
    script's header comment for where it comes from: cert-manager's
    `platform-ca` `ClusterIssuer`) and writes it to a throwaway temp file,
    returning that file's path — the shape `httpx.Client(verify=...)`
    expects. Caller owns cleanup (the returned path isn't tracked here);
    `KeycloakAdminClient`/`KeycloakLoginFlow` unlink it in their own
    `__exit__`.
    """
    try:
        result = subprocess.run(
            [
                *kubectl_cmd.split(),
                "get",
                "secret",
                "platform-ca-secret",
                "-n",
                "cert-manager",
                "-o",
                r"jsonpath={.data.ca\.crt}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise KeycloakAdminError(
            f"Couldn't read platform-ca-secret's ca.crt via kubectl: {exc.stderr.strip()}"
        ) from exc
    ca_bytes = base64.b64decode(result.stdout)
    if not ca_bytes:
        raise KeycloakAdminError(
            "platform-ca-secret's ca.crt came back empty — check it exists: "
            "kubectl get secret platform-ca-secret -n cert-manager -o yaml"
        )
    fd = tempfile.NamedTemporaryFile(prefix="platform-ca-", suffix=".crt", delete=False)
    fd.write(ca_bytes)
    fd.close()
    return fd.name


def cleanup_ca_cert(ca_cert_path: str | Path | None) -> None:
    """Unlinks a path `extract_platform_ca_cert()` returned. Swallows
    "already gone" the same way the old `_PortForward.stop()` did — cleanup
    running twice, or against a path that was never created, shouldn't be
    an error.
    """
    if ca_cert_path is None:
        return
    try:
        Path(ca_cert_path).unlink()
    except OSError:
        pass
