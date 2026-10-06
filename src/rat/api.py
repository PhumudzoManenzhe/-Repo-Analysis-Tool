"""FastAPI application exposing repository analysis and metric queries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from rat.config import Settings
from rat.db import Database
from rat.errors import JobNotFoundError, RatError, RepositoryNotFoundError
from rat.export import metrics_to_csv
from rat.ingestion import RemoteRepositoryCloner, SecureZipExtractor
from rat.ingestion_service import RepositoryIngestionService
from rat.jobs import BackgroundJobManager, JobStore
from rat.metrics import MetricsEngine
from rat.models import CommitSet, ObjectType
from rat.service import AnalysisService
from rat.storage import RepositoryStore


class LocalAnalysisRequest(BaseModel):
    path: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    ref: str = Field(default="HEAD", min_length=1, max_length=250)


class RemoteCloneRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2_000)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    ref: str = Field(default="HEAD", min_length=1, max_length=250)


class AuthorMergeRequest(BaseModel):
    source_author_id: int = Field(gt=0)
    canonical_author_id: int = Field(gt=0)


async def _persist_upload(upload: UploadFile, directory: Path, max_bytes: int) -> Path:
    target = directory / f"{uuid4().hex}.zip"
    size = 0
    try:
        with target.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"ZIP upload exceeds the {max_bytes} byte limit",
                    )
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The uploaded ZIP file is empty")
    return target


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    database = Database(
        resolved_settings.database_path,
        busy_timeout_ms=resolved_settings.sqlite_busy_timeout_ms,
    )
    store = RepositoryStore(database)
    metrics = MetricsEngine(database)
    analysis = AnalysisService(
        store,
        git_timeout_seconds=resolved_settings.git_timeout_seconds,
        batch_size=resolved_settings.analysis_batch_size,
    )
    job_store = JobStore(database)
    job_manager = BackgroundJobManager(
        job_store,
        store,
        max_workers=resolved_settings.background_workers,
    )
    ingestion = RepositoryIngestionService(
        store,
        analysis,
        job_manager,
        SecureZipExtractor(
            max_extracted_bytes=resolved_settings.max_extracted_bytes,
            max_members=resolved_settings.max_archive_members,
            max_compression_ratio=resolved_settings.max_compression_ratio,
        ),
        RemoteRepositoryCloner(timeout_seconds=resolved_settings.git_timeout_seconds),
        resolved_settings.repositories_dir,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        resolved_settings.ensure_directories()
        database.initialize()
        job_store.recover_interrupted()
        try:
            yield
        finally:
            job_manager.shutdown()

    app = FastAPI(
        title="Repo Analysis Tool API",
        version="0.1.0",
        description=(
            "High-performance Git history metrics for files, directories, authors, and repos."
        ),
        lifespan=lifespan,
    )
    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.state.settings = resolved_settings
    app.state.database = database
    app.state.store = store
    app.state.metrics = metrics
    app.state.analysis = analysis
    app.state.job_store = job_store
    app.state.job_manager = job_manager
    app.state.ingestion = ingestion

    @app.exception_handler(RatError)
    async def rat_error_handler(_: Request, error: RatError) -> JSONResponse:
        not_found_errors = (RepositoryNotFoundError, JobNotFoundError)
        status_code = 404 if isinstance(error, not_found_errors) else 400
        return JSONResponse(status_code=status_code, content={"detail": str(error)})

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/repositories")
    def repositories() -> list[dict[str, object]]:
        return store.list_repositories()

    @app.get("/api/repositories/{repository_id}")
    def repository(repository_id: int) -> dict[str, object]:
        return store.get_repository(repository_id)

    @app.get("/api/repositories/{repository_id}/authors")
    def repository_authors(repository_id: int) -> list[dict[str, object]]:
        return store.list_authors(repository_id)

    @app.post("/api/repositories/{repository_id}/author-merges")
    def merge_repository_authors(
        repository_id: int,
        request: AuthorMergeRequest,
    ) -> list[dict[str, object]]:
        return store.merge_authors(
            repository_id,
            request.source_author_id,
            request.canonical_author_id,
        )

    @app.delete("/api/repositories/{repository_id}/author-merges/{source_author_id}")
    def unmerge_repository_author(
        repository_id: int,
        source_author_id: int,
    ) -> list[dict[str, object]]:
        return store.unmerge_author(repository_id, source_author_id)

    @app.post("/api/repositories/local", status_code=201)
    def analyse_local(request: LocalAnalysisRequest) -> dict[str, object]:
        return analysis.analyse_local(Path(request.path), name=request.name, ref=request.ref)

    @app.post("/api/repositories/zip", status_code=status.HTTP_202_ACCEPTED)
    async def analyse_zip(
        file: Annotated[UploadFile, File()],
        name: Annotated[str | None, Form(max_length=200)] = None,
        ref: Annotated[str, Form(min_length=1, max_length=250)] = "HEAD",
    ) -> dict[str, object]:
        filename = Path((file.filename or "repository.zip").replace("\\", "/")).name
        try:
            archive_path = await _persist_upload(
                file,
                resolved_settings.data_dir / "uploads",
                resolved_settings.max_upload_bytes,
            )
        finally:
            await file.close()
        return ingestion.submit_zip(
            archive_path,
            filename=filename,
            name=name,
            ref=ref,
        )

    @app.post("/api/repositories/clone", status_code=status.HTTP_202_ACCEPTED)
    def analyse_remote(request: RemoteCloneRequest) -> dict[str, object]:
        return ingestion.submit_remote(
            request.url,
            name=request.name,
            ref=request.ref,
        )

    @app.get("/api/jobs/{job_id}")
    def ingestion_job(job_id: str) -> dict[str, object]:
        return job_store.get(job_id)

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
