from __future__ import annotations

from pathlib import Path

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


def test_dashboard_and_static_assets_are_served(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        dashboard = client.get("/")
        javascript = client.get("/static/app.js")
        stylesheet = client.get("/static/styles.css")

    assert dashboard.status_code == 200
    assert dashboard.headers["content-type"].startswith("text/html")
    assert "RepoScope" in dashboard.text
    assert "/api/repositories/zip" in javascript.text
    assert javascript.headers["content-type"].startswith("text/javascript")
    assert ".repository-list" in stylesheet.text
    assert stylesheet.headers["content-type"].startswith("text/css")
