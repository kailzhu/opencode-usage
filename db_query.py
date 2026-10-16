#!/usr/bin/env python3
"""查询 OpenCode 用量数据库。

子命令：
  sources   列出已导入的文件：文件名、记录数、去重统计、是否被截断、
            数据覆盖时间范围，以及导入本数据库的时间。
  models    列出数据库里所有 model 的总花费、各类 token 消耗、取消率与失败率。
  accounts  按 workspace / 账户 / API Key 三层列出花费、请求数与各类 token 消耗。

Example:
  uv run python db_query.py sources
  uv run python db_query.py sources --tz UTC
  uv run python db_query.py models
  uv run python db_query.py accounts
  uv run python db_query.py accounts --db other.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from shared.fmt import (fmt_count_pct, fmt_int, fmt_iso, fmt_ms,
                        fmt_span, parse_tz, tz_label, trunc_id)
from shared.paths import DB_PATH
from shared.table import render

SOURCES_HEADERS = ["file name", "records", "added", "dup", "conflict", "trunc",
                   "data from", "data to", "span", "imported at"]
SOURCES_ALIGNS = ["l", "r", "r", "r", "r", "c", "l", "l", "r", "l"]

MODELS_HEADERS = ["model", "total cost (usd)", "input", "output", "reasoning",
                  "cache read", "cache write", "total", "cancelled", "failed"]
MODELS_ALIGNS = ["l"] + ["r"] * 9
MODELS_GROUPS = [None, None,
                 "tokens", "tokens", "tokens", "tokens", "tokens",
                 "requests", "requests", "requests"]

ACCOUNTS_HEADERS = ["workspace", "account", "api key", "total cost (usd)", "requests",
                    "input", "output", "reasoning", "cache read", "cache write"]
ACCOUNTS_ALIGNS = ["l", "l", "l"] + ["r"] * 7
ACCOUNTS_GROUPS = [None, None, None, None, None,
                   "tokens", "tokens", "tokens", "tokens", "tokens"]


def cmd_sources(conn: sqlite3.Connection, tz) -> int:
    rows = conn.execute(
        "SELECT name, records, inserted, duplicates, conflicts, truncated,"
        "       first_started_at, last_finished_at, imported_at"
        "  FROM imports ORDER BY imported_at DESC, name"
    ).fetchall()
    if not rows:
        print("数据库中还没有导入记录，请先运行 db_import.py。")
        return 0

    table = []
    tot = [0, 0, 0, 0]  # records, inserted, duplicates, conflicts
    g_first = g_last = None
    for (name, records, inserted, dup, conf, trunc, first, last, imported_at) in rows:
        tot[0] += records
        tot[1] += inserted
        tot[2] += dup
        tot[3] += conf
        if first is not None:
            g_first = first if g_first is None else min(g_first, first)
        if last is not None:
            g_last = last if g_last is None else max(g_last, last)
        table.append([
            name, records, inserted, dup, conf,
            "yes" if trunc else "no",
            fmt_ms(first, tz), fmt_ms(last, tz),
            fmt_span(first, last),
            fmt_iso(imported_at, tz),
        ])
    table.append([
        "TOTAL", tot[0], tot[1], tot[2], tot[3], "",
        fmt_ms(g_first, tz), fmt_ms(g_last, tz), fmt_span(g_first, g_last), "",
    ])

    print(f"{len(rows)} file(s) imported   [tz: {tz_label(tz)}]\n")
    render(SOURCES_HEADERS, SOURCES_ALIGNS, table, separator_before=len(table) - 1)
    print(
        "\nlegend:\n"
        "  records     该文件内的记录总数（含重复）\n"
        "  added       新增入库 / dup 判重跳过 / conflict 同 ID 但字段不一致\n"
        "  trunc       是否被 1000 条上限截断（yes = 不完整）\n"
        "  data from/to  该 JSON 文件『全部记录（含重复）』的最早 startedAt ~ 最晚 finishedAt（TOTAL 行为整库范围）\n"
        "  span        覆盖跨度\n"
        "  imported at 该文件首次导入本数据库的时间（按 --tz 显示）"
    )
    return 0


def cmd_models(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT model,"
        "       SUM(COALESCE(cost_usd, 0))             AS total_cost,"
        "       SUM(COALESCE(input_tokens, 0))         AS input_tokens,"
        "       SUM(COALESCE(output_tokens, 0))        AS output_tokens,"
        "       SUM(COALESCE(reasoning_tokens, 0))     AS reasoning_tokens,"
        "       SUM(COALESCE(cache_read_tokens, 0))    AS cache_read_tokens,"
        "       SUM(COALESCE(cache_write_tokens, 0))   AS cache_write_tokens,"
        "       COUNT(*)                               AS requests,"
        "       SUM(outcome = 'cancelled')             AS cancelled,"
        "       SUM(outcome IN ('failed', 'rejected')) AS failures"
        "  FROM requests GROUP BY model"
        " ORDER BY total_cost DESC, model"
    ).fetchall()
    if not rows:
        print("数据库中还没有记录，请先运行 db_import.py。")
        return 0

    # cost, in, out, reasoning, cache_read, cache_write, n, cancelled, failures
    acc = [0.0] + [0] * 8
    table = []
    for model, cost, tok_in, tok_out, tok_reason, cache_r, cache_w, n, canc, fail in rows:
        for i, v in enumerate((cost, tok_in, tok_out, tok_reason, cache_r, cache_w, n, canc, fail)):
            acc[i] += v
        table.append([
            model, f"{cost:.6f}",
            fmt_int(tok_in), fmt_int(tok_out), fmt_int(tok_reason),
            fmt_int(cache_r), fmt_int(cache_w), fmt_int(n),
            fmt_count_pct(canc, n), fmt_count_pct(fail, n),
        ])
    table.append([
        "TOTAL", f"{acc[0]:.6f}",
        fmt_int(acc[1]), fmt_int(acc[2]), fmt_int(acc[3]), fmt_int(acc[4]), fmt_int(acc[5]),
        fmt_int(acc[6]),
        fmt_count_pct(acc[7], acc[6]), fmt_count_pct(acc[8], acc[6]),
    ])

    print(f"{len(rows)} model(s)\n")
    render(MODELS_HEADERS, MODELS_ALIGNS, table,
           separator_before=len(table) - 1, groups=MODELS_GROUPS)
    print("\nlegend:\n"
          "  tokens    = 各类型 token 消耗量\n"
          "  requests  = 请求计数：total = 请求总数（去重后）；cancelled = 取消数；\n"
          "              failed = 失败数（failed + rejected）；括号内为占请求总数的比例")
    return 0


def cmd_accounts(conn: sqlite3.Connection, full_id: bool = False) -> int:
    rows = conn.execute(
        "SELECT COALESCE(workspace_id, '(none)'),"
        "       COALESCE(user_id, service_account_id, '(unknown)'),"
        "       service_api_key_id,"
        "       SUM(COALESCE(cost_usd, 0)),"
        "       COUNT(*),"
        "       SUM(COALESCE(input_tokens, 0)),"
        "       SUM(COALESCE(output_tokens, 0)),"
        "       SUM(COALESCE(reasoning_tokens, 0)),"
        "       SUM(COALESCE(cache_read_tokens, 0)),"
        "       SUM(COALESCE(cache_write_tokens, 0))"
        "  FROM requests"
        " GROUP BY workspace_id, user_id, service_account_id, service_api_key_id"
        " ORDER BY workspace_id, SUM(COALESCE(cost_usd, 0)) DESC,"
        "          COALESCE(user_id, service_account_id)"
    ).fetchall()
    if not rows:
        print("数据库中还没有记录，请先运行 db_import.py。")
        return 0

    if full_id:                                  # 只列 ID，不打印表格
        tree: dict[str, dict[str, list[str]]] = {}
        for ws, account, key, *_ in rows:
            tree.setdefault(ws, {}).setdefault(account, [])
            if key:
                tree[ws][account].append(key)
        print(f"{len(tree)} workspace(s)\n")
        for ws, accounts in tree.items():
            print(f"workspace  {ws}")
            for account, keys in accounts.items():
                print(f"  account  {account}")
                for key in keys:
                    print(f"    api key  {key}")
        return 0

    # cost, requests, input, output, reasoning, cache_read, cache_write
    acc = [0.0] + [0] * 6
    table = []
    for idx, (ws, account, key, cost, n, tok_in, tok_out, tok_reason, cache_r, cache_w) in enumerate(rows):
        acc[0] += cost
        acc[1] += n
        for i, v in enumerate((tok_in, tok_out, tok_reason, cache_r, cache_w)):
            acc[2 + i] += v
        if idx == 0 or ws != rows[idx - 1][0]:
            ws_cell = trunc_id(ws)                       # 同一 workspace 的首行
        else:                                            # 后续行用连接符表示归属（靠右对齐）
            more = idx + 1 < len(rows) and rows[idx + 1][0] == ws
            ws_cell = ("├─" if more else "└─", "r")
        table.append([
            ws_cell, trunc_id(account), trunc_id(key), f"{cost:.6f}", fmt_int(n),
            fmt_int(tok_in), fmt_int(tok_out), fmt_int(tok_reason),
            fmt_int(cache_r), fmt_int(cache_w),
        ])
    table.append([
        "TOTAL", "", "", f"{acc[0]:.6f}", fmt_int(acc[1]),
        fmt_int(acc[2]), fmt_int(acc[3]), fmt_int(acc[4]), fmt_int(acc[5]), fmt_int(acc[6]),
    ])

    n_ws = len({r[0] for r in rows})
    n_acct = len({(r[0], r[1]) for r in rows})
    n_key = sum(1 for r in rows if r[2])
    print(f"{n_ws} workspace(s) | {n_acct} account(s) | {n_key} key(s)\n")
    render(ACCOUNTS_HEADERS, ACCOUNTS_ALIGNS, table,
           separator_before=len(table) - 1, groups=ACCOUNTS_GROUPS)
    print("\nlegend:\n"
          "  workspace = 同一 workspace 只在首行打印 ID，后续行以 ├─ / └─ 表示归属\n"
          "  account   = user_id 或 service_account_id（原始 ID）\n"
          "  api key   = service_api_key_id；user 行没有密钥，显示 —\n"
          "  tokens    = 各类型 token 消耗量\n"
          "  ID 已截断显示（保留前缀与尾部）；用 --full-id 查看完整 ID")
    return 0


def common_args(with_tz: bool = True) -> argparse.ArgumentParser:
    """子命令共享的选项：--db（所有子命令）与 --tz（仅显示时间的子命令）。"""
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--db", default=str(DB_PATH), metavar="PATH",
                   help=f"SQLite 数据库路径（默认: {DB_PATH.name}）")
    if with_tz:
        c.add_argument("--tz", default="local", metavar="TZ",
                       help="显示时区：local（默认，本机时区）、UTC，或小时偏移如 8、+8、-5")
    return c


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="db_query.py",
        description="查询 OpenCode 用量数据库。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py sources\n"
            "  uv run python db_query.py sources --tz UTC\n\n"
            "用 'db_query.py <command> --help' 查看子命令帮助。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser(
        "sources",
        parents=[common_args()],
        help="列出已导入的文件及其记录数、去重统计、数据覆盖时间范围",
        description="列出已导入的文件：文件名、记录数、去重统计、是否被截断、"
                    "数据覆盖时间范围，以及导入本数据库的时间。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py sources\n"
            "  uv run python db_query.py sources --tz UTC\n"
            "  uv run python db_query.py sources --db other.db\n\n"
            "时间按 --tz 指定的时区显示（默认使用本机时区）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub.add_parser(
        "models",
        parents=[common_args(with_tz=False)],
        help="列出所有 model 的花费、token 消耗与失败率",
        description="列出数据库里所有 model，按花费从高到低给出总花费（USD）、"
                    "各类 token 消耗（input/output/reasoning/cache read/cache write）"
                    "与取消数、失败数（各带占比）。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py models\n"
            "  uv run python db_query.py models --db other.db\n\n"
            "取消数 = cancelled；失败数 = failed + rejected；"
            "括号内为占该模型请求总数的比例。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    acc_p = sub.add_parser(
        "accounts",
        parents=[common_args(with_tz=False)],
        help="按 workspace / 账户 / API Key 列出花费、请求数与 token 消耗",
        description="按 workspace / 账户（user_id 或 service_account_id）/ API Key 三层，"
                    "列出各主体的总花费（USD）、请求总数与各类 token 消耗。"
                    "user 行没有 API Key，以 — 表示。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py accounts\n"
            "  uv run python db_query.py accounts --full-id\n"
            "  uv run python db_query.py accounts --db other.db\n\n"
            "账户 = COALESCE(user_id, service_account_id)；"
            "每行是该主体（或它的一把密钥）的汇总。\n"
            "默认将 ID 截断显示（保留前缀与尾部）；--full-id 改为只打印完整 ID。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    acc_p.add_argument("--full-id", action="store_true",
                       help="不打印表格，只打印每个 workspace 下的完整 ID")
    return p


def connect_ro(args) -> sqlite3.Connection | None:
    """只读打开 args.db；不存在时返回 None 并打印错误。"""
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: 数据库不存在: {db_path}（请先运行 db_import.py）", file=sys.stderr)
        return None
    # as_uri() 对空格/特殊字符安全
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)


def run_sources(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        return cmd_sources(conn, parse_tz(args.tz))
    finally:
        conn.close()


def run_models(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        return cmd_models(conn)
    finally:
        conn.close()


def run_accounts(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        return cmd_accounts(conn, full_id=args.full_id)
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sources":
        return run_sources(args)
    if args.command == "models":
        return run_models(args)
    if args.command == "accounts":
        return run_accounts(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
