"""SQLite connection management and durable schema setup."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('local', 'clone', 'zip')),
    source_ref TEXT NOT NULL,
    local_path TEXT NOT NULL,
    ref_sha TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'analysing', 'ready', 'failed')),
    error TEXT,
    commit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    analysed_at TEXT
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    UNIQUE (repository_id, name, email)
);

CREATE TABLE IF NOT EXISTS author_merges (
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    source_author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    canonical_author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    PRIMARY KEY (repository_id, source_author_id),
    CHECK (source_author_id <> canonical_author_id)
);

CREATE TABLE IF NOT EXISTS commits (
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    parent_sha TEXT,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    committer_date INTEGER NOT NULL,
    PRIMARY KEY (repository_id, sha)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS object_changes (
    repository_id INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type IN ('repository', 'directory', 'file')),
    path TEXT NOT NULL,
    added INTEGER NOT NULL CHECK (added >= 0),
    removed INTEGER NOT NULL CHECK (removed >= 0),
    PRIMARY KEY (repository_id, commit_sha, object_type, path),
    FOREIGN KEY (repository_id, commit_sha)
        REFERENCES commits(repository_id, sha) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_commits_repo_date
    ON commits(repository_id, committer_date);
CREATE INDEX IF NOT EXISTS idx_commits_repo_author
    ON commits(repository_id, author_id);
CREATE INDEX IF NOT EXISTS idx_changes_repo_object
    ON object_changes(repository_id, object_type, path);
CREATE INDEX IF NOT EXISTS idx_changes_repo_commit
    ON object_changes(repository_id, commit_sha);
CREATE INDEX IF NOT EXISTS idx_author_merges_canonical
    ON author_merges(repository_id, canonical_author_id);
"""


class Database:
    """Creates short-lived SQLite connections configured for concurrent reads."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
