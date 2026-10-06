"""Stable CSV serialization matching the supplied reference contract."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import fields

from rat.models import MetricRow

CSV_COLUMNS = tuple(field.name for field in fields(MetricRow))


def metrics_to_csv(rows: Iterable[MetricRow]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "repo": row.repo,
                "ref_sha": row.ref_sha,
                "commit_set": row.commit_set,
                "commit_count": row.commit_count,
                "object_type": row.object_type,
                "path": row.path,
                "author": row.author,
                "added": row.added,
                "removed": row.removed,
                "growth": row.growth,
                "churn": row.churn,
                "modifications": row.modifications,
                "modification_frequency": _optional_number(row.modification_frequency),
                "churn_rate": _optional_number(row.churn_rate),
                "ownership": _optional_number(row.ownership),
            }
        )
    return stream.getvalue()


def _optional_number(value: float | None) -> str | float:
    return "" if value is None else value
