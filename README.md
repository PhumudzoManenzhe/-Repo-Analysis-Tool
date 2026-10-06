# Repo Analysis Tool

A self-contained FastAPI dashboard for measuring Git repository churn, growth, ownership, and modification frequency across repositories, directories, files, commit sets, and authors.

## Features

- Repository ingestion from a ZIP containing `.git`, a public HTTPS clone URL, or a server-local path
- Multiple independently navigable repositories with durable background job progress
- Streaming, single-pass, non-merge Git history analysis with bounded database batches
- Git-native rename detection, automatic `.mailmap` resolution, and persistent manual author merging
- Binary-file exclusion and recursive file, directory, and repository metrics
- Repository, author, file/directory, time-range, and explicit-commit filtering
- Churn-composition and ownership visualizations, searchable/sortable metrics, and CSV export
- SQLite WAL persistence with indexes optimized for repository, date, author, object, and commit queries
- Secure ZIP extraction and HTTPS cloning protections
- Automated unit, integration, frontend-contract, and reference-CSV verification

## Requirements

- Python 3.11+
- Git 2.x

## Setup

Run these commands from the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

No Node.js build or external database is required. Runtime files are created in `./rat-data` by default.

## Run

```bash
rat-api
```

If the console script is unavailable, use:

```bash
PYTHONPATH=src python -m uvicorn rat.api:create_app --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` for the dashboard or `http://127.0.0.1:8000/docs` for the interactive API. Use **Add repository** to upload a ZIP containing the repository's real `.git` directory, deeply clone a public HTTPS URL, or analyse a server-local path. Select repositories from the sidebar, manage aliases with **Merge authors**, apply metric filters, and use **Export CSV** to download the current selection.

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

Filters include `since`, `until`, repeated `commits`, `object_type`, `path`, and `author`. Time ranges use an inclusive `since` UNIX timestamp and exclusive `until` timestamp. A time range and an explicit commit list are intentionally mutually exclusive.

## Configuration

All configuration is optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `RAT_DATA_DIR` | `./rat-data` | Database, uploads, and cloned repositories |
| `RAT_DATABASE_PATH` | `$RAT_DATA_DIR/rat.sqlite3` | SQLite database location |
| `RAT_REPOSITORIES_DIR` | `$RAT_DATA_DIR/repositories` | Extracted and cloned repositories |
| `RAT_BACKGROUND_WORKERS` | `2` | Concurrent ZIP/clone analysis jobs |
| `RAT_ANALYSIS_BATCH_SIZE` | `1000` | Commits persisted per transaction |
| `RAT_GIT_TIMEOUT_SECONDS` | `1800` | Git command timeout |
| `RAT_MAX_UPLOAD_BYTES` | `262144000` | Maximum compressed ZIP size |

For a clean run, remove or relocate the configured data directory before startup. For production exposure, place the app behind an authenticated reverse proxy; the local-path ingestion option trusts users who can reach the API.

## Verification

```bash
pytest
ruff check .
rat-verify /path/to/cJSON ../repo-references/cJSON_6d9f2443ab07.csv
```

The verifier compares rows without relying on CSV ordering, checks integer fields exactly, and compares floating-point rates using a strict tolerance.

## Architecture

Git history is parsed once into per-commit object changes. File changes are rolled up to every ancestor directory and the repository root during ingestion. Indexed SQL aggregations then calculate metrics for any commit set without re-running Git. Analyses are written in bounded batches for large histories, while repository status prevents partial metric data from being exposed.

Runtime data defaults to `./rat-data` and can be relocated with `RAT_DATA_DIR`. The embedded SQLite database requires no external service and uses WAL mode so dashboard reads can continue during ingestion.