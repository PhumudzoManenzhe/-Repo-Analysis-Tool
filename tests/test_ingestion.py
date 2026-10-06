from __future__ import annotations

import io
import shutil
import socket
import stat
import time
import zipfile
from pathlib import Path

import pytest
from conftest import commit_all
from fastapi.testclient import TestClient

from rat.api import create_app
from rat.config import Settings
from rat.errors import InvalidArchiveError, InvalidRemoteUrlError
from rat.ingestion import RemoteRepositoryCloner, SecureZipExtractor, validate_remote_url


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    data_dir = tmp_path / "data"
    values: dict[str, object] = {
        "data_dir": data_dir,
        "database_path": data_dir / "rat.sqlite3",
        "repositories_dir": data_dir / "repositories",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _extractor(**overrides: object) -> SecureZipExtractor:
    values: dict[str, object] = {
        "max_extracted_bytes": 1024 * 1024,
        "max_members": 100,
        "max_compression_ratio": 100.0,
    }
    values.update(overrides)
    return SecureZipExtractor(**values)  # type: ignore[arg-type]


def _zip_repository(repository: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in repository.rglob("*"):
            if path.is_file():
                relative = path.relative_to(repository)
                archive.write(path, f"project/{relative.as_posix()}")


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Background ingestion job did not finish")


def _public_dns(*_: object, **__: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def test_zip_extractor_rejects_path_traversal_without_writing_outside(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escaped.txt", "unsafe")

    destination = tmp_path / "repository"
    with pytest.raises(InvalidArchiveError, match="unsafe path"):
        _extractor().extract(archive_path, destination)

    assert not destination.exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_zip_extractor_rejects_symbolic_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    member = zipfile.ZipInfo("project/link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member, "../target")

    with pytest.raises(InvalidArchiveError, match="symbolic link"):
        _extractor().extract(archive_path, tmp_path / "repository")


def test_zip_extractor_rejects_git_pointer_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "pointer.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("project/.git", "gitdir: /tmp/outside")

    with pytest.raises(InvalidArchiveError, match="pointer files"):
        _extractor().extract(archive_path, tmp_path / "repository")


def test_zip_extractor_rejects_excessive_compression_ratio(tmp_path: Path) -> None:
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project/large.txt", "0" * 20_000)

    with pytest.raises(InvalidArchiveError, match="compression ratio"):
        _extractor(max_compression_ratio=2.0).extract(
            archive_path,
            tmp_path / "repository",
        )


def test_remote_url_validation_requires_public_credential_free_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    assert validate_remote_url("https://EXAMPLE.com/team/repo.git") == (
        "https://example.com/team/repo.git"
    )

    invalid_urls = [
        "http://example.com/repo.git",
        "file:///tmp/repo",
        "https://user:secret@example.com/repo.git",
        "https://example.com/repo.git?token=secret",
        "git@example.com:team/repo.git",
    ]
    for url in invalid_urls:
        with pytest.raises(InvalidRemoteUrlError):
            validate_remote_url(url)


def test_remote_url_validation_blocks_private_addresses() -> None:
    with pytest.raises(InvalidRemoteUrlError, match="not publicly routable"):
        validate_remote_url("https://127.0.0.1/repo.git")


def test_remote_clone_is_full_history_and_hardened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command: list[str], **_: object) -> None:
            commands.append(command)
            self.stderr = io.StringIO("Receiving objects: 100% (3/3)\n")

        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    monkeypatch.setattr("rat.ingestion.subprocess.Popen", FakeProcess)
    progress: list[tuple[str, int, str]] = []

    RemoteRepositoryCloner(timeout_seconds=10).clone(
        "https://example.com/team/repo.git",
        tmp_path / "clone",
        lambda stage, percent, message: progress.append((stage, percent, message)),
    )

    command = commands[0]
    assert "--no-single-branch" in command
    assert "--depth" not in command
    assert "protocol.file.allow=never" in command
    assert "http.followRedirects=false" in command
    assert "http.curloptResolve=example.com:443:93.184.216.34" in command
    assert progress[-1] == ("cloning", 40, "Full-history clone complete")


def test_zip_api_runs_in_background_and_exposes_progress(
    tmp_path: Path,
    git_repository: Path,
) -> None:
    (git_repository / "app.py").write_text("print('hello')\n", encoding="utf-8")
    commit_all(git_repository, "initial")
    archive_path = tmp_path / "repository.zip"
    _zip_repository(git_repository, archive_path)

    with TestClient(create_app(_settings(tmp_path))) as client:
        with archive_path.open("rb") as archive:
            response = client.post(
                "/api/repositories/zip",
                files={"file": ("repository.zip", archive, "application/zip")},
                data={"name": "uploaded", "ref": "HEAD"},
            )
        assert response.status_code == 202
        submitted = response.json()
        job = _wait_for_job(client, submitted["id"])
        repository = client.get(
            f"/api/repositories/{submitted['repository_id']}"
        ).json()

    assert job["status"] == "succeeded"
    assert job["stage"] == "complete"
    assert job["progress"] == 100
    assert repository["source_type"] == "zip"
    assert repository["status"] == "ready"
    assert repository["commit_count"] == 1


def test_clone_api_runs_in_background_and_analyses_full_repository(
    tmp_path: Path,
    git_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (git_repository / "app.py").write_text("print('hello')\n", encoding="utf-8")
    commit_all(git_repository, "initial")
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    def fake_clone(
        _: RemoteRepositoryCloner,
        __: str,
        destination: Path,
        progress: object,
    ) -> None:
        shutil.copytree(git_repository, destination)
        progress("cloning", 40, "Full-history clone complete")  # type: ignore[operator]

    monkeypatch.setattr(RemoteRepositoryCloner, "clone", fake_clone)
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/repositories/clone",
            json={"url": "https://example.com/team/repo.git", "ref": "HEAD"},
        )
        assert response.status_code == 202
        submitted = response.json()
        job = _wait_for_job(client, submitted["id"])
        repository = client.get(
            f"/api/repositories/{submitted['repository_id']}"
        ).json()

    assert job["status"] == "succeeded"
    assert repository["name"] == "repo"
    assert repository["source_type"] == "clone"
    assert repository["status"] == "ready"
    assert repository["commit_count"] == 1


def test_zip_upload_limit_and_missing_job_errors(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, max_upload_bytes=4))) as client:
        response = client.post(
            "/api/repositories/zip",
            files={"file": ("large.zip", b"12345", "application/zip")},
        )
        missing = client.get("/api/jobs/not-a-job")

    assert response.status_code == 413
    assert missing.status_code == 404
