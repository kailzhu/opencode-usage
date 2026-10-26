# AGENTS.md - opencode-usage

## What This Repo Is

CLI tool that imports OpenCode Console usage JSON logs into one
local SQLite database and queries usage/cost statistics.

- `db_import.py` - importer: idempotent load, `request_id` dedup, ledger, conflict log.
- `db_query.py` - query tool with subcommands: `sources` / `models` / `accounts` / `clients` / `sessions`.
- `schema.sql` - sole DDL source. Tables: `requests` / `attempts` / `imports` / `conflicts`.
- `shared/` - shared modules only. No business logic elsewhere:
  - `shared/paths.py` - `ROOT` / `DB_PATH` / `LOGS_DIR` / `SCHEMA_PATH`. Single source of paths.
  - `shared/table.py` - `render()` grid table printer (`+---+` / `|`).
  - `shared/fmt.py` - display formatters (`fmt_int`, `fmt_pct`, `fmt_count_pct`,
    `trunc_id`, `client_name`, `parse_tz`, `tz_label`, `fmt_ms`, `fmt_span`, `fmt_iso`).
- `logs/` - raw JSON logs, added manually, read-only. Never commit (gitignored, contains secrets).
- `tmp/` - scratch scripts and drafts. Never commit (gitignored).
- `usage.db` (+ `-wal` / `-shm`) - generated database. Never commit (`*.db` gitignored).
- `README.md` (English) + `README.zh-CN.md` (Chinese) - keep in sync on every user-facing change.

## Commands

```
uv run python db_import.py
uv run python db_import.py --file logs/a.json logs/b.json
uv run python db_import.py --dry-run
uv run python db_query.py sources [--all] [--tz UTC|local|8] [--db other.db]
uv run python db_query.py models [--all] [--days N] [--tz UTC|local|8] [--db other.db]
uv run python db_query.py accounts [--full-id] [--days N] [--tz UTC|local|8] [--db other.db]
uv run python db_query.py clients [--all] [--days N] [--tz UTC|local|8] [--db other.db]
uv run python db_query.py sessions [--all] [--days N] [--tz UTC|local|8] [--db other.db]
```

- Use `uv run python`, not bare `python`.
- Examples in `--help` and docs use `uv run python ...` with PowerShell syntax.
- No test suite, linter, or formatter is configured. Verify by running the
  importer with `--dry-run` and each touched `db_query.py` subcommand before finishing.
- Open the DB read-only (`mode=ro`); the query tool must never write.

## Import Rules (do not change silently)

1. File idempotency is keyed on file-content `sha256`, not filename - renames/moves are recognised.
   An already-imported file is re-compared record-by-record for reporting (`dup`/`conflict`,
   `added` = 0, ledger `imported_at` unchanged), but nothing is written. Its row is marked `(skipped)`.
2. Record dedup is by `request_id` with a canonical full-record `record_hash`:
   same hash - duplicate, skip; different hash - row in `conflicts`, first write wins,
   never overwrite, never abort the import.
3. `attempts[]` expands into the `attempts` child table and is written/skipped with its parent.
4. `day_utc` is derived at import time from `startedAt` in UTC (`YYYY-MM-DD`); raw
   timestamps stay as epoch milliseconds; `REQUEST_COLUMNS` order must match `schema.sql`.
5. `load_doc()` accepts both envelope (`items` list) and bare-array JSON.
6. Every run prints a `shared.table.render()` summary plus `Total records in database: N`.

## Query / Display Rules

- New shared formatting or path helpers go in `shared/fmt.py` / `shared/paths.py` -
  do not keep local copies in `db_import.py` / `db_query.py`.
- Use `render(headers, aligns, rows, separator_before, groups)` for all tables:
  `l` / `r` / `c` aligns, `(text, align)` tuple for per-cell override,
  `separator_before` for the `TOTAL` row, `groups` for two-row grouped headers.
  `groups` must be exactly as long as `headers` (else `ValueError`).
- Number style: thousands separators (`fmt_int`), `N (P%)` via `fmt_count_pct`,
  `n/a` for empty denominators - never divide by zero.
- Metric definitions (keep legend + `--help` + both READMEs consistent):
  - `cancelled` = `cancelled`; `failed` = `failed + rejected` (+ share of row `COUNT(*)`).
  - `cost/req` = `SUM(cost_usd) / COUNT(cost_usd)` - denominator is billed requests only
    (records carrying a `cost` field; free-model `0` counts, unbilled `NULL` does not).
    It is larger than total-cost / `requests`. `TOTAL` uses the global ratio, never the mean of rows.
  - `models` groups by `(model, provider, product)`, prints `provider(product)`,
    orders models by total cost then requests; `--all`/top-5 limit counts models,
    never splits one model provider rows. Header shows the model count.
  - `accounts` groups by `workspace_id x COALESCE(user_id, service_account_id) x service_api_key_id`;
    user rows render the key as em-dash; merge the workspace cell with tree connectors;
    truncate IDs with `trunc_id()` (default width 20); `--full-id` prints only the ID tree, no table.
  - `clients` groups by `client_name(app, user_agent)` (`shared/fmt.py`): first `user_agent`
    segment, all versions merged; `opencode`'s desktop/cli merged into one row, `openai`
    SDKs as `openai-<lang>`, any other token used as-is; falls back to `app`, then
    `(unknown)`. Orders by total cost then requests.
  - `sessions` groups by `session_id` (NULL -> `(none)`, `trunc_id()` for display): cost,
    requests, five token categories, `cost/req`, `updated`, `client`, `model`. `updated` /
    `client` / `model` come from the latest request in the window (`started_at`, then
    `finished_at`, then `request_id`); `client` uses `client_name()`, `updated` is minute
    precision. Orders by total cost then requests.
  - `sources`/`models`/`clients`/`sessions` show top 5 by default with a `...` omission
    row; `TOTAL` always covers the whole dataset. `--all` prints everything.
- Time display: `--tz` accepts `local` / `UTC` / hour offsets (`8`, `+8`, `-5`);
  label with `tz_label()`; format with `fmt_ms()` / `fmt_iso()` / `fmt_span()`.
- `--days N` (models/accounts/clients/sessions only): last N calendar days in the
  `--tz` zone, window = [00:00 N-1 days ago, +infinity) via `started_at >= cutoff_ms` — the upper end
  is unbounded, i.e. "to now" as the READMEs phrase it (identical in practice unless the
  table somehow holds future records); `--days 0` counts as 1, negatives are a parse
  error; `TOTAL` covers the window; an empty window prints the window plus the newest
  record in the DB.

## Docs, Commits, Safety

- Update `README.md` and `README.zh-CN.md` together for any CLI, metric, or output change,
  including `--all` defaults, `(skipped)` rows, and new subcommands/columns.
- Commit style (from history): `type(scope): short imperative subject` + bullet body explaining
  the semantics, e.g. `feat(db_query): ...`, `fix(shared): ...`, `refactor: ...`,
  `docs: ...`, `chore: ...`. One logical change per commit.
- Code comments and `--help`/`epilog` are Chinese-leaning with English terms kept as-is
  (`model`, `provider`, `cost/req`); keep that voice for new text.
- Never commit `logs/`, `tmp/`, `*.db*`, or real IDs/keys. Use generic filenames
  (e.g. `logs/a.json`) in examples. Never overwrite the DB in query paths.
- Do not add copyright headers, inline code comments, one-letter variable names,
  new dependencies, or a `lib/` directory (it matches the Python gitignore; `shared/` is intentional).
