from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_principal
from app.models import Dataset
from app.schemas import DatasetCreate, DatasetRead, DatasetUpdate
from app.visibility import Principal

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetRead])
def list_datasets(db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    return crud.list_visible(db, Dataset, principal)


@router.post("", response_model=DatasetRead, status_code=201)
def create_dataset(
    body: DatasetCreate, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)
):
    return crud.create(db, Dataset, body.model_dump(), principal)


@router.get("/{dataset_id}", response_model=DatasetRead)
def get_dataset(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.get_visible_or_404(db, Dataset, dataset_id, principal)


@router.patch("/{dataset_id}", response_model=DatasetRead)
def update_dataset(
    dataset_id: uuid.UUID,
    body: DatasetUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.update(db, Dataset, dataset_id, body.model_dump(exclude_unset=True), principal)


@router.delete("/{dataset_id}", status_code=204)
def delete_dataset(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    crud.delete(db, Dataset, dataset_id, principal)
