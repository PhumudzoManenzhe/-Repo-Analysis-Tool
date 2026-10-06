"""Persistence operations for repository metadata and parsed history."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rat.db import Database
from rat.errors import RepositoryNotFoundError
from rat.models import ParsedCommit, RepositoryStatus


class RepositoryStore:
    """A small data-access layer that keeps SQLite details out of domain code."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_repository(
        self,
        *,
        name: str,
        source_type: str,
        source_ref: str,
        local_path: Path,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO repositories(name, source_type, source_ref, local_path, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    source_type,
                    source_ref,
                    str(local_path.resolve()),
                    RepositoryStatus.PENDING,
                ),
            )
            return int(cursor.lastrowid)

    def get_repository(self, repository_id: int) -> dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        if row is None:
            raise RepositoryNotFoundError(f"Repository {repository_id} was not found")
        return dict(row)

    def list_repositories(self) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM repositories ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_status(
        self,
        repository_id: int,
        status: RepositoryStatus,
        *,
        error: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE repositories SET status = ?, error = ? WHERE id = ?",
                (status, error, repository_id),
            )
            if cursor.rowcount == 0:
                raise RepositoryNotFoundError(f"Repository {repository_id} was not found")

    def set_local_path(self, repository_id: int, local_path: Path) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE repositories SET local_path = ? WHERE id = ?",
                (str(local_path.resolve()), repository_id),
            )
            if cursor.rowcount == 0:
                raise RepositoryNotFoundError(f"Repository {repository_id} was not found")

    def replace_analysis(
        self,
        repository_id: int,
        ref_sha: str,
        commits: Iterable[ParsedCommit],
        *,
        batch_size: int = 250,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        """Replace derived history in bounded batches hidden behind analysis status."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")

        with self.database.transaction() as connection:
            self._assert_repository_exists(connection, repository_id)
            self._clear_analysis(connection, repository_id)

        author_ids: dict[tuple[str, str], int] = {}
        commit_count = 0
        batch: list[ParsedCommit] = []
        for parsed in commits:
            batch.append(parsed)
            if len(batch) < batch_size:
                continue
            self._insert_batch(repository_id, batch, author_ids)
            commit_count += len(batch)
            batch.clear()
            if progress is not None:
                progress(commit_count)

        if batch:
            self._insert_batch(repository_id, batch, author_ids)
            commit_count += len(batch)
            if progress is not None:
                progress(commit_count)

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE repositories
                SET ref_sha = ?, status = ?, error = NULL, commit_count = ?,
                    analysed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (ref_sha, RepositoryStatus.READY, commit_count, repository_id),
            )

        return commit_count

    def clear_analysis(self, repository_id: int) -> None:
        """Remove partial derived data after an interrupted or failed analysis."""
        with self.database.transaction() as connection:
            self._assert_repository_exists(connection, repository_id)
            self._clear_analysis(connection, repository_id)

    @staticmethod
    def _clear_analysis(connection: sqlite3.Connection, repository_id: int) -> None:
        connection.execute("DELETE FROM object_changes WHERE repository_id = ?", (repository_id,))
        connection.execute("DELETE FROM commits WHERE repository_id = ?", (repository_id,))
        connection.execute("DELETE FROM author_merges WHERE repository_id = ?", (repository_id,))
        connection.execute("DELETE FROM authors WHERE repository_id = ?", (repository_id,))

    def _insert_batch(
        self,
        repository_id: int,
        batch: list[ParsedCommit],
        author_ids: dict[tuple[str, str], int],
    ) -> None:
        with self.database.transaction() as connection:
            for parsed in batch:
                commit = parsed.commit
                author_id = self._author_id(
                    connection,
                    repository_id,
                    commit.author_name,
                    commit.author_email,
                    author_ids,
                )
                connection.execute(
                    """
                    INSERT INTO commits(
                        repository_id, sha, parent_sha, author_id, committer_date
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        repository_id,
                        commit.sha,
                        commit.parent_sha,
                        author_id,
                        commit.committer_date,
                    ),
                )
                if parsed.changes:
                    connection.executemany(
                        """
                        INSERT INTO object_changes(
                            repository_id, commit_sha, object_type, path, added, removed
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                repository_id,
                                commit.sha,
                                change.object_type,
                                change.path,
                                change.added,
                                change.removed,
                            )
                            for change in parsed.changes
                        ),
                    )

    @staticmethod
    def _assert_repository_exists(connection: sqlite3.Connection, repository_id: int) -> None:
        row = connection.execute(
            "SELECT 1 FROM repositories WHERE id = ?", (repository_id,)
        ).fetchone()
        if row is None:
            raise RepositoryNotFoundError(f"Repository {repository_id} was not found")

    @staticmethod
    def _author_id(
        connection: sqlite3.Connection,
        repository_id: int,
        name: str,
        email: str,
        cache: dict[tuple[str, str], int],
    ) -> int:
        key = (name, email)
        cached = cache.get(key)
        if cached is not None:
            return cached

        display_name = f"{name} <{email}>"
        cursor = connection.execute(
            """
            INSERT INTO authors(repository_id, name, email, display_name)
            VALUES (?, ?, ?, ?)
            """,
            (repository_id, name, email, display_name),
        )
        author_id = int(cursor.lastrowid)
        cache[key] = author_id
        return author_id
