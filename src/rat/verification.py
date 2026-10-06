"""Compare generated metrics with the supplied reference CSV contract."""

from __future__ import annotations

import argparse
import csv
import math
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rat.db import Database
from rat.metrics import MetricsEngine
from rat.service import AnalysisService
from rat.storage import RepositoryStore

_KEY_FIELDS = ("object_type", "path", "author")
_INTEGER_FIELDS = ("commit_count", "added", "removed", "growth", "churn", "modifications")
_FLOAT_FIELDS = ("modification_frequency", "churn_rate", "ownership")


def verify_reference(repository_path: Path, reference_path: Path) -> list[str]:
    expected = _load_reference(reference_path)
    if not expected:
        return ["Reference CSV contains no metric rows"]

    first = next(iter(expected.values()))
    with tempfile.TemporaryDirectory(prefix="rat-verify-") as temporary_directory:
        database = Database(Path(temporary_directory) / "verify.sqlite3")
        database.initialize()
        store = RepositoryStore(database)
        service = AnalysisService(store)
        repository = service.analyse_local(
            repository_path,
            name=first["repo"],
            ref=first["ref_sha"],
        )
        actual_rows = MetricsEngine(database).calculate(int(repository["id"]))
        actual = {
            _metric_key(asdict(row)): _normalize_generated(asdict(row)) for row in actual_rows
        }

    return _compare(expected, actual)


def _load_reference(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    return {_metric_key(row): _normalize_reference(row) for row in rows}


def _metric_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return tuple(str(row[field]) for field in _KEY_FIELDS)  # type: ignore[return-value]


def _normalize_reference(row: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = dict(row)
    for field in _INTEGER_FIELDS:
        normalized[field] = int(row[field])
    for field in _FLOAT_FIELDS:
        normalized[field] = None if row[field] == "" else float(row[field])
    return normalized


def _normalize_generated(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    normalized["object_type"] = str(normalized["object_type"])
    return normalized


def _compare(
    expected: dict[tuple[str, str, str], dict[str, Any]],
    actual: dict[tuple[str, str, str], dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for key in sorted(expected.keys() - actual.keys()):
        errors.append(f"Missing metric row: {key}")
    for key in sorted(actual.keys() - expected.keys()):
        errors.append(f"Unexpected metric row: {key}")

    for key in sorted(expected.keys() & actual.keys()):
        expected_row = expected[key]
        actual_row = actual[key]
        for field in ("repo", "ref_sha", "commit_set", *_INTEGER_FIELDS):
            if expected_row[field] != actual_row[field]:
                errors.append(
                    f"{key} {field}: expected {expected_row[field]!r}, got {actual_row[field]!r}"
                )
        for field in _FLOAT_FIELDS:
            expected_value = expected_row[field]
            actual_value = actual_row[field]
            if expected_value is None or actual_value is None:
                if expected_value is not actual_value:
                    errors.append(
                        f"{key} {field}: expected {expected_value!r}, got {actual_value!r}"
                    )
            elif not math.isclose(expected_value, actual_value, rel_tol=1e-12, abs_tol=1e-12):
                errors.append(f"{key} {field}: expected {expected_value!r}, got {actual_value!r}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path, help="Path to a cloned Git working tree")
    parser.add_argument("reference", type=Path, help="Path to its expected metrics CSV")
    arguments = parser.parse_args()

    errors = verify_reference(arguments.repository, arguments.reference)
    if errors:
        print(f"Verification failed with {len(errors)} difference(s):")
        for error in errors[:100]:
            print(f"- {error}")
        if len(errors) > 100:
            print(f"- ... and {len(errors) - 100} more")
        raise SystemExit(1)
    print("Verification passed: every metric row matches the reference data.")
