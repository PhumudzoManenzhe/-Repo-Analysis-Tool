# Repo Analysis Tool

A portable FastAPI backend for measuring Git repository churn, growth, ownership, and modification frequency across repositories, directories, files, commit sets, and authors.

## Current foundation

- Streaming, single-pass Git history parser
- Non-merge commits reachable from a selected reference
- Git-native 50% rename detection and `.mailmap` author resolution
- Binary-file exclusion and zero-line object tracking
- Recursive file, directory, and repository metrics
- Time-range and explicit-commit filtering
- Author ownership metrics
- SQLite persistence with WAL mode and atomic analysis replacement
- JSON API and reference-compatible CSV export
- Automated unit, integration, and cJSON contract verification

## Requirements

- Python 3.11+
- Git 2.x

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Run

```bash
rat-api
```

The dashboard is available at `http://127.0.0.1:8000`, with interactive API documentation at `http://127.0.0.1:8000/docs`. It supports local paths, secure ZIP uploads, HTTPS cloning, background progress, metric filters, and CSV export.

Analyse a local Git working tree:

```bash
curl -X POST http://127.0.0.1:8000/api/repositories/local \
  -H 'Content-Type: application/json' \
  -d '{"path":"/path/to/repository","name":"example","ref":"HEAD"}'
```

Retrieve metrics or export the exact reference CSV schema:

```bash
curl 'http://127.0.0.1:8000/api/repositories/1/metrics'
curl -o metrics.csv 'http://127.0.0.1:8000/api/repositories/1/metrics.csv'
```

Filters include `since`, `until`, repeated `commits`, `object_type`, `path`, and `author`. Time ranges use an inclusive `since` UNIX timestamp and exclusive `until` timestamp.

## Verification

```bash
pytest
ruff check .
rat-verify /path/to/cJSON ../repo-references/cJSON_6d9f2443ab07.csv
```

The verifier compares rows without relying on CSV ordering, checks integer fields exactly, and compares floating-point rates using a strict tolerance.

## Architecture

Git history is parsed once into per-commit object changes. File changes are rolled up to every ancestor directory and the repository root during ingestion. Indexed SQL aggregations then calculate metrics for any commit set without re-running Git. Repository analyses are replaced in one transaction, so failed runs never expose partial metric data.

Runtime data defaults to `./rat-data` and can be relocated with `RAT_DATA_DIR`. The embedded SQLite database requires no external service and uses WAL mode so dashboard reads can continue during ingestion.