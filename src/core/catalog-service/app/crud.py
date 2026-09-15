"""Generic CRUD used by the datasets/pipelines/models routers — those three
are structurally identical (name, visibility, description, plus one extra
field each), so this is one implementation instead of three near-copies.
functions.py doesn't use this — publish/promote need function-specific
logic that isn't worth forcing through a generic shape.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.visibility import Principal, can_create, can_write, read_filter


def list_visible(db: Session, model, principal: Principal):
    return db.query(model).filter(read_filter(principal, model)).order_by(model.created_at.desc()).all()


def get_visible_or_404(db: Session, model, entity_id: uuid.UUID, principal: Principal):
    entity = db.get(model, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found.")
    from app.visibility import can_read  # local import avoids a cycle at module load time

    if not can_read(principal, entity):
        # 404, not 403 — don't confirm existence of something the requester
        # can't see at all (private entries in other workspaces).
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found.")
    return entity


def create(db: Session, model, data: dict, principal: Principal):
    if not can_create(principal):
        raise HTTPException(status_code=403, detail="Viewers cannot create entries.")
    entity = model(**data, workspace_id=principal.workspace_id, created_by=principal.user_id)
    db.add(entity)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — surfaced as 409, likely the (workspace_id, name) unique constraint
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Could not create {model.__name__}: {exc}") from exc
    db.refresh(entity)
    return entity


def update(db: Session, model, entity_id: uuid.UUID, data: dict, principal: Principal):
    entity = get_visible_or_404(db, model, entity_id, principal)
    if not can_write(principal, entity):
        raise HTTPException(status_code=403, detail="Not writable by this workspace.")
    for key, value in data.items():
        if value is not None:
            setattr(entity, key, value)
    db.commit()
    db.refresh(entity)
    return entity


def delete(db: Session, model, entity_id: uuid.UUID, principal: Principal) -> None:
    entity = get_visible_or_404(db, model, entity_id, principal)
    if not can_write(principal, entity):
        raise HTTPException(status_code=403, detail="Not writable by this workspace.")
    db.delete(entity)
    db.commit()
