# opencode-usage

[English](README.md) | [简体中文](README.zh-CN.md)

Import multiple OpenCode usage JSON logs into a **single SQLite database**, deduplicate
them by `request_id`, and analyse usage and cost per model.

## Directory layout

```
opencode-usage/
├─ logs/                       raw JSON logs (added manually, read-only; gitignored)
├─ tmp/                        scratch scripts and drafts (gitignored)
├─ schema.sql                  tables + indexes, executed by db_import.py
├─ db_import.py                importer: idempotent load, dedup, ledger, conflict log
├─ db_query.py                 query tool: sources / models / accounts subcommands
├─ usage.db                    SQLite database (generated; gitignored)
├─ README.md                   English docs (this file)
└─ README.zh-CN.md             Chinese docs
```

## Usage

```pwsh
# Import every *.json under logs/ (when --file is omitted)
uv run python db_import.py

# Import specific files (repeatable, several paths allowed)
uv run python db_import.py --file logs/a.json logs/b.json

# Parse and report only, without writing to the database
uv run python db_import.py --dry-run

# List imported files, dedup stats and data coverage
uv run python db_query.py sources
uv run python db_query.py sources --tz UTC
uv run python db_query.py sources --db other.db

# Cost, token usage and request/cancelled/failed counts per model
uv run python db_query.py models

# Cost, request count and token usage by workspace / account / API key
# (IDs are truncated by default)
uv run python db_query.py accounts

# Full IDs only, no table
uv run python db_query.py accounts --full-id
```

## TODO / roadmap

- [x] **1. Analyse the JSON structure under `logs/`** (the write-up contains private data, so it is not committed)
- [x] **2. Fix the data model and metric definitions** → `schema.sql`
- [x] **3. Implement the importer** (sha256 idempotency / `requestID` dedup / conflict log) → `db_import.py`
- [x] **4. Query tool** (imported files and data coverage; cost, token usage and request/cancelled/failed counts per model; cost and token usage by workspace/account/API key) → `db_query.py` (`sources` / `models` / `accounts`)
- [ ] **5. Aggregate report per model** → `db_analyze.py`, phase 1 outputs
      (1) a main table by `model` (with a `product` breakdown), (2) an overall summary; supports `--csv` and `--tz`
- [ ] **6. Validation and anomaly report (deferred)**: retry distribution / per-attempt failure rate, anomaly list (e.g. `succeeded` without `cost`)
- [ ] **7. Model price table / theoretical cost comparison (deferred)**

## Import rules

1. Scan `logs/*.json` and read the `items` records one by one.
2. Idempotency is keyed on the **file content `sha256`**: already-imported files are skipped (renames and moves are still recognised).
3. **Deduplication** by `request_id`, using a canonical full-record hash `record_hash` to check consistency:
   - same `request_id` and same `record_hash` → duplicate, skipped;
   - same `request_id` but a different hash → **conflict**, recorded in the `conflicts` table;
     **the first write wins, nothing is overwritten, and the import keeps going**.
4. `attempts[]` is expanded into a child table and written or skipped together with its parent record.
5. `day_utc` is derived from `startedAt` in UTC at import time; raw timestamps are stored as epoch milliseconds.
6. Each file gets one ledger row in `imports` recording `truncated` / `until` / data coverage and similar
   metadata (kept for the record only, not used for filtering — `--complete-only` was dropped).

Importing is **idempotent** and can be re-run; write order does not affect the final deduplicated result.

## Data model

Full column definitions live in `schema.sql`.

- **`requests`** (PK `request_id`) — one row per request. All top-level scalar fields become columns
  (including `model`/`requested_model`/`outcome`/`status_code`/`cost_usd`/the token fields/`day_utc`/`source_file`);
  `requestHeaders` and `metadata` are kept only inside `raw` (the full JSON).
- **`attempts`** (PK `(request_id, attempt_index)`) — one row per attempt:
  `provider`/`model`/`status_code`/`duration_ms`/`error_code`.
- **`imports`** (PK `sha256`) — import ledger: file name/path/bytes/import time/record count/
  added/duplicate/conflict/`truncated`/`until`/data coverage start and end.
- **`conflicts`** (PK `(request_id, other_hash)`) — records with the same ID but different content (currently empty).

> `cost_usd` is `REAL` and stores the raw `cost` (in USD); a missing value is `NULL`.

## Metric definitions (per `model`)

| Metric | Definition |
|---|---|
| Success rate | `SUM(outcome='succeeded') / COUNT(*)`, plus a breakdown by `outcome` and `status_code` |
| Average cost per request (primary) | `SUM(COALESCE(cost_usd,0)) / COUNT(*)` (failed requests count as 0) |
| Average cost per request (secondary) | `SUM(cost_usd) / number of successful requests` |
| Cache hit rate | `SUM(cache_read_tokens) / (SUM(input_tokens) + SUM(cache_read_tokens) + SUM(cache_write_tokens))` |

`db_query.py` renders times according to `--tz` (default: the local timezone, shown as `UTC+xx` in the header).

## Conventions

- Run with `uv run python ...`
- Python standard library only (`sqlite3`, `json`); no third-party dependencies.
- `logs/`, `usage.db` and `tmp/` are not version-controlled (see `.gitignore`; the logs contain
  sensitive identifiers such as `sk_`/`acc_`/`wrk_`).
