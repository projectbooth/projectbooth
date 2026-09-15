from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_principal
from app.models import Pipeline
from app.schemas import PipelineCreate, PipelineRead, PipelineUpdate
from app.visibility import Principal

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


@router.get("", response_model=list[PipelineRead])
def list_pipelines(db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    return crud.list_visible(db, Pipeline, principal)


@router.post("", response_model=PipelineRead, status_code=201)
def create_pipeline(
    body: PipelineCreate, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)
):
    return crud.create(db, Pipeline, body.model_dump(), principal)


@router.get("/{pipeline_id}", response_model=PipelineRead)
def get_pipeline(
    pipeline_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.get_visible_or_404(db, Pipeline, pipeline_id, principal)


@router.patch("/{pipeline_id}", response_model=PipelineRead)
def update_pipeline(
    pipeline_id: uuid.UUID,
    body: PipelineUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.update(db, Pipeline, pipeline_id, body.model_dump(exclude_unset=True), principal)


@router.delete("/{pipeline_id}", status_code=204)
def delete_pipeline(
    pipeline_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    crud.delete(db, Pipeline, pipeline_id, principal)
