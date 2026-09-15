"""Deliberately minimal, per this file's own models.py note: record edges
and list them back out, filtered by workspace. No graph traversal (e.g.
"show me everything three hops upstream of this dataset") yet — that's a
real feature, just not one anything in this repo needs before something
actually populates edges (platform-sdk's decorators, once they exist).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_principal
from app.models import EntityKind, LineageEdge, LineageRelation
from app.schemas import LineageEdgeCreate, LineageEdgeRead
from app.visibility import Principal, can_create

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.get("", response_model=list[LineageEdgeRead])
def list_lineage(
    source_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Filtered to edges owned by the caller's workspace — lineage isn't
    given its own visibility flag (ARCHITECTURE.md doesn't call for one);
    it inherits scope from the workspace that recorded it. Revisit if edges
    ever need to be independently public/private of their endpoints."""
    query = db.query(LineageEdge).filter(LineageEdge.workspace_id == principal.workspace_id)
    if source_id is not None:
        query = query.filter(LineageEdge.source_id == source_id)
    if target_id is not None:
        query = query.filter(LineageEdge.target_id == target_id)
    return query.order_by(LineageEdge.created_at.desc()).all()


@router.post("", response_model=LineageEdgeRead, status_code=201)
def create_lineage_edge(
    body: LineageEdgeCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    if not can_create(principal):
        raise HTTPException(status_code=403, detail="Viewers cannot create lineage edges.")
    try:
        source_kind = EntityKind(body.source_kind)
        target_kind = EntityKind(body.target_kind)
        relation = LineageRelation(body.relation)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    edge = LineageEdge(
        workspace_id=principal.workspace_id,
        source_kind=source_kind,
        source_id=body.source_id,
        target_kind=target_kind,
        target_id=body.target_id,
        relation=relation,
    )
    db.add(edge)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — likely the uq_lineage_edges_edge constraint
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Could not create lineage edge: {exc}") from exc
    db.refresh(edge)
    return edge
