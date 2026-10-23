# opencode-usage

[English](README.md) | [简体中文](README.zh-CN.md)

Import multiple OpenCode usage JSON logs into a **single SQLite database**, deduplicate
them by `request_id`, and analyse usage and cost per model, account or client.

## Directory layout

```
opencode-usage/
├─ logs/                       raw JSON logs (added manually, read-only; gitignored)
├─ schema.sql                  tables + indexes, executed by db_import.py
├─ db_import.py                importer: idempotent load, dedup, ledger, conflict log
├─ db_query.py                 query tool: sources / models / accounts / clients subcommands
├─ shared/                     shared modules: table rendering and formatters
└─ usage.db                    SQLite database (generated; gitignored)
```

## Usage

```pwsh
# Import every *.json under logs/ (when --file is omitted)
uv run python db_import.py

# Import specific files (repeatable, several paths allowed)
uv run python db_import.py --file logs/a.json logs/b.json

# Parse and report only, without writing to the database
uv run python db_import.py --dry-run

# List commands (all take --all, --tz and --db; models/accounts/clients take --days)
uv run python db_query.py sources [--all] [--tz UTC] [--db other.db]
uv run python db_query.py models [--all] [--days 7] [--tz UTC] [--db other.db]
uv run python db_query.py accounts [--full-id] [--days 7] [--tz UTC] [--db other.db]
uv run python db_query.py clients [--all] [--days 7] [--tz UTC] [--db other.db]
```

List commands show the top 5 rows by default — the most recently imported files
(`sources`), the highest-total-cost models (`models`) and clients (`clients`), with
ties broken by request count. A `...` row between the detail rows and `TOTAL` marks
the omission; `--all` prints the full table. `TOTAL` always summarises the whole
dataset, not just the rows shown. `accounts --full-id` prints only full IDs as an
indented tree, without a table.

`sources` scans the files in data-time order and prints a gap note below the table
when coverage is discontinuous (`A -> B: <span> missing (...)`).

`models`, `accounts` and `clients` take `--days N` for the last N calendar days in the
`--tz` zone (default: local; `--days 0` counts as 1): the window runs from 00:00 N-1 days
ago to now, `TOTAL` covers the window, and the same command run in the morning vs
evening can give different totals.

## Roadmap

- [x] Import + query (`schema.sql`, `db_import.py`, `db_query.py` with `sources` / `models` / `accounts` / `clients`)
- [ ] **Aggregate report per model** → `db_analyze.py`, phase 1 outputs
      (1) a main table by `model` (with a `product` breakdown), (2) an overall summary; supports `--csv` and `--tz`
- [ ] **Validation and anomaly report (deferred)**: retry distribution / per-attempt failure rate, anomaly list (e.g. `succeeded` without `cost`)
- [ ] **Model price table / theoretical cost comparison (deferred)**

## Import rules

1. Scan `logs/*.json` and read the `items` records one by one.
2. Idempotency is keyed on the **file content `sha256`**: an already-imported file is not written
   again (renames and moves are still recognised). Its records are still compared against the
   database so `dup` / `conflict` can be reported (`added` is 0), its ledger row keeps the
   original `imported_at`, and its summary row carries a `(skipped)` suffix.
3. **Deduplication** by `request_id`, using a canonical full-record hash `record_hash` to check consistency:
   same hash → duplicate, skipped; different hash → **conflict**, recorded in the `conflicts` table.
   **The first write wins, nothing is overwritten, and the import keeps going.**
   `attempts[]` is expanded into the child table and written or skipped together with its parent record.
4. `day_utc` is derived from `startedAt` in UTC at import time; raw timestamps are stored as epoch milliseconds.
5. Each file gets one ledger row in `imports` (record-keeping only, not used for filtering).

Importing is **idempotent** and can be re-run; write order does not affect the final deduplicated result.
Each run ends with a summary table followed by a `Total records in database: N` line
(the deduplicated number of requests in the database).

## Data model

Full column definitions live in `schema.sql`.

- **`requests`** (PK `request_id`) — one row per request; all top-level scalar fields become columns,
  the full JSON stays in `raw` (`requestHeaders` / `metadata` only live there).
- **`attempts`** (PK `(request_id, attempt_index)`) — one row per attempt.
- **`imports`** (PK `sha256`) — import ledger: file, bytes, import time, record/added/duplicate/conflict counts, coverage.
- **`conflicts`** (PK `(request_id, other_hash)`) — same ID but different content; first write is kept.

> `cost_usd` is `REAL` and stores the raw `cost` (in USD); a missing value is `NULL`.

## Metrics

- `cancelled` = `cancelled`; `failed` = `failed + rejected` (each with its share of the row `COUNT(*)`).
- **`cost/req`** = `SUM(cost_usd) / COUNT(cost_usd)` — the denominator is the billed requests only
  (records carrying a `cost` field; free models bill `0` and still count, unbilled records are `NULL`
  and do not). Shows `n/a` when the denominator is 0. In `accounts` this denominator is **not**
  the adjacent `requests` column (that one is `COUNT(*)`). `TOTAL` uses the global ratio,
  never the mean of the per-row values.
- `models` groups by `(model, provider, product)`, printed as `provider(product)`,
  ordered by total cost then request count. `accounts` groups by
  `workspace_id` / `COALESCE(user_id, service_account_id)` / API key; user rows render the key as `—`.
- `clients` groups by the client name derived from `user_agent` (first token, all versions
  merged): `opencode` also merges desktop + cli, `openai` SDKs become `openai-<lang>`; other
  clients are whatever the logs contain (e.g. `openclaw`, `opencode-go-copilot`,
  `codex-desktop`). Falls back to `app`, then `(unknown)`; ordered by total cost then
  request count.

`db_query.py` renders times according to `--tz` (default: the local timezone, shown as `UTC+xx` in the header); `--days` windows are drawn in the same zone.

## Conventions

- Run with `uv run python ...`
- Python standard library only (`sqlite3`, `json`); no third-party dependencies.
- `logs/` (sensitive identifiers), `tmp/` (agent scratch space) and SQLite databases (`*.db`, plus `-wal`/`-shm` sidecars)
  are not version-controlled (see `.gitignore`).
