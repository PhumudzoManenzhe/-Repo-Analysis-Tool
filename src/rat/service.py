"""Application services coordinating Git parsing and durable storage."""

from __future__ import annotations

from pathlib import Path

from rat.errors import RatError
from rat.git_parser import GitHistoryParser
from rat.models import RepositoryStatus
from rat.storage import RepositoryStore


class AnalysisService:
    """Run repository analysis as an atomic, observable workflow."""

    def __init__(self, store: RepositoryStore, *, git_timeout_seconds: int = 1800) -> None:
        self.store = store
        self.git_timeout_seconds = git_timeout_seconds

    def analyse_local(
        self,
        path: Path,
        *,
        name: str | None = None,
        ref: str = "HEAD",
        source_type: str = "local",
        source_ref: str | None = None,
    ) -> dict[str, object]:
        parser = GitHistoryParser(path, timeout_seconds=self.git_timeout_seconds)
        parser.validate()
        ref_sha = parser.resolve_ref(ref)
        repository_id = self.store.create_repository(
            name=name or path.resolve().name,
            source_type=source_type,
            source_ref=source_ref or str(path.resolve()),
            local_path=path,
        )
        self.store.set_status(repository_id, RepositoryStatus.ANALYSING)

        try:
            self.store.replace_analysis(repository_id, ref_sha, parser.iter_commits(ref_sha))
        except Exception as error:
            message = str(error) if isinstance(error, RatError) else "Repository analysis failed"
            self.store.set_status(repository_id, RepositoryStatus.FAILED, error=message)
            raise

        return self.store.get_repository(repository_id)
