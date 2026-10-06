"""FastAPI application exposing repository analysis and metric queries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from rat.config import Settings
from rat.db import Database
from rat.errors import RatError, RepositoryNotFoundError
from rat.export import metrics_to_csv
from rat.metrics import MetricsEngine
from rat.models import CommitSet, ObjectType
from rat.service import AnalysisService
from rat.storage import RepositoryStore


class LocalAnalysisRequest(BaseModel):
    path: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    ref: str = Field(default="HEAD", min_length=1, max_length=250)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    database = Database(
        resolved_settings.database_path,
        busy_timeout_ms=resolved_settings.sqlite_busy_timeout_ms,
    )
    store = RepositoryStore(database)
    metrics = MetricsEngine(database)
    analysis = AnalysisService(store, git_timeout_seconds=resolved_settings.git_timeout_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        resolved_settings.ensure_directories()
        database.initialize()
        yield

    app = FastAPI(
        title="Repo Analysis Tool API",
        version="0.1.0",
        description=(
            "High-performance Git history metrics for files, directories, authors, and repos."
        ),
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.store = store
    app.state.metrics = metrics
    app.state.analysis = analysis

    @app.exception_handler(RatError)
    async def rat_error_handler(_: Request, error: RatError) -> JSONResponse:
        status_code = 404 if isinstance(error, RepositoryNotFoundError) else 400
        return JSONResponse(status_code=status_code, content={"detail": str(error)})

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/repositories")
    def repositories() -> list[dict[str, object]]:
        return store.list_repositories()

    @app.get("/api/repositories/{repository_id}")
    def repository(repository_id: int) -> dict[str, object]:
        return store.get_repository(repository_id)

    @app.post("/api/repositories/local", status_code=201)
    def analyse_local(request: LocalAnalysisRequest) -> dict[str, object]:
        return analysis.analyse_local(Path(request.path), name=request.name, ref=request.ref)

    @app.get("/api/repositories/{repository_id}/metrics")
    def repository_metrics(
        repository_id: int,
        since: int | None = None,
        until: int | None = None,
        commits: Annotated[list[str] | None, Query()] = None,
        object_type: ObjectType | None = None,
        path: str | None = None,
        author: str | None = None,
    ) -> list[dict[str, object]]:
        rows = metrics.calculate(
            repository_id,
            commit_set=CommitSet(since=since, until=until, hashes=tuple(commits or ())),
            object_type=object_type,
            path=path,
            author=author,
        )
        return [asdict(row) for row in rows]

    @app.get("/api/repositories/{repository_id}/metrics.csv")
    def repository_metrics_csv(
        repository_id: int,
        since: int | None = None,
        until: int | None = None,
        commits: Annotated[list[str] | None, Query()] = None,
        object_type: ObjectType | None = None,
        path: str | None = None,
        author: str | None = None,
    ) -> Response:
        rows = metrics.calculate(
            repository_id,
            commit_set=CommitSet(since=since, until=until, hashes=tuple(commits or ())),
            object_type=object_type,
            path=path,
            author=author,
        )
        return Response(
            metrics_to_csv(rows),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="repository-{repository_id}.csv"'
            },
        )

    return app
