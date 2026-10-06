"""Background workflows for ZIP and remote repository ingestion."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from rat.ingestion import RemoteRepositoryCloner, SecureZipExtractor, validate_remote_url
from rat.jobs import BackgroundJobManager, ProgressCallback
from rat.service import AnalysisService
from rat.storage import RepositoryStore


class RepositoryIngestionService:
    """Create durable repository records and schedule their ingestion work."""

    def __init__(
        self,
        store: RepositoryStore,
        analysis: AnalysisService,
        jobs: BackgroundJobManager,
        extractor: SecureZipExtractor,
        cloner: RemoteRepositoryCloner,
        repositories_dir: Path,
    ) -> None:
        self.store = store
        self.analysis = analysis
        self.jobs = jobs
        self.extractor = extractor
        self.cloner = cloner
        self.repositories_dir = repositories_dir.resolve()

    def submit_zip(
        self,
        archive_path: Path,
        *,
        filename: str,
        name: str | None = None,
        ref: str = "HEAD",
    ) -> dict[str, Any]:
        destination = self._new_destination()
        repository_id = self.store.create_repository(
            name=name or self._name_from_filename(filename),
            source_type="zip",
            source_ref=filename,
            local_path=destination,
        )

        def ingest(progress: ProgressCallback) -> None:
            succeeded = False
            try:
                repository_path = self.extractor.extract(archive_path, destination, progress)
                self.store.set_local_path(repository_id, repository_path)
                self.analysis.analyse_repository(
                    repository_id,
                    repository_path,
                    ref=ref,
                    progress=progress,
                )
                succeeded = True
            finally:
                archive_path.unlink(missing_ok=True)
                if not succeeded:
                    self._remove_managed_directory(destination)

        try:
            return self.jobs.submit(repository_id, "zip", ingest)
        except Exception:
            archive_path.unlink(missing_ok=True)
            self._remove_managed_directory(destination)
            raise

    def submit_remote(
        self,
        url: str,
        *,
        name: str | None = None,
        ref: str = "HEAD",
    ) -> dict[str, Any]:
        normalized_url = validate_remote_url(url)
        destination = self._new_destination()
        repository_id = self.store.create_repository(
            name=name or self._name_from_url(normalized_url),
            source_type="clone",
            source_ref=normalized_url,
            local_path=destination,
        )

        def ingest(progress: ProgressCallback) -> None:
            succeeded = False
            try:
                self.cloner.clone(normalized_url, destination, progress)
                self.analysis.analyse_repository(
                    repository_id,
                    destination,
                    ref=ref,
                    progress=progress,
                )
                succeeded = True
            finally:
                if not succeeded:
                    self._remove_managed_directory(destination)

        return self.jobs.submit(repository_id, "clone", ingest)

    def _new_destination(self) -> Path:
        return self.repositories_dir / uuid4().hex

    def _remove_managed_directory(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved.parent != self.repositories_dir:
            raise RuntimeError("Refusing to remove a directory outside managed repository storage")
        if resolved.exists():
            shutil.rmtree(resolved)

    @staticmethod
    def _name_from_filename(filename: str) -> str:
        name = Path(filename).stem.strip()
        return name[:200] or "uploaded-repository"

    @staticmethod
    def _name_from_url(url: str) -> str:
        name = Path(url.rstrip("/")).name
        if name.endswith(".git"):
            name = name[:-4]
        return name[:200] or "remote-repository"
