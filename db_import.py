#!/usr/bin/env python3
"""把 OpenCode 用量 JSON 日志导入 SQLite。

规则（与设计结论一致）：
  * 以文件内容 sha256 判幂等 —— 已导入过的文件直接跳过（重命名/移动也能识别）。
  * 以 requestID 去重；同 ID 且 record_hash 相同视为重复跳过；
    同 ID 但 record_hash 不同 → 记入 conflicts 表，保留首次、不覆盖、不中断。
  * attempts[] 展开为子表，随父记录一起写入或跳过。
  * day_utc 在导入时按 UTC 由 startedAt 推导。

Example:
  uv run python db_import.py
  uv run python db_import.py --file logs/request-logs-YYYY-MM-DD-24h.json
  uv run python db_import.py --file logs/a.json logs/b.json
  uv run python db_import.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from shared.fmt import fmt_int
from shared.paths import DB_PATH, LOGS_DIR, ROOT, SCHEMA_PATH
from shared.table import render

IMPORT_HEADERS = ["file name", "records", "added", "dup", "conflict"]
IMPORT_ALIGNS = ["l", "r", "r", "r", "r"]

# 列顺序必须与 schema.sql 中的 requests 表一致。
REQUEST_COLUMNS = [
    "request_id", "id", "record_hash", "workspace_id", "user_id",
    "service_account_id", "service_api_key_id", "session_id", "app",
    "started_at", "finished_at", "duration_ms", "day_utc", "outcome",
    "status_code", "category", "protocol", "product", "path", "method",
    "stream", "provider", "user_agent", "model", "requested_model",
    "error_code", "error_message", "finish_reason", "time_to_first_token_ms",
    "first_token_at", "last_token_at", "input_tokens", "output_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_mode", "reasoning_effort", "attempt_count", "cost_usd",
    "country", "city", "region", "source_file", "raw",
]
ATTEMPT_COLUMNS = [
    "request_id", "attempt_index", "provider", "model",
    "status_code", "duration_ms", "error_code",
]


def sql_placeholders(n: int) -> str:
    return ",".join("?" * n)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(rec) -> str:
    """字段顺序无关、无空白的字符串，用于记录一致性比对。"""
    return json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def record_hash(rec) -> str:
    return hashlib.sha256(canonical(rec).encode("utf-8")).hexdigest()


def load_doc(path: Path):
    """返回 (records, truncated, until)；兼容信封与裸数组两种形态。"""
    data = path.read_bytes()
    doc = json.loads(data.decode("utf-8-sig"))
    if isinstance(doc, list):
        return doc, 0, None, data
    if isinstance(doc, dict):
        items = doc.get("items")
        if not isinstance(items, list):
            # 兜底：取唯一的对象数组
            lists = [v for v in doc.values() if isinstance(v, list)]
            items = lists[0] if len(lists) == 1 else []
        truncated = 1 if doc.get("truncated") is True else 0
        until = doc.get("until") if isinstance(doc.get("until"), int) else None
        return items, truncated, until, data
    raise ValueError(f"{path}: 顶层既不是对象也不是数组")


def as_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def as_float(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_row(rec: dict, rhash: str, source: str) -> dict:
    started = as_int(rec.get("startedAt"))
    day_utc = None
    if started is not None:
        day_utc = datetime.fromtimestamp(started / 1000, timezone.utc).strftime("%Y-%m-%d")
    finish = rec.get("finish")
    finish_reason = finish.get("reason") if isinstance(finish, dict) else None
    attempts = rec.get("attempts") if isinstance(rec.get("attempts"), list) else []
    count = as_int(rec.get("attemptCount"))
    return {
        "request_id": rec.get("requestID") or rec.get("id"),
        "id": rec.get("id"),
        "record_hash": rhash,
        "workspace_id": rec.get("workspaceID"),
        "user_id": rec.get("userID"),
        "service_account_id": rec.get("serviceAccountID"),
        "service_api_key_id": rec.get("serviceAPIKeyID"),
        "session_id": rec.get("sessionID"),
        "app": rec.get("app"),
        "started_at": started,
        "finished_at": as_int(rec.get("finishedAt")),
        "duration_ms": as_int(rec.get("durationMs")),
        "day_utc": day_utc,
        "outcome": rec.get("outcome"),
        "status_code": as_int(rec.get("statusCode")),
        "category": rec.get("category"),
        "protocol": rec.get("protocol"),
        "product": rec.get("product"),
        "path": rec.get("path"),
        "method": rec.get("method"),
        "stream": int(rec["stream"]) if isinstance(rec.get("stream"), bool) else None,
        "provider": rec.get("provider"),
        "user_agent": rec.get("userAgent"),
        "model": rec.get("model"),
        "requested_model": rec.get("requestedModel"),
        "error_code": rec.get("errorCode"),
        "error_message": rec.get("errorMessage"),
        "finish_reason": finish_reason,
        "time_to_first_token_ms": as_int(rec.get("timeToFirstTokenMs")),
        "first_token_at": as_int(rec.get("firstTokenAt")),
        "last_token_at": as_int(rec.get("lastTokenAt")),
        "input_tokens": as_int(rec.get("inputTokens")),
        "output_tokens": as_int(rec.get("outputTokens")),
        "reasoning_tokens": as_int(rec.get("reasoningTokens")),
        "cache_read_tokens": as_int(rec.get("cacheReadTokens")),
        "cache_write_tokens": as_int(rec.get("cacheWriteTokens")),
        "reasoning_mode": rec.get("reasoningMode"),
        "reasoning_effort": rec.get("reasoningEffort"),
        "attempt_count": count if count is not None else len(attempts),
        "cost_usd": as_float(rec.get("cost")),
        "country": rec.get("country"),
        "city": rec.get("city"),
        "region": rec.get("region"),
        "source_file": source,
        "raw": json.dumps(rec, ensure_ascii=False, separators=(",", ":")),
    }


def build_attempt_rows(rid: str, rec: dict) -> list[tuple]:
    rows = []
    for i, a in enumerate(rec.get("attempts") or []):
        if not isinstance(a, dict):
            continue
        rows.append((rid, i, a.get("provider"), a.get("model"),
                     as_int(a.get("statusCode")), as_int(a.get("durationMs")),
                     a.get("errorCode")))
    return rows


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def source_label(path: Path) -> str:
    """记录来源标签，用文件名（与 imports.name 对应）。"""
    return path.name


def display_path(path: Path) -> str:
    """台账里的路径：尽量相对项目目录，便于阅读且跨机器稳定。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def import_file(conn, path: Path, *, dry_run: bool) -> dict:
    records, truncated, until, data = load_doc(path)
    sha = hashlib.sha256(data).hexdigest()
    name = path.name

    # 已导入过（sha256 命中台账）：逐条与库内比对以得出 dup / conflict，
    # 但不写入任何东西 —— 不插记录、不写 conflicts、不重写台账（保留首次 imported_at）。
    stats_only = conn.execute(
        "SELECT 1 FROM imports WHERE sha256 = ?", (sha,)
    ).fetchone() is not None
    writes = not dry_run and not stats_only

    hashes = dict(conn.execute("SELECT request_id, record_hash FROM requests"))
    req_sql = (f"INSERT INTO requests ({','.join(REQUEST_COLUMNS)}) "
               f"VALUES ({sql_placeholders(len(REQUEST_COLUMNS))})")
    att_sql = (f"INSERT INTO attempts ({','.join(ATTEMPT_COLUMNS)}) "
               f"VALUES ({sql_placeholders(len(ATTEMPT_COLUMNS))})")

    inserted = duplicates = conflicts = 0
    first_start = last_finish = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        st = as_int(rec.get("startedAt"))
        if st is not None:
            first_start = st if first_start is None else min(first_start, st)
        fi = as_int(rec.get("finishedAt"))
        if fi is not None:
            last_finish = fi if last_finish is None else max(last_finish, fi)
        rid = rec.get("requestID") or rec.get("id")
        if not rid:
            continue
        rhash = record_hash(rec)
        prev = hashes.get(rid)
        if prev is None:
            inserted += 1
            if writes:
                row = build_row(rec, rhash, source_label(path))
                conn.execute(req_sql, tuple(row[c] for c in REQUEST_COLUMNS))
                conn.executemany(att_sql, build_attempt_rows(rid, rec))
            hashes[rid] = rhash
        elif prev == rhash:
            duplicates += 1
        else:
            conflicts += 1
            if writes:
                conn.execute(
                    "INSERT OR IGNORE INTO conflicts"
                    "(request_id, first_hash, other_hash, first_source, other_source, detected_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (rid, prev, rhash, None, name, now_utc()),
                )

    if writes:
        conn.execute(
            "INSERT OR REPLACE INTO imports"
            "(sha256, name, path, bytes, imported_at, records, inserted, duplicates,"
            " conflicts, truncated, until, first_started_at, last_finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sha, name, display_path(path), len(data), now_utc(), len(records),
             inserted, duplicates, conflicts, truncated, until, first_start, last_finish),
        )
    return {"status": "skipped" if stats_only else "imported", "name": name,
            "records": len(records), "inserted": inserted, "duplicates": duplicates,
            "conflicts": conflicts, "truncated": truncated}


