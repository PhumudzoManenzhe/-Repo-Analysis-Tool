from __future__ import annotations

from pathlib import Path

from conftest import commit_all
from fastapi.testclient import TestClient

from rat.api import create_app
from rat.config import Settings


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "rat.sqlite3",
        repositories_dir=data_dir / "repositories",
    )


def test_health_and_not_found_responses(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        response = client.get("/api/repositories/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Repository 999 was not found"}


def test_local_analysis_to_metrics_is_an_end_to_end_vertical_slice(
    tmp_path: Path,
    git_repository: Path,
) -> None:
    (git_repository / "app.py").write_text("print('hello')\n", encoding="utf-8")
    ref_sha = commit_all(git_repository, "initial")

    with TestClient(create_app(_settings(tmp_path))) as client:
        analysis_response = client.post(
            "/api/repositories/local",
            json={"path": str(git_repository), "name": "sample", "ref": ref_sha},
        )
        assert analysis_response.status_code == 201
        repository = analysis_response.json()
        assert repository["status"] == "ready"
        assert repository["commit_count"] == 1

        metrics_response = client.get(
            f"/api/repositories/{repository['id']}/metrics",
            params={"object_type": "repository", "path": "/"},
        )
        csv_response = client.get(
            f"/api/repositories/{repository['id']}/metrics.csv",
            params={"object_type": "repository", "path": "/"},
        )

    assert metrics_response.status_code == 200
    aggregate = metrics_response.json()[0]
    assert aggregate["author"] == "ALL"
    assert aggregate["added"] == 1
    assert aggregate["churn"] == 1
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")


def test_multiple_repositories_are_kept_independently(
    tmp_path: Path,
    git_repository: Path,
) -> None:
    (git_repository / "app.py").write_text("one\n", encoding="utf-8")
    commit_all(git_repository, "initial")

    with TestClient(create_app(_settings(tmp_path))) as client:
        first = client.post(
            "/api/repositories/local",
            json={"path": str(git_repository), "name": "first"},
        ).json()
        second = client.post(
            "/api/repositories/local",
            json={"path": str(git_repository), "name": "second"},
        ).json()
        repositories = client.get("/api/repositories").json()

    assert first["id"] != second["id"]
    assert {repository["name"] for repository in repositories} == {"first", "second"}
    assert all(repository["status"] == "ready" for repository in repositories)


def test_author_merge_api_combines_identity_metrics(
    tmp_path: Path,
    git_repository: Path,
) -> None:
    (git_repository / "app.py").write_text("one\n", encoding="utf-8")
    commit_all(git_repository, "alice", author_name="Alice", author_email="alice@example.com")
    (git_repository / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    commit_all(
        git_repository,
        "bob",
        author_name="Bob",
        author_email="bob@example.com",
        timestamp=1_700_000_100,
    )

    with TestClient(create_app(_settings(tmp_path))) as client:
        repository = client.post(
            "/api/repositories/local",
            json={"path": str(git_repository)},
        ).json()
        authors = client.get(f"/api/repositories/{repository['id']}/authors").json()
        alice = next(author for author in authors if author["name"] == "Alice")
        bob = next(author for author in authors if author["name"] == "Bob")
        response = client.post(
            f"/api/repositories/{repository['id']}/author-merges",
            json={"source_author_id": bob["id"], "canonical_author_id": alice["id"]},
        )
        metrics = client.get(
            f"/api/repositories/{repository['id']}/metrics",
            params={"object_type": "repository", "path": "/"},
        ).json()

    assert response.status_code == 200
    assert len(metrics) == 2
    assert metrics[1]["author"] == "Alice <alice@example.com>"
    assert metrics[1]["ownership"] == 1.0
