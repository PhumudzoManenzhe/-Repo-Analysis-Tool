"""Secure archive extraction and full-history remote Git cloning."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import stat
import subprocess
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit

from rat.errors import GitCommandError, InvalidArchiveError, InvalidRemoteUrlError

ProgressCallback = Callable[[str, int, str], None]
_CHUNK_SIZE = 1024 * 1024
_CLONE_PERCENT = re.compile(r"(\d{1,3})%")


class SecureZipExtractor:
    """Extract ZIP files without trusting member paths, types, or size metadata."""

    def __init__(
        self,
        *,
        max_extracted_bytes: int,
        max_members: int,
        max_compression_ratio: float,
    ) -> None:
        self.max_extracted_bytes = max_extracted_bytes
        self.max_members = max_members
        self.max_compression_ratio = max_compression_ratio

    def extract(
        self,
        archive_path: Path,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> Path:
        if destination.exists():
            raise InvalidArchiveError("Archive destination already exists")

        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                self._validate_members(members)
                destination.mkdir(parents=True)
                self._extract_members(archive, members, destination, progress)
            repository_path = self._find_repository(destination)
        except InvalidArchiveError:
            self._remove_destination(destination)
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            self._remove_destination(destination)
            raise InvalidArchiveError("The uploaded file is not a valid ZIP archive") from error

        self._report(progress, "extracting", 40, "ZIP archive extracted securely")
        return repository_path

    def _validate_members(self, members: list[zipfile.ZipInfo]) -> None:
        if not members:
            raise InvalidArchiveError("The ZIP archive is empty")
        if len(members) > self.max_members:
            raise InvalidArchiveError(
                f"The ZIP archive exceeds the {self.max_members} member limit"
            )

        total_size = 0
        paths: set[str] = set()
        for member in members:
            normalized = self._safe_member_path(member)
            path_key = normalized.as_posix().rstrip("/")
            if path_key in paths:
                raise InvalidArchiveError(f"The ZIP archive contains a duplicate path: {path_key}")
            paths.add(path_key)

            if member.flag_bits & 0x1:
                raise InvalidArchiveError("Encrypted ZIP members are not supported")
            self._validate_member_type(member)
            total_size += member.file_size
            if total_size > self.max_extracted_bytes:
                raise InvalidArchiveError("The ZIP archive is too large after extraction")
            if member.file_size:
                ratio = member.file_size / max(member.compress_size, 1)
                if ratio > self.max_compression_ratio:
                    raise InvalidArchiveError(
                        f"ZIP member exceeds the compression ratio limit: {path_key}"
                    )

    def _extract_members(
        self,
        archive: zipfile.ZipFile,
        members: list[zipfile.ZipInfo],
        destination: Path,
        progress: ProgressCallback | None,
    ) -> None:
        total_expected = sum(member.file_size for member in members)
        total_written = 0
        for index, member in enumerate(members, start=1):
            relative = self._safe_member_path(member)
            target = destination.joinpath(*relative.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(member) as source, target.open("xb") as output:
                    while chunk := source.read(_CHUNK_SIZE):
                        written += len(chunk)
                        total_written += len(chunk)
                        if written > member.file_size or total_written > self.max_extracted_bytes:
                            raise InvalidArchiveError("ZIP contents exceed their declared size")
                        output.write(chunk)
                if written != member.file_size:
                    raise InvalidArchiveError("ZIP member size does not match its metadata")

            ratio = total_written / total_expected if total_expected else index / len(members)
            percent = min(39, 5 + int(ratio * 34))
            self._report(
                progress,
                "extracting",
                percent,
                f"Extracted {index} of {len(members)} ZIP members",
            )

    @staticmethod
    def _safe_member_path(member: zipfile.ZipInfo) -> PurePosixPath:
        raw_name = member.filename.replace("\\", "/")
        windows_path = PureWindowsPath(member.filename)
        path = PurePosixPath(raw_name)
        if (
            not raw_name
            or raw_name.startswith("/")
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise InvalidArchiveError(f"ZIP member has an unsafe path: {member.filename!r}")
        return path

    @staticmethod
    def _validate_member_type(member: zipfile.ZipInfo) -> None:
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if not file_type:
            return
        if stat.S_ISLNK(mode):
            raise InvalidArchiveError(f"ZIP member is a symbolic link: {member.filename}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise InvalidArchiveError(f"ZIP member has an unsupported type: {member.filename}")

    @staticmethod
    def _find_repository(destination: Path) -> Path:
        candidates: list[Path] = []
        for root, directories, files in os.walk(destination, followlinks=False):
            root_path = Path(root)
            if ".git" in files:
                raise InvalidArchiveError("Git metadata pointer files are not supported")
            if ".git" in directories:
                candidates.append(root_path)
                directories.remove(".git")

        top_level = [
            candidate
            for candidate in candidates
            if not any(
                candidate.is_relative_to(other) for other in candidates if other != candidate
            )
        ]
        if not top_level:
            raise InvalidArchiveError("The ZIP archive does not contain a Git working tree")
        if len(top_level) != 1:
            raise InvalidArchiveError("The ZIP archive contains multiple Git working trees")
        return top_level[0]

    @staticmethod
    def _remove_destination(destination: Path) -> None:
        if destination.exists():
            shutil.rmtree(destination)

    @staticmethod
    def _report(
        progress: ProgressCallback | None,
        stage: str,
        percent: int,
        message: str,
    ) -> None:
        if progress is not None:
            progress(stage, percent, message)


def validate_remote_url(url: str) -> str:
    """Return a normalized public HTTPS Git URL or reject it before cloning."""
    normalized_url, _, _ = _resolve_remote_url(url)
    return normalized_url


def _resolve_remote_url(url: str) -> tuple[str, str, tuple[str, ...]]:
    if not url or url != url.strip() or any(character.isspace() for character in url):
        raise InvalidRemoteUrlError("The remote URL is invalid")

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise InvalidRemoteUrlError("Only HTTPS remote URLs are supported")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise InvalidRemoteUrlError("Remote URLs must not contain credentials")
    if parsed.query or parsed.fragment:
        raise InvalidRemoteUrlError("Remote URLs must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise InvalidRemoteUrlError("The remote URL has an invalid port") from error
    if port not in {None, 443}:
        raise InvalidRemoteUrlError("Only the standard HTTPS port is supported")
    if not parsed.path or parsed.path == "/":
        raise InvalidRemoteUrlError("The remote URL must identify a repository")

    try:
        host = parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError as error:
        raise InvalidRemoteUrlError("The remote URL has an invalid host") from error
    if host == "localhost" or host.endswith(".localhost"):
        raise InvalidRemoteUrlError("The remote host is not publicly routable")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise InvalidRemoteUrlError("The remote host could not be resolved") from error
    if not addresses:
        raise InvalidRemoteUrlError("The remote host could not be resolved")

    resolved_ips: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        except ValueError as error:
            raise InvalidRemoteUrlError("The remote host resolved to an invalid address") from error
        if not ip.is_global:
            raise InvalidRemoteUrlError("The remote host is not publicly routable")
        address_text = str(ip)
        if address_text not in resolved_ips:
            resolved_ips.append(address_text)

    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    normalized_url = urlunsplit(("https", netloc, parsed.path, "", ""))
    return normalized_url, host, tuple(resolved_ips)


class RemoteRepositoryCloner:
    """Clone every branch and tag while reporting Git transfer progress."""

    def __init__(self, *, timeout_seconds: int = 1800) -> None:
        self.timeout_seconds = timeout_seconds

    def clone(
        self,
        url: str,
        destination: Path,
        progress: ProgressCallback | None = None,
    ) -> None:
        normalized_url, host, resolved_ips = _resolve_remote_url(url)
        if destination.exists():
            raise GitCommandError("Clone destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._report(progress, "cloning", 5, "Starting full-history clone")

        pinned_addresses = ",".join(
            f"[{address}]" if ":" in address else address for address in resolved_ips
        )
        command = [
            "git",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "http.followRedirects=false",
            "-c",
            "credential.helper=",
            "-c",
            f"http.curloptResolve={host}:443:{pinned_addresses}",
            "clone",
            "--progress",
            "--no-single-branch",
            "--",
            normalized_url,
            str(destination),
        ]
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            bufsize=0,
        )
        assert process.stderr is not None
        timed_out = threading.Event()

        def terminate() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(self.timeout_seconds, terminate)
        timer.daemon = True
        timer.start()
        recent_output: list[str] = []
        current = ""
        try:
            while character := process.stderr.read(1):
                if character not in {"\r", "\n"}:
                    if len(current) < 4_000:
                        current += character
                    continue
                if current:
                    recent_output.append(current)
                    del recent_output[:-5]
                    self._report_git_progress(current, progress)
                    current = ""
            if current:
                recent_output.append(current)
                self._report_git_progress(current, progress)
            return_code = process.wait()
        finally:
            timer.cancel()
            process.stderr.close()

        if return_code != 0:
            if destination.exists():
                shutil.rmtree(destination)
            if timed_out.is_set():
                raise GitCommandError(f"Git clone exceeded {self.timeout_seconds} seconds")
            detail = recent_output[-1].strip() if recent_output else "Git clone failed"
            raise GitCommandError(detail)
        self._report(progress, "cloning", 40, "Full-history clone complete")

    @staticmethod
    def _report_git_progress(line: str, progress: ProgressCallback | None) -> None:
        match = _CLONE_PERCENT.search(line)
        if match is None:
            return
        git_percent = min(100, int(match.group(1)))
        percent = min(39, 5 + int(git_percent * 0.34))
        RemoteRepositoryCloner._report(progress, "cloning", percent, line.strip()[:500])

    @staticmethod
    def _report(
        progress: ProgressCallback | None,
        stage: str,
        percent: int,
        message: str,
    ) -> None:
        if progress is not None:
            progress(stage, percent, message)
