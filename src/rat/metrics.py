"""SQL-backed metric aggregation over a selected commit set."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from rat.db import Database
from rat.errors import InvalidCommitSetError, RepositoryNotFoundError
from rat.models import CommitSet, MetricRow, ObjectType, RepositoryStatus


class MetricsEngine:
    """Compute all specified metrics without re-reading Git history."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def calculate(
        self,
        repository_id: int,
        *,
        commit_set: CommitSet | None = None,
        object_type: ObjectType | None = None,
        path: str | None = None,
        author: str | None = None,
    ) -> list[MetricRow]:
        selection = commit_set or CommitSet()
        try:
            selection.validate()
        except ValueError as error:
            raise InvalidCommitSetError(str(error)) from error

        with self.database.read() as connection:
            repository = connection.execute(
                "SELECT name, ref_sha, status FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            if repository is None:
                raise RepositoryNotFoundError(f"Repository {repository_id} was not found")
            if repository["status"] != RepositoryStatus.READY:
                raise InvalidCommitSetError("Metrics are unavailable until analysis is complete")

            where, parameters = self._prepare_selection(
                connection, repository_id, selection, object_type, path
            )
            commit_count = self._commit_count(connection, where, parameters)
            if selection.hashes and commit_count != len(set(selection.hashes)):
                raise InvalidCommitSetError(
                    "One or more selected commits are unknown or are merge commits"
                )

            aggregate_rows = connection.execute(
                f"""
                SELECT oc.object_type, oc.path,
                       SUM(oc.added) AS added,
                       SUM(oc.removed) AS removed,
                       SUM(CASE WHEN oc.added + oc.removed > 0 THEN 1 ELSE 0 END)
                           AS modifications
                FROM commits c
                JOIN object_changes oc
                  ON oc.repository_id = c.repository_id AND oc.commit_sha = c.sha
                WHERE {where}
                GROUP BY oc.object_type, oc.path
                """,
                parameters,
            ).fetchall()

            author_where = f"{where} AND (oc.added + oc.removed) > 0"
            author_parameters = list(parameters)
            if author is not None:
                author_where += " AND canonical.display_name = ?"
                author_parameters.append(author)
            author_rows = connection.execute(
                f"""
                SELECT oc.object_type, oc.path, canonical.display_name AS author,
                       SUM(oc.added) AS added,
                       SUM(oc.removed) AS removed,
                       SUM(CASE WHEN oc.added + oc.removed > 0 THEN 1 ELSE 0 END)
                           AS modifications
                FROM commits c
                JOIN object_changes oc
                  ON oc.repository_id = c.repository_id AND oc.commit_sha = c.sha
                LEFT JOIN author_merges am
                  ON am.repository_id = c.repository_id AND am.source_author_id = c.author_id
                JOIN authors canonical
                  ON canonical.id = COALESCE(am.canonical_author_id, c.author_id)
                WHERE {author_where}
                GROUP BY oc.object_type, oc.path, canonical.id, canonical.display_name
                """,
                author_parameters,
            ).fetchall()

        return self._build_rows(
            repository["name"],
            repository["ref_sha"],
            self._selection_label(selection),
            commit_count,
            aggregate_rows,
            author_rows,
        )

    @staticmethod
    def _prepare_selection(
        connection: sqlite3.Connection,
        repository_id: int,
        selection: CommitSet,
        object_type: ObjectType | None,
        path: str | None,
    ) -> tuple[str, list[object]]:
        clauses = ["c.repository_id = ?"]
        parameters: list[object] = [repository_id]

        if selection.hashes:
            connection.execute("DROP TABLE IF EXISTS temp.selected_commits")
            connection.execute(
                "CREATE TEMP TABLE selected_commits(sha TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT OR IGNORE INTO selected_commits(sha) VALUES (?)",
                ((sha,) for sha in selection.hashes),
            )
            clauses.append("c.sha IN (SELECT sha FROM selected_commits)")
        if selection.since is not None:
            clauses.append("c.committer_date >= ?")
            parameters.append(selection.since)
        if selection.until is not None:
            clauses.append("c.committer_date < ?")
            parameters.append(selection.until)
        if object_type is not None:
            clauses.append("oc.object_type = ?")
            parameters.append(object_type)
        if path is not None:
            clauses.append("oc.path = ?")
            parameters.append(path)

        return " AND ".join(clauses), parameters

    @staticmethod
    def _commit_count(
        connection: sqlite3.Connection,
        where: str,
        parameters: list[object],
    ) -> int:
        # Object filters select displayed objects, not the denominator commit set.
        commit_clauses = [clause for clause in where.split(" AND ") if not clause.startswith("oc.")]
        filter_count = len(where.split("oc.")) - 1
        commit_parameters = parameters[:-filter_count] if filter_count else parameters
        row = connection.execute(
            f"SELECT COUNT(*) AS count FROM commits c WHERE {' AND '.join(commit_clauses)}",
            commit_parameters,
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _selection_label(selection: CommitSet) -> str:
        if selection.hashes:
            return "manual"
        if selection.since is not None or selection.until is not None:
            start = "*" if selection.since is None else str(selection.since)
            end = "*" if selection.until is None else str(selection.until)
            return f"time:{start}:{end}"
        return "all"

    @staticmethod
    def _build_rows(
        repository_name: str,
        ref_sha: str,
        selection_label: str,
        commit_count: int,
        aggregate_rows: list[sqlite3.Row],
        author_rows: list[sqlite3.Row],
    ) -> list[MetricRow]:
        grouped_authors: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in author_rows:
            grouped_authors[(row["object_type"], row["path"])].append(row)

        object_order = {"repository": 0, "directory": 1, "file": 2}
        sorted_aggregates = sorted(
            aggregate_rows,
            key=lambda row: (object_order[row["object_type"]], row["path"]),
        )
        result: list[MetricRow] = []

        for aggregate in sorted_aggregates:
            added = int(aggregate["added"])
            removed = int(aggregate["removed"])
            churn = added + removed
            modifications = int(aggregate["modifications"])
            key = (aggregate["object_type"], aggregate["path"])
            common = {
                "repo": repository_name,
                "ref_sha": ref_sha,
                "commit_set": selection_label,
                "commit_count": commit_count,
                "object_type": ObjectType(aggregate["object_type"]),
                "path": aggregate["path"],
            }
            result.append(
                MetricRow(
                    **common,
                    author="ALL",
                    added=added,
                    removed=removed,
                    growth=added - removed,
                    churn=churn,
                    modifications=modifications,
                    modification_frequency=modifications / commit_count if commit_count else 0.0,
                    churn_rate=churn / commit_count if commit_count else 0.0,
                    ownership=None,
                )
            )

            for author_row in sorted(
                grouped_authors[key],
                key=lambda row: (-(int(row["added"]) + int(row["removed"])), row["author"]),
            ):
                author_added = int(author_row["added"])
                author_removed = int(author_row["removed"])
                author_churn = author_added + author_removed
                result.append(
                    MetricRow(
                        **common,
                        author=author_row["author"],
                        added=author_added,
                        removed=author_removed,
                        growth=author_added - author_removed,
                        churn=author_churn,
                        modifications=int(author_row["modifications"]),
                        modification_frequency=None,
                        churn_rate=None,
                        ownership=author_churn / churn if churn else 0.0,
                    )
                )

        return result
