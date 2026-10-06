from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rat.db import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "test.sqlite3")
    instance.initialize()
    return instance


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    return repository


def commit_all(
    repository: Path,
    message: str,
    *,
    author_name: str = "Test User",
    author_email: str = "test@example.com",
    timestamp: int = 1_700_000_000,
) -> str:
    _git(repository, "add", "-A")
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    _git(repository, "commit", "-m", message, environment=environment)
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
