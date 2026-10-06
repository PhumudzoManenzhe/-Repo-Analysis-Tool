"""Persistence operations for repository metadata and parsed history."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rat.db import Database
from rat.errors import InvalidAuthorMergeError, RepositoryNotFoundError
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

    def list_authors(self, repository_id: int) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            self._assert_repository_exists(connection, repository_id)
            rows = connection.execute(
                """
                SELECT a.id, a.name, a.email, a.display_name,
                       COUNT(c.sha) AS commit_count,
                       am.canonical_author_id,
                       canonical.display_name AS canonical_display_name
                FROM authors a
                LEFT JOIN commits c
                  ON c.repository_id = a.repository_id AND c.author_id = a.id
                LEFT JOIN author_merges am
                  ON am.repository_id = a.repository_id AND am.source_author_id = a.id
                LEFT JOIN authors canonical ON canonical.id = am.canonical_author_id
                WHERE a.repository_id = ?
                GROUP BY a.id, a.name, a.email, a.display_name,
                         am.canonical_author_id, canonical.display_name
                ORDER BY a.display_name COLLATE NOCASE
                """,
                (repository_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def merge_authors(
        self,
        repository_id: int,
        source_author_id: int,
        canonical_author_id: int,
    ) -> list[dict[str, Any]]:
        if source_author_id == canonical_author_id:
            raise InvalidAuthorMergeError("An author cannot be merged into itself")
        with self.database.transaction() as connection:
            self._assert_repository_exists(connection, repository_id)
            rows = connection.execute(
                """
                SELECT id FROM authors
                WHERE repository_id = ? AND id IN (?, ?)
                """,
                (repository_id, source_author_id, canonical_author_id),
            ).fetchall()
            if len(rows) != 2:
                raise InvalidAuthorMergeError("Both authors must belong to this repository")

            target = connection.execute(
                """
                SELECT canonical_author_id FROM author_merges
                WHERE repository_id = ? AND source_author_id = ?
                """,
                (repository_id, canonical_author_id),
            ).fetchone()
            final_canonical_id = (
                int(target["canonical_author_id"]) if target is not None else canonical_author_id
            )
            if final_canonical_id == source_author_id:
                raise InvalidAuthorMergeError("The requested merge would create a cycle")

            connection.execute(
                """
                UPDATE author_merges SET canonical_author_id = ?
                WHERE repository_id = ? AND canonical_author_id = ?
                """,
                (final_canonical_id, repository_id, source_author_id),
            )
            connection.execute(
                """
                INSERT INTO author_merges(repository_id, source_author_id, canonical_author_id)
                VALUES (?, ?, ?)
                ON CONFLICT(repository_id, source_author_id)
                DO UPDATE SET canonical_author_id = excluded.canonical_author_id
                """,
                (repository_id, source_author_id, final_canonical_id),
            )
        return self.list_authors(repository_id)

    def unmerge_author(self, repository_id: int, source_author_id: int) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            self._assert_repository_exists(connection, repository_id)
            connection.execute(
                "DELETE FROM author_merges WHERE repository_id = ? AND source_author_id = ?",
                (repository_id, source_author_id),
            )
        return self.list_authors(repository_id)

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
        batch_size: int = 1_000,
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
            commit_rows: list[tuple[object, ...]] = []
            change_rows: list[tuple[object, ...]] = []
            for parsed in batch:
                commit = parsed.commit
                author_id = self._author_id(
                    connection,
                    repository_id,
                    commit.author_name,
                    commit.author_email,
                    author_ids,
                )
                commit_rows.append(
                    (
                        repository_id,
                        commit.sha,
                        commit.parent_sha,
                        author_id,
                        commit.committer_date,
                    )
                )
                change_rows.extend(
                    (
                        repository_id,
                        commit.sha,
                        change.object_type,
                        change.path,
                        change.added,
                        change.removed,
                    )
                    for change in parsed.changes
                )

            connection.executemany(
                """
                INSERT INTO commits(repository_id, sha, parent_sha, author_id, committer_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                commit_rows,
            )
            if change_rows:
                connection.executemany(
                    """
                    INSERT INTO object_changes(
                        repository_id, commit_sha, object_type, path, added, removed
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    change_rows,
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
