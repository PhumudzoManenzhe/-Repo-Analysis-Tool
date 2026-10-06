"""Application services coordinating Git parsing and durable storage."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rat.errors import RatError
from rat.git_parser import GitHistoryParser
from rat.models import RepositoryStatus
from rat.storage import RepositoryStore

ProgressCallback = Callable[[str, int, str], None]


class AnalysisService:
    """Run repository analysis as an observable, failure-safe workflow."""

    def __init__(
        self,
        store: RepositoryStore,
        *,
        git_timeout_seconds: int = 1800,
        batch_size: int = 1_000,
    ) -> None:
        self.store = store
        self.git_timeout_seconds = git_timeout_seconds
        self.batch_size = batch_size

    def analyse_local(
        self,
        path: Path,
        *,
        name: str | None = None,
        ref: str = "HEAD",
        source_type: str = "local",
        source_ref: str | None = None,
    ) -> dict[str, object]:
        repository_id = self.store.create_repository(
            name=name or path.resolve().name,
            source_type=source_type,
            source_ref=source_ref or str(path.resolve()),
            local_path=path,
        )
        return self.analyse_repository(repository_id, path, ref=ref)

    def analyse_repository(
        self,
        repository_id: int,
        path: Path,
        *,
        ref: str = "HEAD",
        progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        self.store.set_status(repository_id, RepositoryStatus.ANALYSING)
        parser = GitHistoryParser(path, timeout_seconds=self.git_timeout_seconds)

        try:
            self._report(progress, "validating", 45, "Validating Git repository")
            parser.validate()
            ref_sha = parser.resolve_ref(ref)
            total_commits = parser.count_commits(ref_sha)
            self._report(progress, "analysing", 50, f"Analysing {total_commits} commits")

            def report_commits(processed: int) -> None:
                ratio = processed / total_commits if total_commits else 1.0
                percent = min(99, 50 + int(ratio * 49))
                self._report(
                    progress,
                    "analysing",
                    percent,
                    f"Analysed {processed} of {total_commits} commits",
                )

            self.store.replace_analysis(
                repository_id,
                ref_sha,
                parser.iter_commits(ref_sha),
                batch_size=self.batch_size,
                progress=report_commits,
            )
        except Exception as error:
            try:
                self.store.clear_analysis(repository_id)
            finally:
                message = (
                    str(error) if isinstance(error, RatError) else "Repository analysis failed"
                )
                self.store.set_status(repository_id, RepositoryStatus.FAILED, error=message)
            raise

        return self.store.get_repository(repository_id)

    @staticmethod
    def _report(
        progress: ProgressCallback | None,
        stage: str,
        percent: int,
        message: str,
    ) -> None:
        if progress is not None:
            progress(stage, percent, message)
