"""Streaming parser for Git commit metadata and numstat changes."""

from __future__ import annotations

import ast
import re
import subprocess
import threading
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from rat.errors import GitCommandError, InvalidRepositoryError
from rat.models import CommitRecord, ObjectChange, ObjectType, ParsedCommit

_HEADER_MARKER = "\x1e"
_FIELD_SEPARATOR = "\x1f"
_RENAME_BRACES = re.compile(r"\{[^{}]* => ([^{}]*)\}")


class GitHistoryParser:
    """Parse a reachable Git history once while keeping memory bounded per commit."""

    def __init__(self, repository_path: Path, *, timeout_seconds: int = 1800) -> None:
        self.repository_path = repository_path.resolve()
        self.timeout_seconds = timeout_seconds

    def validate(self) -> None:
        if not self.repository_path.exists():
            raise InvalidRepositoryError(f"Repository path does not exist: {self.repository_path}")
        result = self._run_git("rev-parse", "--is-inside-work-tree")
        if result.stdout.strip() != "true":
            raise InvalidRepositoryError(f"Path is not a Git working tree: {self.repository_path}")

    def resolve_ref(self, ref: str = "HEAD") -> str:
        if not ref or ref.startswith("-") or any(character.isspace() for character in ref):
            raise InvalidRepositoryError("The Git reference is invalid")
        result = self._run_git("rev-parse", "--verify", f"{ref}^{{commit}}")
        sha = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise GitCommandError(f"Git returned an invalid commit hash for {ref!r}")
        return sha.lower()

    def iter_commits(self, ref_sha: str) -> Iterator[ParsedCommit]:
        """Yield non-merge commits and recursively aggregated object changes."""
        command = [
            "git",
            "-C",
            str(self.repository_path),
            "-c",
            "core.quotePath=false",
            "log",
            "--no-merges",
            "--find-renames=50%",
            "--numstat",
            f"--format={_HEADER_MARKER}%H{_FIELD_SEPARATOR}%P{_FIELD_SEPARATOR}%aN"
            f"{_FIELD_SEPARATOR}%aE{_FIELD_SEPARATOR}%ct",
            ref_sha,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        timed_out = threading.Event()

        def terminate() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(self.timeout_seconds, terminate)
        timer.daemon = True
        timer.start()
        current: CommitRecord | None = None
        file_changes: list[tuple[str, int, int]] = []

        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                if line.startswith(_HEADER_MARKER):
                    if current is not None:
                        yield ParsedCommit(current, self._aggregate_changes(file_changes))
                    current = self._parse_header(line)
                    file_changes = []
                    continue
                if current is None or not line or "\t" not in line:
                    continue
                parsed = self._parse_numstat(line)
                if parsed is not None:
                    file_changes.append(parsed)

            if current is not None:
                yield ParsedCommit(current, self._aggregate_changes(file_changes))
        finally:
            process.stdout.close()
            return_code = process.wait()
            timer.cancel()

        if return_code != 0:
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read().strip()
                process.stderr.close()
            if timed_out.is_set():
                raise GitCommandError(
                    f"Git history parsing exceeded {self.timeout_seconds} seconds"
                )
            raise GitCommandError(stderr or f"Git history parsing failed with status {return_code}")

    def _run_git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(self.repository_path), *arguments],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise GitCommandError(f"Git command exceeded {self.timeout_seconds} seconds") from error
        except subprocess.CalledProcessError as error:
            message = error.stderr.strip() or error.stdout.strip() or "Git command failed"
            raise InvalidRepositoryError(message) from error

    @staticmethod
    def _parse_header(line: str) -> CommitRecord:
        fields = line[1:].split(_FIELD_SEPARATOR)
        if len(fields) != 5:
            raise GitCommandError("Unexpected Git log header format")
        sha, parents, author_name, author_email, committer_date = fields
        parent_sha = parents.split(maxsplit=1)[0] if parents else None
        try:
            timestamp = int(committer_date)
        except ValueError as error:
            raise GitCommandError("Git returned an invalid committer timestamp") from error
        return CommitRecord(
            sha=sha,
            parent_sha=parent_sha,
            author_name=author_name,
            author_email=author_email,
            committer_date=timestamp,
        )

    @staticmethod
    def _parse_numstat(line: str) -> tuple[str, int, int] | None:
        fields = line.split("\t", 2)
        if len(fields) != 3:
            raise GitCommandError("Unexpected Git numstat row")
        added_text, removed_text, raw_path = fields
        if added_text == "-" or removed_text == "-":
            return None
        try:
            added = int(added_text)
            removed = int(removed_text)
        except ValueError as error:
            raise GitCommandError("Git returned non-numeric line counts") from error
        path = GitHistoryParser._rename_destination(GitHistoryParser._unquote(raw_path))
        return path, added, removed

    @staticmethod
    def _rename_destination(path: str) -> str:
        if " => " not in path:
            return path
        while match := _RENAME_BRACES.search(path):
            path = f"{path[: match.start()]}{match.group(1)}{path[match.end() :]}"
        if " => " in path:
            path = path.rsplit(" => ", 1)[1]
        return path

    @staticmethod
    def _unquote(path: str) -> str:
        if len(path) >= 2 and path[0] == path[-1] == '"':
            try:
                value = ast.literal_eval(path)
                if isinstance(value, str):
                    return value
            except (SyntaxError, ValueError):
                pass
        return path

    @staticmethod
    def _aggregate_changes(
        file_changes: list[tuple[str, int, int]],
    ) -> tuple[ObjectChange, ...]:
        totals: dict[tuple[ObjectType, str], list[int]] = defaultdict(lambda: [0, 0])
        for path, added, removed in file_changes:
            totals[(ObjectType.FILE, path)][0] += added
            totals[(ObjectType.FILE, path)][1] += removed

            parts = path.split("/")
            for depth in range(1, len(parts)):
                directory = "/".join(parts[:depth])
                totals[(ObjectType.DIRECTORY, directory)][0] += added
                totals[(ObjectType.DIRECTORY, directory)][1] += removed

            totals[(ObjectType.REPOSITORY, "/")][0] += added
            totals[(ObjectType.REPOSITORY, "/")][1] += removed

        order = {
            ObjectType.REPOSITORY: 0,
            ObjectType.DIRECTORY: 1,
            ObjectType.FILE: 2,
        }
        return tuple(
            ObjectChange(object_type, path, values[0], values[1])
            for (object_type, path), values in sorted(
                totals.items(), key=lambda item: (order[item[0][0]], item[0][1])
            )
        )
