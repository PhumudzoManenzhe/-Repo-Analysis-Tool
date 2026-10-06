from __future__ import annotations

from pathlib import Path

import pytest

from rat.db import Database
from rat.export import CSV_COLUMNS, metrics_to_csv
from rat.metrics import MetricsEngine
from rat.models import CommitRecord, CommitSet, ObjectChange, ObjectType, ParsedCommit
from rat.storage import RepositoryStore


def _parsed_commit(
    sha: str,
    author: tuple[str, str],
    timestamp: int,
    path: str,
    added: int,
    removed: int,
) -> ParsedCommit:
    parts = path.split("/")
    changes = [ObjectChange(ObjectType.FILE, path, added, removed)]
    changes.extend(
        ObjectChange(ObjectType.DIRECTORY, "/".join(parts[:depth]), added, removed)
        for depth in range(1, len(parts))
    )
    changes.append(ObjectChange(ObjectType.REPOSITORY, "/", added, removed))
    return ParsedCommit(
        CommitRecord(sha, None, author[0], author[1], timestamp),
        tuple(changes),
    )


def _seed(database: Database, local_path: Path) -> tuple[int, MetricsEngine]:
    store = RepositoryStore(database)
    repository_id = store.create_repository(
        name="sample",
        source_type="local",
        source_ref=str(local_path),
        local_path=local_path,
    )
    commits = [
        _parsed_commit("a" * 40, ("Alice", "alice@example.com"), 100, "src/app.py", 10, 0),
        _parsed_commit("b" * 40, ("Bob", "bob@example.com"), 200, "src/app.py", 2, 1),
        _parsed_commit("c" * 40, ("Alice", "alice@example.com"), 300, "docs/readme.md", 5, 0),
    ]
    store.replace_analysis(repository_id, "c" * 40, commits)
    return repository_id, MetricsEngine(database)


def test_repository_metrics_and_author_ownership(database: Database, tmp_path: Path) -> None:
    repository_id, engine = _seed(database, tmp_path)

    rows = engine.calculate(
        repository_id,
        object_type=ObjectType.REPOSITORY,
        path="/",
    )

    assert len(rows) == 3
    aggregate = rows[0]
    assert aggregate.author == "ALL"
    assert (aggregate.added, aggregate.removed, aggregate.growth, aggregate.churn) == (
        17,
        1,
        16,
        18,
    )
    assert aggregate.modifications == 3
    assert aggregate.modification_frequency == 1.0
    assert aggregate.churn_rate == 6.0

    alice = next(row for row in rows if row.author == "Alice <alice@example.com>")
    bob = next(row for row in rows if row.author == "Bob <bob@example.com>")
    assert alice.modifications == 2
    assert alice.ownership == pytest.approx(15 / 18)
    assert bob.ownership == pytest.approx(3 / 18)


def test_author_filter_limits_aggregate_and_author_metrics(
    database: Database,
    tmp_path: Path,
) -> None:
    repository_id, engine = _seed(database, tmp_path)

    rows = engine.calculate(
        repository_id,
        object_type=ObjectType.REPOSITORY,
        path="/",
        author="Bob <bob@example.com>",
    )

    assert len(rows) == 2
    aggregate, bob = rows
    assert aggregate.author == "ALL"
    assert (aggregate.added, aggregate.removed, aggregate.churn) == (2, 1, 3)
    assert bob.author == "Bob <bob@example.com>"
    assert bob.ownership == 1.0


def test_manual_author_merge_updates_metrics_and_can_be_reversed(
    database: Database,
    tmp_path: Path,
) -> None:
    repository_id, engine = _seed(database, tmp_path)
    store = RepositoryStore(database)
    authors = store.list_authors(repository_id)
    alice = next(author for author in authors if author["name"] == "Alice")
    bob = next(author for author in authors if author["name"] == "Bob")

    merged = store.merge_authors(repository_id, int(bob["id"]), int(alice["id"]))
    bob_after = next(author for author in merged if author["name"] == "Bob")
    rows = engine.calculate(
        repository_id,
        object_type=ObjectType.REPOSITORY,
        path="/",
    )

    assert bob_after["canonical_display_name"] == "Alice <alice@example.com>"
    assert len(rows) == 2
    canonical = next(row for row in rows if row.author == "Alice <alice@example.com>")
    assert canonical.churn == 18
    assert canonical.modifications == 3
    assert canonical.ownership == 1.0

    store.unmerge_author(repository_id, int(bob["id"]))
    restored = engine.calculate(
        repository_id,
        object_type=ObjectType.REPOSITORY,
        path="/",
    )
    assert len(restored) == 3


def test_time_and_manual_commit_sets_use_selected_commit_denominator(
    database: Database,
    tmp_path: Path,
) -> None:
    repository_id, engine = _seed(database, tmp_path)

    timed = engine.calculate(
        repository_id,
        commit_set=CommitSet(since=150, until=300),
        object_type=ObjectType.REPOSITORY,
        path="/",
    )[0]
    assert timed.commit_count == 1
    assert timed.churn == 3
    assert timed.churn_rate == 3.0

    manual = engine.calculate(
        repository_id,
        commit_set=CommitSet(hashes=("a" * 40, "c" * 40)),
        object_type=ObjectType.REPOSITORY,
        path="/",
    )[0]
    assert manual.commit_count == 2
    assert manual.modifications == 2
    assert manual.churn == 15


def test_csv_schema_matches_reference_contract(database: Database, tmp_path: Path) -> None:
    repository_id, engine = _seed(database, tmp_path)
    csv_text = metrics_to_csv(
        engine.calculate(repository_id, object_type=ObjectType.REPOSITORY, path="/")
    )

    assert csv_text.splitlines()[0] == ",".join(CSV_COLUMNS)
    assert ",ALL,17,1,16,18,3,1.0,6.0," in csv_text


def test_zero_line_object_is_listed_without_an_author_modification(
    database: Database,
    tmp_path: Path,
) -> None:
    store = RepositoryStore(database)
    repository_id = store.create_repository(
        name="empty-file",
        source_type="local",
        source_ref=str(tmp_path),
        local_path=tmp_path,
    )
    commit = ParsedCommit(
        CommitRecord("d" * 40, None, "Alice", "alice@example.com", 100),
        (
            ObjectChange(ObjectType.FILE, "empty.txt", 0, 0),
            ObjectChange(ObjectType.REPOSITORY, "/", 0, 0),
        ),
    )
    store.replace_analysis(repository_id, "d" * 40, [commit])

    rows = MetricsEngine(database).calculate(
        repository_id,
        object_type=ObjectType.FILE,
        path="empty.txt",
    )

    assert len(rows) == 1
    assert rows[0].author == "ALL"
    assert rows[0].modifications == 0
    assert rows[0].churn == 0


def test_database_uses_wal_mode(database: Database) -> None:
    with database.read() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert journal_mode == "wal"
    assert foreign_keys == 1
