"""Route prefix is /models (matches ARCHITECTURE.md's vocabulary); module is
named ml_models.py, not models.py, so it doesn't collide with app/models.py
(the ORM module) on import.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_principal
from app.models import MLModel
from app.schemas import MLModelCreate, MLModelRead, MLModelUpdate
from app.visibility import Principal

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[MLModelRead])
def list_models(db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    return crud.list_visible(db, MLModel, principal)


@router.post("", response_model=MLModelRead, status_code=201)
def create_model(
    body: MLModelCreate, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)
):
    return crud.create(db, MLModel, body.model_dump(), principal)


@router.get("/{model_id}", response_model=MLModelRead)
def get_model(
    model_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.get_visible_or_404(db, MLModel, model_id, principal)


@router.patch("/{model_id}", response_model=MLModelRead)
def update_model(
    model_id: uuid.UUID,
    body: MLModelUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.update(db, MLModel, model_id, body.model_dump(exclude_unset=True), principal)


@router.delete("/{model_id}", status_code=204)
def delete_model(
    model_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    crud.delete(db, MLModel, model_id, principal)
