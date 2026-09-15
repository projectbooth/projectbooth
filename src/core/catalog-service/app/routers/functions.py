"""Functions get two extra endpoints beyond the generic CRUD shape:

POST /functions/{id}/publish — ARCHITECTURE.md §4: "A function decorated
with @platform.function(...) registers into catalog-lite with an owner
workspace, a version (bumped on each `platform-cli publish`), an extracted
signature/docstring, and lineage." This is that bump: it writes a new
FunctionVersion row and advances Function.current_version to match. The
extraction itself (reading the decorated function's real signature/docstring
out of the source) is platform-sdk's job, not this service's — this endpoint
just records what it's given.

POST /functions/{id}/promote — the one-line "make this public" ARCHITECTURE
calls out explicitly ("`platform-cli function promote <name> --public`").
Separate from PATCH because promoting is a workspace-membership action with
its own meaning (crossing the private-instance visibility boundary), not a
generic field edit.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_principal
from app.models import Function, FunctionVersion, Visibility
from app.schemas import FunctionCreate, FunctionPublish, FunctionRead, FunctionUpdate, FunctionVersionRead
from app.visibility import Principal, can_write

router = APIRouter(prefix="/functions", tags=["functions"])


@router.get("", response_model=list[FunctionRead])
def list_functions(db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    return crud.list_visible(db, Function, principal)


@router.post("", response_model=FunctionRead, status_code=201)
def create_function(
    body: FunctionCreate, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)
):
    return crud.create(db, Function, body.model_dump(), principal)


@router.get("/{function_id}", response_model=FunctionRead)
def get_function(
    function_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.get_visible_or_404(db, Function, function_id, principal)


@router.patch("/{function_id}", response_model=FunctionRead)
def update_function(
    function_id: uuid.UUID,
    body: FunctionUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    return crud.update(db, Function, function_id, body.model_dump(exclude_unset=True), principal)


@router.delete("/{function_id}", status_code=204)
def delete_function(
    function_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    crud.delete(db, Function, function_id, principal)


@router.get("/{function_id}/versions", response_model=list[FunctionVersionRead])
def list_function_versions(
    function_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    crud.get_visible_or_404(db, Function, function_id, principal)  # 404s if not visible at all
    return (
        db.query(FunctionVersion)
        .filter(FunctionVersion.function_id == function_id)
        .order_by(FunctionVersion.version.desc())
        .all()
    )


@router.post("/{function_id}/publish", response_model=FunctionVersionRead, status_code=201)
def publish_function(
    function_id: uuid.UUID,
    body: FunctionPublish,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    function = crud.get_visible_or_404(db, Function, function_id, principal)
    if not can_write(principal, function):
        raise HTTPException(status_code=403, detail="Not writable by this workspace.")

    next_version = function.current_version + 1
    version_row = FunctionVersion(
        function_id=function.id,
        version=next_version,
        signature=body.signature,
        docstring=body.docstring,
        module_path=body.module_path,
        published_by=body.published_by or principal.user_id,
    )
    function.current_version = next_version
    function.module_path = body.module_path
    db.add(version_row)
    db.commit()
    db.refresh(version_row)
    return version_row


@router.post("/{function_id}/promote", response_model=FunctionRead)
def promote_function(
    function_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
):
    """Sets visibility to `public`. One-directional on purpose — demoting
    back is a plain PATCH {"visibility": "workspace"}, same as any other
    field edit; only the "make it public" action gets its own named verb,
    matching the CLI command ARCHITECTURE.md names."""
    function = crud.get_visible_or_404(db, Function, function_id, principal)
    if not can_write(principal, function):
        raise HTTPException(status_code=403, detail="Not writable by this workspace.")
    function.visibility = Visibility.PUBLIC
    db.commit()
    db.refresh(function)
    return function
