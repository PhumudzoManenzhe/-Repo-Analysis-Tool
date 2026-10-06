"""Typed domain models shared by ingestion, storage, and API layers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ObjectType(StrEnum):
    REPOSITORY = "repository"
    DIRECTORY = "directory"
    FILE = "file"


class RepositoryStatus(StrEnum):
    PENDING = "pending"
    ANALYSING = "analysing"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommitRecord:
    sha: str
    parent_sha: str | None
    author_name: str
    author_email: str
    committer_date: int

    @property
    def author_display(self) -> str:
        return f"{self.author_name} <{self.author_email}>"


@dataclass(frozen=True, slots=True)
class ObjectChange:
    object_type: ObjectType
    path: str
    added: int
    removed: int

    @property
    def churn(self) -> int:
        return self.added + self.removed


@dataclass(frozen=True, slots=True)
class ParsedCommit:
    commit: CommitRecord
    changes: tuple[ObjectChange, ...]


@dataclass(frozen=True, slots=True)
class CommitSet:
    """A subset of non-merge commits selected by time or explicit hashes."""

    since: int | None = None
    until: int | None = None
    hashes: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.hashes and (self.since is not None or self.until is not None):
            raise ValueError("Explicit commit hashes cannot be combined with a time range")
        if self.since is not None and self.until is not None and self.since >= self.until:
            raise ValueError("since must be earlier than until")


@dataclass(frozen=True, slots=True)
class MetricRow:
    repo: str
    ref_sha: str
    commit_set: str
    commit_count: int
    object_type: ObjectType
    path: str
    author: str
    added: int
    removed: int
    growth: int
    churn: int
    modifications: int
    modification_frequency: float | None
    churn_rate: float | None
    ownership: float | None
