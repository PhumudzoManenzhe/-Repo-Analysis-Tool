"""Persistent background job tracking with a bounded local worker pool."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from rat.db import Database
from rat.errors import JobNotFoundError, RatError
from rat.models import JobStatus, RepositoryStatus
from rat.storage import RepositoryStore

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int, str], None]
JobTask = Callable[[ProgressCallback], None]


class JobStore:
    """Persist job state so polling survives request and worker boundaries."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, repository_id: int, kind: str) -> dict[str, Any]:
        job_id = uuid4().hex
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ingestion_jobs(
                    id, repository_id, kind, status, stage, progress, message
                ) VALUES (?, ?, ?, ?, 'queued', 0, 'Waiting for a worker')
                """,
                (job_id, repository_id, kind, JobStatus.QUEUED),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Ingestion job {job_id} was not found")
        return dict(row)

    def mark_running(self, job_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = ?, stage = 'starting', progress = 1,
                    message = 'Starting ingestion', started_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.RUNNING, job_id),
            )

    def update_progress(self, job_id: str, stage: str, progress: int, message: str) -> None:
        bounded_progress = max(1, min(99, progress))
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET stage = ?,
                    progress = CASE WHEN progress < ? THEN ? ELSE progress END,
                    message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                (
                    stage,
                    bounded_progress,
                    bounded_progress,
                    message[:500],
                    job_id,
                    JobStatus.RUNNING,
                ),
            )

    def mark_succeeded(self, job_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = ?, stage = 'complete', progress = 100,
                    message = 'Repository analysis complete', error = NULL,
                    updated_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.SUCCEEDED, job_id),
            )

    def mark_failed(self, job_id: str, error: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = ?, stage = 'failed', message = 'Ingestion failed',
                    error = ?, updated_at = CURRENT_TIMESTAMP,
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (JobStatus.FAILED, error[:2000], job_id),
            )

    def recover_interrupted(self) -> int:
        """Mark work abandoned by a previous process as failed on startup."""
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT repository_id FROM ingestion_jobs
                WHERE status IN (?, ?)
                """,
                (JobStatus.QUEUED, JobStatus.RUNNING),
            ).fetchall()
            repository_ids = [int(row["repository_id"]) for row in rows]
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = ?, stage = 'failed', message = 'Ingestion interrupted',
                    error = 'The application stopped before this job completed',
                    updated_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP
                WHERE status IN (?, ?)
                """,
                (JobStatus.FAILED, JobStatus.QUEUED, JobStatus.RUNNING),
            )
            if repository_ids:
                placeholders = ",".join("?" for _ in repository_ids)
                connection.execute(
                    f"""
                    UPDATE repositories
                    SET status = ?, error = 'Ingestion interrupted'
                    WHERE id IN ({placeholders}) AND status IN (?, ?)
                    """,
                    (
                        RepositoryStatus.FAILED,
                        *repository_ids,
                        RepositoryStatus.PENDING,
                        RepositoryStatus.ANALYSING,
                    ),
                )
            return cursor.rowcount


class BackgroundJobManager:
    """Execute ingestion tasks outside request threads with bounded concurrency."""

    def __init__(
        self,
        jobs: JobStore,
        repositories: RepositoryStore,
        *,
        max_workers: int = 2,
    ) -> None:
        self.jobs = jobs
        self.repositories = repositories
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="rat-ingestion",
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()

    def submit(self, repository_id: int, kind: str, task: JobTask) -> dict[str, Any]:
        job = self.jobs.create(repository_id, kind)
        job_id = str(job["id"])
        future = self._executor.submit(self._run, job_id, repository_id, task)
        with self._lock:
            self._futures[job_id] = future
        future.add_done_callback(lambda _: self._forget(job_id))
        return job

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, job_id: str, repository_id: int, task: JobTask) -> None:
        self.jobs.mark_running(job_id)

        def progress(stage: str, percent: int, message: str) -> None:
            self.jobs.update_progress(job_id, stage, percent, message)

        try:
            task(progress)
        except Exception as error:
            logger.exception("Ingestion job %s failed", job_id)
            message = str(error) if isinstance(error, RatError) else "Unexpected ingestion failure"
            self.repositories.set_status(
                repository_id,
                RepositoryStatus.FAILED,
                error=message,
            )
            self.jobs.mark_failed(job_id, message)
        else:
            self.jobs.mark_succeeded(job_id)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)
