"""catalog-lite's FastAPI app — the "thin API" ARCHITECTURE.md §12 calls for
over the schema in app/models.py. Run locally with:

    uvicorn app.main:app --reload

against a Postgres reachable at DATABASE_URL (see .env.example) with
migrations applied (`alembic upgrade head`, see ../migrations/).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.deps import get_current_principal
from app.routers import datasets, functions, lineage, ml_models, pipelines, workspaces
from app.schemas import PrincipalRead
from app.visibility import Principal

app = FastAPI(
    title="catalog-lite",
    description="Datasets, functions, pipelines, models, workspaces — ARCHITECTURE.md §2, §4, §12.",
    version="0.1.0",
)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(workspaces.router)
app.include_router(datasets.router)
app.include_router(functions.router)
app.include_router(pipelines.router)
app.include_router(ml_models.router)
app.include_router(lineage.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/me", response_model=PrincipalRead)
def me(principal: Principal = Depends(get_current_principal)):
    """"Who am I, per my current X-Workspace/X-User/X-Role headers" — see
    app/deps.py's docstring for why those are placeholders, not verified
    identity. Top-level, not under /workspaces, since this describes the
    caller, not a workspace resource — see PrincipalRead's own docstring."""
    return PrincipalRead(
        workspace_id=principal.workspace_id,
        workspace_name=principal.workspace_name,
        user_id=principal.user_id,
        role=principal.role,
    )
