-- OpenCode 用量日志库结构
-- 由 db_import.py 自动执行；全部 IF NOT EXISTS，可重复运行。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- requests
-- 一次 request 一行；request_id 为去重主键。
-- 顶层标量字段全部成列；requestHeaders / metadata 只保留在 raw 中。
CREATE TABLE IF NOT EXISTS requests (
    request_id              TEXT PRIMARY KEY,
    id                      TEXT,
    record_hash             TEXT NOT NULL,
    workspace_id            TEXT,
    user_id                 TEXT,
    service_account_id      TEXT,
    service_api_key_id      TEXT,
    session_id              TEXT,
    app                     TEXT,
    started_at              INTEGER,
    finished_at             INTEGER,
    duration_ms             INTEGER,
    day_utc                 TEXT,      -- 由 started_at 推导的 UTC 日期 YYYY-MM-DD
    outcome                 TEXT,
    status_code             INTEGER,
    category                TEXT,
    protocol                TEXT,
    product                 TEXT,
    path                    TEXT,
    method                  TEXT,
    stream                  INTEGER,
    provider                TEXT,
    user_agent              TEXT,
    model                   TEXT,
    requested_model         TEXT,
    error_code              TEXT,
    error_message           TEXT,
    finish_reason           TEXT,
    time_to_first_token_ms  INTEGER,
    first_token_at          INTEGER,
    last_token_at           INTEGER,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    reasoning_tokens        INTEGER,
    cache_read_tokens       INTEGER,
    cache_write_tokens      INTEGER,
    reasoning_mode          TEXT,
    reasoning_effort        TEXT,
    attempt_count           INTEGER,
    cost_usd                REAL,      -- 原始 cost，无值即 NULL
    country                 TEXT,
    city                    TEXT,
    region                  TEXT,
    source_file             TEXT,      -- 首次写入该记录的来源文件名
    raw                     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_model      ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_started_at ON requests(started_at);
CREATE INDEX IF NOT EXISTS idx_requests_day_utc    ON requests(day_utc);
CREATE INDEX IF NOT EXISTS idx_requests_outcome    ON requests(outcome);

-- ----------------------------------------------------------------- attempts
-- 一次 attempt 一行；随父记录一起写入/跳过。
CREATE TABLE IF NOT EXISTS attempts (
    request_id    TEXT    NOT NULL,
    attempt_index INTEGER NOT NULL,
    provider      TEXT,
    model         TEXT,
    status_code   INTEGER,
    duration_ms   INTEGER,
    error_code    TEXT,
    PRIMARY KEY (request_id, attempt_index),
    FOREIGN KEY (request_id) REFERENCES requests(request_id) ON DELETE CASCADE
);

-- ------------------------------------------------------------------ imports
-- 导入台账；sha256 为文件内容哈希（幂等判定键）。
CREATE TABLE IF NOT EXISTS imports (
    sha256      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    records     INTEGER NOT NULL,
    inserted    INTEGER NOT NULL,
    duplicates  INTEGER NOT NULL,
    conflicts   INTEGER NOT NULL,
    truncated   INTEGER NOT NULL DEFAULT 0,
    until       INTEGER,
    first_started_at INTEGER,   -- 文件内最早已 startedAt
    last_finished_at INTEGER    -- 文件内最晚的 finishedAt
);

-- ---------------------------------------------------------------- conflicts
-- 同 request_id 但 record_hash 不同 → 记一行；保留首次写入，不覆盖。
CREATE TABLE IF NOT EXISTS conflicts (
    request_id   TEXT NOT NULL,
    first_hash   TEXT NOT NULL,
    other_hash   TEXT NOT NULL,
    first_source TEXT,
    other_source TEXT,
    detected_at  TEXT NOT NULL,
    PRIMARY KEY (request_id, other_hash)
);
