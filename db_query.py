#!/usr/bin/env python3
"""查询 OpenCode 用量数据库。

子命令：
  sources   列出已导入的文件：文件名、记录数、去重统计、是否被截断、
            数据覆盖时间范围，以及导入本数据库的时间。
  models    列出数据库里所有 model 的总花费、各类 token 消耗、取消率与失败率。

Example:
  uv run python db_query.py sources
  uv run python db_query.py sources --tz UTC
  uv run python db_query.py models
  uv run python db_query.py models --db other.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "usage.db"

SOURCES_HEADERS = ["file name", "records", "added", "dup", "conflict", "trunc",
                   "data from", "data to", "span", "imported at"]
SOURCES_ALIGNS = ["l", "r", "r", "r", "r", "c", "l", "l", "r", "l"]

MODELS_HEADERS = ["model", "total cost (usd)", "input", "output", "reasoning",
                  "cache read", "cache write", "total", "cancelled", "failed"]
MODELS_ALIGNS = ["l"] + ["r"] * 9
MODELS_GROUPS = [None, None,
                 "tokens", "tokens", "tokens", "tokens", "tokens",
                 "requests", "requests", "requests"]


def parse_tz(value: str):
    """把 --tz 值解析成 tzinfo；返回 None 表示用本机时区。

    接受 'local'（本机）、'UTC'，或小时偏移如 '8'、'+8'、'-5'。
    """
    v = value.strip().lower()
    if v in ("", "local"):
        return None
    if v in ("utc", "gmt", "z"):
        return timezone.utc
    return timezone(timedelta(hours=float(v.lstrip("+"))))


def tz_label(tz) -> str:
    """时区标签，形如 'UTC+08'、'UTC-05'、'UTC+05:30'；本机时区追加 ' local'。"""
    t = tz if tz is not None else datetime.now().astimezone().tzinfo
    offset = datetime.now(t).utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hh, mm = divmod(abs(total) // 60, 60)
    label = f"UTC{sign}{hh:02d}" + (f":{mm:02d}" if mm else "")
    return f"{label} local" if tz is None else label


def fmt_ms(ms, tz) -> str:
    if ms is None:
        return "n/a"
    dt = datetime.fromtimestamp(ms / 1000, tz) if tz else datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_span(a, b) -> str:
    if a is None or b is None:
        return "n/a"
    hours = (b - a) / 3_600_000
    return f"{hours / 24:.1f}d" if hours >= 48 else f"{hours:.1f}h"


def fmt_iso(s, tz) -> str:
    """把导入台账里的 ISO-8601 UTC 时间按 tz 显示（tz 为 None 时用本机时区）。"""
    if not s:
        return "n/a"
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(s)
    dt = dt.astimezone(tz) if tz else dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def render(headers, aligns, rows, separator_before=None, groups=None) -> None:
    widths = []
    for i, h in enumerate(headers):
        cells = [len(str(r[i])) for r in rows] or [0]
        widths.append(max(len(str(h)), *cells))

    def rule() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def row_line(vals) -> str:
        cells = []
        for i, v in enumerate(vals):
            s = str(v)
            if aligns[i] == "r":
                s = s.rjust(widths[i])
            elif aligns[i] == "c":
                s = s.center(widths[i])
            else:
                s = s.ljust(widths[i])
            cells.append(s)
        return "| " + " | ".join(cells) + " |"

    def group_line() -> str:
        """分组表头：组名横跨其覆盖的几列（组内不画竖线）。"""
        out, i = [], 0
        while i < len(headers):
            g = groups[i]
            j = i
            while j + 1 < len(headers) and groups[j + 1] == g:
                j += 1
            area = sum(widths[i:j + 1]) + 2 + 3 * (j - i)
            out.append("|" + ("" if g is None else str(g)).center(area))
            i = j + 1
        return "".join(out) + "|"

    print(rule())
    if groups:
        print(group_line())
    print(row_line(headers))
    print(rule())
    for i, r in enumerate(rows):
        if separator_before is not None and i == separator_before:
            print(rule())
        print(row_line(r))
    print(rule())


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


def fmt_pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.2f}%" if den else "n/a"


def fmt_int(v: int) -> str:
    return f"{v:,}"


def fmt_count_pct(num: int, den: int) -> str:
    return f"{num} ({fmt_pct(num, den)})"


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


def common_args(with_tz: bool = True) -> argparse.ArgumentParser:
    """子命令共享的选项：--db（所有子命令）与 --tz（仅显示时间的子命令）。"""
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--db", default=str(DEFAULT_DB), metavar="PATH",
                   help=f"SQLite 数据库路径（默认: {DEFAULT_DB.name}）")
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


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sources":
        return run_sources(args)
    if args.command == "models":
        return run_models(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
