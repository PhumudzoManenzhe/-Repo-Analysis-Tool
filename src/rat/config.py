"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Application settings with portable, local-first defaults."""

    data_dir: Path
    database_path: Path
    repositories_dir: Path
    git_timeout_seconds: int = 1800
    sqlite_busy_timeout_ms: int = 10_000

    @classmethod
    def from_environment(cls) -> Settings:
        data_dir = Path(os.getenv("RAT_DATA_DIR", "rat-data")).expanduser().resolve()
        database_path = (
            Path(os.getenv("RAT_DATABASE_PATH", str(data_dir / "rat.sqlite3")))
            .expanduser()
            .resolve()
        )
        repositories_dir = (
            Path(os.getenv("RAT_REPOSITORIES_DIR", str(data_dir / "repositories")))
            .expanduser()
            .resolve()
        )
        timeout = int(os.getenv("RAT_GIT_TIMEOUT_SECONDS", "1800"))
        busy_timeout = int(os.getenv("RAT_SQLITE_BUSY_TIMEOUT_MS", "10000"))
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            repositories_dir=repositories_dir,
            git_timeout_seconds=timeout,
            sqlite_busy_timeout_ms=busy_timeout,
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repositories_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
