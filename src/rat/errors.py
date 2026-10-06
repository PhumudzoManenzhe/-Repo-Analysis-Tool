"""Domain-specific errors for predictable API and CLI failures."""


class RatError(Exception):
    """Base class for expected Repo Analysis Tool errors."""


class GitCommandError(RatError):
    """Raised when Git cannot clone, inspect, or parse a repository."""


class InvalidRepositoryError(RatError):
    """Raised when a path or archive is not a usable Git repository."""


class RepositoryNotFoundError(RatError):
    """Raised when repository metadata does not exist."""


class InvalidCommitSetError(RatError):
    """Raised when commit selection filters are invalid."""


class InvalidArchiveError(RatError):
    """Raised when an uploaded archive is unsafe or malformed."""


class InvalidRemoteUrlError(RatError):
    """Raised when a clone URL is malformed or targets a blocked network."""


class JobNotFoundError(RatError):
    """Raised when a background ingestion job does not exist."""


class InvalidAuthorMergeError(RatError):
    """Raised when authors cannot be merged within a repository."""
