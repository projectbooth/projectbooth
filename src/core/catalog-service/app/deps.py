"""FastAPI dependencies — currently just one.

get_current_principal reads three plain headers rather than verifying a
Keycloak JWT itself — and, as of platform-gateway-auth (2026-09-02), that's
no longer a placeholder to feel uneasy about: it's the correct, permanent
shape. platform-gateway now sits in front of this service, verifies every
caller's Keycloak JWT itself (JWKS-checked signature, issuer, expiry — see
src/core/gateway/app/auth.py), and forwards exactly the three headers this
function already expected — X-Workspace, X-User, X-Role — but now
GATEWAY-DERIVED rather than client-declared: X-User/X-Role come straight off
the verified token's claims (never anything a caller sent), and X-Workspace,
while still a client-supplied hint, has already been checked by gateway
against that same token's `groups` claim before it ever reaches here (no
match → gateway itself 403s, this function never sees the request). Nothing
in visibility.py, the routers, or this function needed to change for real
auth to land — that was the point of designing it this way from the start:
only the identity of whatever sits in front of catalog-service changed, not
this trust boundary's shape.

X-Role's mapping still mirrors src/core/auth/realm-platform.yaml's Keycloak
groups directly: those groups encode workspace + role together as one path
(/workspaces/<name>/<role>), which is exactly the claim gateway reads to
derive X-Role — one definition of what a role means, expressed once in
Keycloak, not reimplemented here.

What this means in practice: catalog-service's own trust boundary is now
"whatever reaches me on my ClusterIP already went through gateway's
verification" — true for any request coming from `platform-cli` (which only
ever talks to gateway; see platform_sdk/client.py), but NOT yet true at the
network layer. Nothing in this cluster currently stops another in-cluster
pod from reaching catalog-service's Service directly and forging these same
three headers — k3s's bundled NetworkPolicy controller is enabled by
default and WOULD enforce a policy restricting ingress to gateway's
namespace only, but no such policy has been written yet. That's the one
gap this branch doesn't close; see docs/known-issues.md for the tracked
follow-up. DEFAULT_ROLE below (Role.OWNER when X-Role is absent) matters
more now than it used to for exactly this reason — see its own comment.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Workspace
from app.visibility import Principal, Role

DEFAULT_WORKSPACE = "personal"
DEFAULT_USER = "anonymous"
# Owner, not viewer — matches DEFAULT_WORKSPACE/DEFAULT_USER's existing
# "frictionless by default" choice, and still needed for local dev / this
# service's own test suite (curl by hand, tests/test_visibility.py,
# tests/test_datasets_api.py — none of which sit behind gateway).
#
# Sharper edge now that platform-gateway-auth (2026-09-02) is real, worth
# restating plainly: gateway ALWAYS sends an explicit X-Role for every
# request it forwards (derived from the caller's verified token — see this
# module's docstring) and must never omit it. If gateway ever did omit it,
# this default would silently grant that request Owner — a real
# privilege-escalation path, not a hypothetical one, since Owner is the
# most-privileged role there is. Nothing here can enforce "gateway always
# sends X-Role" from catalog-service's side; that invariant lives in
# gateway's own code (src/core/gateway/app/auth.py's derive_headers) and is
# exactly why that function is designed to raise rather than return a
# request with role omitted.
DEFAULT_ROLE = Role.OWNER


def get_current_principal(
    x_workspace: str | None = Header(default=None, alias="X-Workspace"),
    x_user: str | None = Header(default=None, alias="X-User"),
    x_role: str | None = Header(default=None, alias="X-Role"),
    db: Session = Depends(get_db),
) -> Principal:
    workspace_name = x_workspace or DEFAULT_WORKSPACE
    workspace = db.query(Workspace).filter(Workspace.name == workspace_name).one_or_none()
    if workspace is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown workspace '{workspace_name}' (X-Workspace header, or the "
            f"'{DEFAULT_WORKSPACE}' default) — it needs a row in the workspaces table first.",
        )
    if x_role is None:
        role = DEFAULT_ROLE
    else:
        try:
            role = Role(x_role.lower())
        except ValueError as exc:
            valid = ", ".join(r.value for r in Role)
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role '{x_role}' (X-Role header) — must be one of: {valid}.",
            ) from exc
    return Principal(
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        user_id=x_user or DEFAULT_USER,
        role=role,
    )