def resolve_files(args) -> list[Path]:
    if args.file:
        files = [Path(p) for group in args.file for p in group]
        missing = [p for p in files if not p.is_file()]
        if missing:
            for p in missing:
                print(f"error: 找不到文件: {p}", file=sys.stderr)
            raise SystemExit(2)
        return files
    logs = Path(args.logs)
    if not logs.is_dir():
        print(f"error: 日志目录不存在: {logs}（可用 --file 指定文件）", file=sys.stderr)
        raise SystemExit(2)
    return sorted(logs.glob("*.json"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="db_import.py",
        description="把 OpenCode 用量 JSON 日志导入 SQLite（按 request_id 去重，登记导入台账与冲突）。",
        epilog=(
            "示例:\n"
            "  uv run python db_import.py\n"
            "  uv run python db_import.py --file logs/request-logs-YYYY-MM-DD-24h.json\n"
            "  uv run python db_import.py --file logs/a.json logs/b.json\n"
            "  uv run python db_import.py --dry-run\n\n"
            "说明:\n"
            "  不指定 --file 时，导入 --logs 目录下全部 *.json。\n"
            "  同一文件（按 sha256）重复导入会被跳过。\n"
            "  同 request_id 但字段不一致时保留首次写入，并把冲突记入 conflicts 表。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--file", action="append", nargs="+", metavar="PATH",
                   help="要导入的 JSON 文件；可给多个，亦可重复使用本选项。省略则导入 --logs 目录下全部 *.json")
    p.add_argument("--db", default=str(DB_PATH), metavar="PATH",
                   help=f"SQLite 数据库路径（默认: {DB_PATH.name}）")
    p.add_argument("--logs", default=str(LOGS_DIR), metavar="DIR",
                   help=f"未指定 --file 时扫描的目录（默认: {LOGS_DIR.name}/）")
    p.add_argument("--schema", default=str(SCHEMA_PATH), metavar="PATH",
                   help=f"建表脚本路径（默认: {SCHEMA_PATH.name}）")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析并统计，不写入数据库")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    files = resolve_files(args)
    if not files:
        print("没有找到可导入的 *.json 文件。")
        return 0

    schema = Path(args.schema)
    if not schema.is_file():
        print(f"error: 找不到 schema 文件: {schema}", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    if args.dry_run and not db_path.exists():
        db_uri = ":memory:"          # 只解析不落盘
    else:
        db_uri = str(db_path)
    conn = sqlite3.connect(db_uri)
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn, schema)

    print(f"database : {db_path}")
    print(f"files    : {len(files)}" + ("   [dry-run]" if args.dry_run else ""))
    print()

    table = []
    tot = {"records": 0, "inserted": 0, "duplicates": 0, "conflicts": 0}
    for path in files:
        try:
            res = import_file(conn, path, dry_run=args.dry_run)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {path.name}: {exc}", file=sys.stderr)
            continue
        for k in tot:
            tot[k] += res[k]
        label = f"{res['name']} (skipped)" if res["status"] == "skipped" else res["name"]
        table.append([
            label, fmt_int(res["records"]), fmt_int(res["inserted"]),
            fmt_int(res["duplicates"]), fmt_int(res["conflicts"]),
        ])
        if not args.dry_run:
            conn.commit()

    table.append([
        "TOTAL", fmt_int(tot["records"]), fmt_int(tot["inserted"]),
        fmt_int(tot["duplicates"]), fmt_int(tot["conflicts"]),
    ])
    render(IMPORT_HEADERS, IMPORT_ALIGNS, table,
           separator_before=len(table) - 1 if len(table) > 1 else None)

    total_records = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    print(f"\nTotal records in database: {fmt_int(total_records)}")

    if args.dry_run:
        conn.rollback()
        print("\n[dry-run] 未写入数据库。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
