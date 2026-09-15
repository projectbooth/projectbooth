"""Deliberately minimal: list + create + get. Membership, roles
(owner/editor/viewer — ARCHITECTURE.md §4), and invites are Keycloak-group
territory (`platform workspace invite`, not built yet) — this table mirrors
workspace *existence* for the catalog's own FK, it isn't where membership
gets decided. Once a role IS resolved (per-request, via app/deps.py), what
it's allowed to do is app/visibility.py's Role/can_write/can_create — not
this file, and not another column here.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_principal
from app.models import Workspace
from app.schemas import WorkspaceCreate, WorkspaceRead
from app.visibility import Principal

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(db: Session = Depends(get_db)):
    # Unlike the four entity types, workspace existence itself isn't
    # visibility-gated — knowing a workspace named "personal" exists isn't
    # sensitive the way its private datasets are.
    return db.query(Workspace).order_by(Workspace.name).all()


@router.post("", response_model=WorkspaceRead, status_code=201)
def create_workspace(body: WorkspaceCreate, db: Session = Depends(get_db)):
    workspace = Workspace(**body.model_dump())
    db.add(workspace)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Could not create workspace: {exc}") from exc
    db.refresh(workspace)
    return workspace


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(workspace_id: uuid.UUID, db: Session = Depends(get_db)):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return workspace


@router.get("/me", response_model=WorkspaceRead)
def get_my_workspace(principal: Principal = Depends(get_current_principal), db: Session = Depends(get_db)):
    """Convenience for platform-cli/platform-sdk: "what workspace am I
    running as, given my current headers" without needing to know the id."""
    return db.get(Workspace, principal.workspace_id)
