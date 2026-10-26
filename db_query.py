#!/usr/bin/env python3
"""查询 OpenCode 用量数据库。

子命令：
  sources   列出最近导入的文件：文件名、记录数、去重统计、是否被截断、
            数据覆盖时间范围，以及导入本数据库的时间。
  models    列出总花费最高的 model（按 provider/product 拆分）：总花费、各类 token 消耗、
            取消率与失败率，以及平均单次花费（总花费 ÷ 被计费的请求数）。
  accounts  按 workspace / 账户 / API Key 三层列出花费、请求数、各类 token 消耗
            与平均单次花费（总花费 ÷ 被计费的请求数）。
  clients   按调用 API 的客户端（由 user_agent 推导）列出花费、请求数、
            各类 token 消耗与平均单次花费（总花费 ÷ 被计费的请求数）。
  sessions  按 session ID 列出花费、请求数、各类 token 消耗与平均单次花费
            （总花费 ÷ 被计费的请求数），以及该 session 最新一条请求的时间、client / model。

Example:
  uv run python db_query.py sources
  uv run python db_query.py sources --all
  uv run python db_query.py models
  uv run python db_query.py accounts
  uv run python db_query.py clients
  uv run python db_query.py sessions
  uv run python db_query.py accounts --db other.db

sources / models / clients / sessions 默认只显示前 5 行（sources 取最近导入的 5 个文件，
models、clients、sessions 取花费最高的 5 个），并用一行 ... 表示省略；--all 打印完整表格。
TOTAL 行始终按全部记录汇总（加 --days 时按窗口内全量汇总）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from shared.fmt import (client_name, fmt_count_pct, fmt_int, fmt_iso, fmt_ms,
                        fmt_span, parse_tz, tz_label, trunc_id)
from shared.paths import DB_PATH
from shared.table import render

DEFAULT_ROWS = 5      # sources / models / clients / sessions 默认显示的行数；--all 显示全部

SOURCES_HEADERS = ["file name", "records", "added", "dup", "conflict", "trunc",
                   "data from", "data to", "span", "imported at"]
SOURCES_ALIGNS = ["l", "r", "r", "r", "r", "c", "l", "l", "r", "l"]

MODELS_HEADERS = ["model", "provider", "total cost (usd)", "input", "output",
                  "reasoning", "cache read", "cache write", "total", "cancelled", "failed",
                  "cost/req"]
MODELS_ALIGNS = ["r", "l"] + ["r"] * 10
MODELS_GROUPS = [None, None, None,
                 "tokens", "tokens", "tokens", "tokens", "tokens",
                 "requests", "requests", "requests", None]

ACCOUNTS_HEADERS = ["workspace", "account", "api key", "total cost (usd)", "requests",
                    "input", "output", "reasoning", "cache read", "cache write",
                    "cost/req"]
ACCOUNTS_ALIGNS = ["l", "l", "l"] + ["r"] * 8
ACCOUNTS_GROUPS = [None, None, None, None, None,
                   "tokens", "tokens", "tokens", "tokens", "tokens", None]

CLIENTS_HEADERS = ["client", "total cost (usd)", "requests", "input", "output",
                   "reasoning", "cache read", "cache write", "cost/req"]
CLIENTS_ALIGNS = ["l"] + ["r"] * 8
CLIENTS_GROUPS = [None, None, None,
                  "tokens", "tokens", "tokens", "tokens", "tokens", None]

SESSIONS_HEADERS = ["session", "updated", "total cost (usd)", "requests", "input",
                    "output", "reasoning", "cache read", "cache write", "cost/req",
                    "client", "model"]
SESSIONS_ALIGNS = ["l", "l"] + ["r"] * 8 + ["l", "l"]
SESSIONS_GROUPS = [None, None, None, None,
                   "tokens", "tokens", "tokens", "tokens", "tokens", None, None, None]


def days_type(value: str) -> int:
    """--days 的取值：正整数；0 视作 1。"""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"天数必须是整数：{value!r}")
    if number < 0:
        raise argparse.ArgumentTypeError("--days 不能为负数")
    return max(number, 1)


def add_days_arg(sub_parser: argparse.ArgumentParser) -> None:
    """给 models / accounts / clients 添加 --days 选项。"""
    sub_parser.add_argument("--days", type=days_type, default=None, metavar="N",
                            help="只统计最近 N 个自然日（0 视作 1；默认统计全部记录）")


# --days 的长说明：三个子命令的 epilog 共用，改这里即三处同时生效。
DAYS_EPILOG = (
    "\n--days N    只统计最近 N 个自然日：窗口为所选时区今天 00:00 起往前 N 天\n"
    "            （--days 0 视作 1；默认统计全部；TOTAL 按窗口内全量汇总）。\n"
    "            窗口锚定当前时间，同一命令早晚运行结果可能不同。"
)


def days_window(tz, days: int):
    """最近 days 个自然日的窗口：返回 (cutoff_ms, label)。窗口为所选时区今天 00:00 起往前 days 天，调用方用 started_at >= cutoff_ms 过滤。"""
    today = datetime.now(tz).date()
    start = today - timedelta(days=days - 1)
    cutoff_ms = int(datetime(start.year, start.month, start.day, tzinfo=tz).timestamp() * 1000)
    return cutoff_ms, f"last {days} calendar day(s) [{tz_label(tz)}]: {start} ~ {today}"


def resolve_days(args, tz):
    """由 --days 算出 (cutoff_ms, label)；未指定时返回 (None, "")。"""
    if args.days is None:
        return None, ""
    return days_window(tz, args.days)


def title_with_window(base: str, window: str) -> str:
    return f"{base}, {window}" if window else base


def print_no_rows(conn: sqlite3.Connection, tz, window) -> None:
    """空结果提示：区分「库为空」与「窗口内为空」两种情况。"""
    total, latest = conn.execute(
        "SELECT COUNT(*), MAX(started_at) FROM requests").fetchone()
    if not total:
        print("数据库中还没有记录，请先运行 db_import.py。")
        return
    print(f"窗口（{window}）内没有记录。")
    if latest is not None:
        print(f"库内最新记录：{fmt_ms(latest, tz)}；请增大 --days 或导入更新的日志。")

def cmd_sources(conn: sqlite3.Connection, tz, show_all: bool = False) -> int:
    rows = conn.execute(
        "SELECT name, records, inserted, duplicates, conflicts, truncated,"
        "       first_started_at, last_finished_at, imported_at"
        "  FROM imports ORDER BY imported_at DESC, name"
    ).fetchall()
    if not rows:
        print("数据库中还没有导入记录，请先运行 db_import.py。")
        return 0

    all_rows = []
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
        all_rows.append([
            name, records, inserted, dup, conf,
            "yes" if trunc else "no",
            fmt_ms(first, tz), fmt_ms(last, tz),
            fmt_span(first, last),
            fmt_iso(imported_at, tz),
        ])

    table = list(all_rows if show_all else all_rows[:DEFAULT_ROWS])
    if len(table) < len(all_rows):                 # 省略了过长的部分
        table.append(["..."] + [""] * (len(SOURCES_HEADERS) - 1))
    table.append([
        "TOTAL", tot[0], tot[1], tot[2], tot[3], "",
        fmt_ms(g_first, tz), fmt_ms(g_last, tz), fmt_span(g_first, g_last), "",
    ])

    gaps = []
    by_data = sorted(
        ((rec[0], rec[6], rec[7]) for rec in rows
         if rec[6] is not None and rec[7] is not None),
        key=lambda item: (item[1], item[2], item[0]),
    )
    cov_end = cov_name = None
    for name, first, last in by_data:
        if cov_end is None:
            cov_end, cov_name = last, name
        elif first > cov_end:
            gaps.append((cov_name, name, cov_end, first))
            cov_end, cov_name = last, name
        elif last > cov_end:
            cov_end, cov_name = last, name
    print(f"{len(rows)} file(s) imported   [tz: {tz_label(tz)}]\n")
    render(SOURCES_HEADERS, SOURCES_ALIGNS, table, separator_before=len(table) - 1)
    if gaps:
        print("\ngaps (files sorted by data from):")
        for prev_name, curr_name, gap_from, gap_to in gaps:
            print(f"  {prev_name} -> {curr_name}: {fmt_span(gap_from, gap_to)} missing"
                  f" ({fmt_ms(gap_from, tz)} ~ {fmt_ms(gap_to, tz)})")
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


def cmd_models(conn: sqlite3.Connection, tz, show_all: bool = False,
               cutoff_ms=None, window="") -> int:
    where = " WHERE started_at >= ?" if cutoff_ms is not None else ""
    params: tuple = (cutoff_ms,) if cutoff_ms is not None else ()
    # Column layout is positional: the row is sliced as r[3:12] and unpacked into 13
    # names below. Append any new aggregate at the END — inserting one elsewhere would
    # silently shift every following column (nothing raises).
    rows = conn.execute(f"""
        SELECT model,
               COALESCE(provider, '(none)'),
               COALESCE(product, ''),
               SUM(COALESCE(cost_usd, 0)),
               SUM(COALESCE(input_tokens, 0)),
               SUM(COALESCE(output_tokens, 0)),
               SUM(COALESCE(reasoning_tokens, 0)),
               SUM(COALESCE(cache_read_tokens, 0)),
               SUM(COALESCE(cache_write_tokens, 0)),
               COUNT(*),
               SUM(outcome = 'cancelled'),
               SUM(outcome IN ('failed', 'rejected')),
               COALESCE(SUM(cost_usd IS NOT NULL), 0)   -- 被计费的请求数 = cost/req 的分母
          FROM requests{where}
         GROUP BY model, provider, product
    """, params).fetchall()
    if not rows:
        print_no_rows(conn, tz, window)
        return 0

    # 全量汇总（TOTAL 用）：
    # cost, in, out, reasoning, cache_read, cache_write, n, cancelled, failures
    acc = [0.0] + [0] * 8
    tot_billed = 0            # 被计费请求数合计（cost/req 的分母）
    by_model: dict[str, list] = {}
    for r in rows:
        for i, v in enumerate(r[3:12]):
            acc[i] += v
        tot_billed += r[12]
        by_model.setdefault(r[0], []).append(r)

    def total_of(rs, idx):
        return sum(x[idx] for x in rs)

    # 组（model）间：该 model 的总花费 desc → 总请求 desc → model 名
    ordered = sorted(by_model,
                     key=lambda m: (-total_of(by_model[m], 3),
                                    -total_of(by_model[m], 9), m))
    shown = ordered if show_all else ordered[:DEFAULT_ROWS]

    table = []
    for model in shown:
        # 组内：花费 desc → 请求 desc → provider → product
        rs = sorted(by_model[model], key=lambda x: (-x[3], -x[9], x[1], x[2]))
        for k, (_, provider, product, cost, tok_in, tok_out, tok_reason,
                cache_r, cache_w, n, canc, fail, billed) in enumerate(rs):
            if k == 0:                                    # 组的首行写 model 名
                model_cell = model
            else:                                         # 同 model 的后续行用连接符
                model_cell = ("├─" if k < len(rs) - 1 else "└─", "r")
            table.append([
                model_cell, f"{provider}({product})" if product else provider,
                f"{cost:.6f}",
                fmt_int(tok_in), fmt_int(tok_out), fmt_int(tok_reason),
                fmt_int(cache_r), fmt_int(cache_w), fmt_int(n),
                fmt_count_pct(canc, n), fmt_count_pct(fail, n),
                f"{cost / billed:.6f}" if billed else "n/a",
            ])

    if len(shown) < len(ordered):                     # 省略了过长的部分
        table.append(["..."] + [""] * (len(MODELS_HEADERS) - 1))
    table.append([
        "TOTAL", "", f"{acc[0]:.6f}",
        fmt_int(acc[1]), fmt_int(acc[2]), fmt_int(acc[3]), fmt_int(acc[4]), fmt_int(acc[5]),
        fmt_int(acc[6]),
        fmt_count_pct(acc[7], acc[6]), fmt_count_pct(acc[8], acc[6]),
        f"{acc[0] / tot_billed:.6f}" if tot_billed else "n/a",
    ])

    print(title_with_window(f"{len(ordered)} model(s)", window) + "\n")
    render(MODELS_HEADERS, MODELS_ALIGNS, table,
           separator_before=len(table) - 1, groups=MODELS_GROUPS)
    print("\nlegend:\n"
          "  provider  = provider(product)；同一 model 按 provider/product 分别汇总，\n"
          "              同一 model 的后续行以 ├─ / └─ 表示归属\n"
          "  tokens    = 各类型 token 消耗量\n"
          "  requests  = 请求计数：total = 请求总数（去重后）；cancelled = 取消数；\n"
          "              failed = 失败数（failed + rejected）；括号内为占请求总数的比例\n"
          "  cost/req (usd) = 该行总花费 ÷ 被计费的请求数（= JSON 里带 cost 字段的记录数）；\n"
          "              免费模型的成功记录 cost=0 也计入分母，故其均值为 0.000000；\n"
          "              没有可计费请求时显示 n/a")
    return 0


def cmd_accounts(conn: sqlite3.Connection, tz, full_id: bool = False,
                 cutoff_ms=None, window="") -> int:
    where = " WHERE started_at >= ?" if cutoff_ms is not None else ""
    params: tuple = (cutoff_ms,) if cutoff_ms is not None else ()
    # Column layout is positional: the row is unpacked into 11 names below. Append any
    # new aggregate at the END — inserting one elsewhere would silently shift every
    # following column (nothing raises).
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
        "       SUM(COALESCE(cache_write_tokens, 0)),"
        # 被计费的请求数 = cost/req 的分母（注意：这是 Python 注释，不能写 SQL 的 --，
        # 否则隐式拼接后会把后面的 FROM requests 一起注释掉）
        "       COALESCE(SUM(cost_usd IS NOT NULL), 0)"
        f"  FROM requests{where}"
        " GROUP BY workspace_id, user_id, service_account_id, service_api_key_id"
        " ORDER BY workspace_id, SUM(COALESCE(cost_usd, 0)) DESC,"
        "          COALESCE(user_id, service_account_id)",
        params).fetchall()
    if not rows:
        print_no_rows(conn, tz, window)
        return 0

    if full_id:                                  # 只列 ID，不打印表格
        tree: dict[str, dict[str, list[str]]] = {}
        for ws, account, key, *_ in rows:
            tree.setdefault(ws, {}).setdefault(account, [])
            if key:
                tree[ws][account].append(key)
        print(title_with_window(f"{len(tree)} workspace(s)", window) + "\n")
        for ws, accounts in tree.items():
            print(f"workspace  {ws}")
            for account, keys in accounts.items():
                print(f"  account  {account}")
                for key in keys:
                    print(f"    api key  {key}")
        return 0

    # cost, requests, input, output, reasoning, cache_read, cache_write
    acc = [0.0] + [0] * 6
    tot_billed = 0            # 被计费请求数合计（cost/req 的分母）
    table = []
    for idx, (ws, account, key, cost, n, tok_in, tok_out, tok_reason, cache_r, cache_w,
              billed) in enumerate(rows):
        acc[0] += cost
        acc[1] += n
        tot_billed += billed
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
            f"{cost / billed:.6f}" if billed else "n/a",
        ])
    table.append([
        "TOTAL", "", "", f"{acc[0]:.6f}", fmt_int(acc[1]),
        fmt_int(acc[2]), fmt_int(acc[3]), fmt_int(acc[4]), fmt_int(acc[5]), fmt_int(acc[6]),
        f"{acc[0] / tot_billed:.6f}" if tot_billed else "n/a",
    ])

    n_ws = len({r[0] for r in rows})
    n_acct = len({(r[0], r[1]) for r in rows})
    n_key = sum(1 for r in rows if r[2])
    print(title_with_window(f"{n_ws} workspace(s) | {n_acct} account(s) | {n_key} key(s)", window) + "\n")
    render(ACCOUNTS_HEADERS, ACCOUNTS_ALIGNS, table,
           separator_before=len(table) - 1, groups=ACCOUNTS_GROUPS)
    print("\nlegend:\n"
          "  workspace = 同一 workspace 只在首行打印 ID，后续行以 ├─ / └─ 表示归属\n"
          "  account   = user_id 或 service_account_id（原始 ID）\n"
          "  api key   = service_api_key_id；user 行没有密钥，显示 —\n"
          "  tokens    = 各类型 token 消耗量\n"
          "  cost/req (usd) = 该行总花费 ÷ 被计费的请求数（= JSON 里带 cost 字段的记录数）；\n"
          "              ⚠️ 分母**不是**同行的 requests 列（那是全部请求数 COUNT(*)），\n"
          "              故 cost/req 会大于「总花费 ÷ requests」；免费模型计 0 也算被计费\n"
          "              （均值 0.000000）；本行没有可计费请求时显示 n/a\n"
          "  ID 已截断显示（保留前缀与尾部）；用 --full-id 查看完整 ID")
    return 0


def cmd_clients(conn: sqlite3.Connection, tz, show_all: bool = False,
                cutoff_ms=None, window="") -> int:
    where = " WHERE started_at >= ?" if cutoff_ms is not None else ""
    params: tuple = (cutoff_ms,) if cutoff_ms is not None else ()
    # 先按 (app, user_agent) 在 SQL 里汇总（取值很少），再在 Python 里归并成客户端名，
    # 因为同一个客户端对应多种 user_agent（版本号不同）。
    rows = conn.execute(f"""
        SELECT COALESCE(app, ''),
               COALESCE(user_agent, ''),
               SUM(COALESCE(cost_usd, 0)),
               COUNT(*),
               SUM(COALESCE(input_tokens, 0)),
               SUM(COALESCE(output_tokens, 0)),
               SUM(COALESCE(reasoning_tokens, 0)),
               SUM(COALESCE(cache_read_tokens, 0)),
               SUM(COALESCE(cache_write_tokens, 0)),
               COALESCE(SUM(cost_usd IS NOT NULL), 0)   -- 被计费的请求数 = cost/req 的分母
          FROM requests{where}
         GROUP BY app, user_agent
    """, params).fetchall()
    if not rows:
        print_no_rows(conn, tz, window)
        return 0

    # cost, n, in, out, reasoning, cache_read, cache_write, billed
    acc: dict[str, list] = {}
    for app, ua, cost, n, tok_in, tok_out, tok_reason, cache_r, cache_w, billed in rows:
        a = acc.setdefault(client_name(app, ua), [0.0, 0, 0, 0, 0, 0, 0, 0])
        a[0] += cost
        a[1] += n
        for i, v in enumerate((tok_in, tok_out, tok_reason, cache_r, cache_w)):
            a[2 + i] += v
        a[7] += billed

    # 客户端之间：总花费 desc → 请求 desc → 名字
    ordered = sorted(acc.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    tot = [0.0] + [0] * 7
    for _, a in ordered:
        for i, v in enumerate(a):
            tot[i] += v

    picked = ordered if show_all else ordered[:DEFAULT_ROWS]
    table = []
    for name, (cost, n, tok_in, tok_out, tok_reason, cache_r, cache_w, billed) in picked:
        table.append([
            name, f"{cost:.6f}", fmt_int(n),
            fmt_int(tok_in), fmt_int(tok_out), fmt_int(tok_reason),
            fmt_int(cache_r), fmt_int(cache_w),
            f"{cost / billed:.6f}" if billed else "n/a",
        ])

    if len(picked) < len(ordered):                    # 省略了过长的部分
        table.append(["..."] + [""] * (len(CLIENTS_HEADERS) - 1))
    table.append([
        "TOTAL", f"{tot[0]:.6f}", fmt_int(tot[1]),
        fmt_int(tot[2]), fmt_int(tot[3]), fmt_int(tot[4]), fmt_int(tot[5]), fmt_int(tot[6]),
        f"{tot[0] / tot[7]:.6f}" if tot[7] else "n/a",
    ])

    print(title_with_window(f"{len(ordered)} client(s)", window) + "\n")
    render(CLIENTS_HEADERS, CLIENTS_ALIGNS, table,
           separator_before=len(table) - 1, groups=CLIENTS_GROUPS)
    print("\nlegend:\n"
          "  client    = 由 user_agent 首段推导（去掉版本号）；opencode 的 desktop / cli\n"
          "              合并为一行，openai 的 SDK 记为 openai-<语言>；无 user_agent 时回退到 app 列\n"
          "  tokens    = 各类型 token 消耗量\n"
          "  requests  = 请求总数（去重后）\n"
          "  cost/req (usd) = 该行总花费 ÷ 被计费的请求数（= JSON 里带 cost 字段的记录数）；\n"
          "              免费模型计 0 也算被计费（均值 0.000000）；没有可计费请求时显示 n/a")
    return 0


def cmd_sessions(conn: sqlite3.Connection, tz, show_all: bool = False,
                 cutoff_ms=None, window="") -> int:
    where = " WHERE started_at >= ?" if cutoff_ms is not None else ""
    params: tuple = (cutoff_ms,) if cutoff_ms is not None else ()
    # Column layout is positional: r[1:8] is cost + requests + five token types,
    # r[8] is the billed-request count used as the cost/req denominator.
    rows = conn.execute(f"""
        SELECT COALESCE(session_id, '(none)'),
               SUM(COALESCE(cost_usd, 0)),
               COUNT(*),
               SUM(COALESCE(input_tokens, 0)),
               SUM(COALESCE(output_tokens, 0)),
               SUM(COALESCE(reasoning_tokens, 0)),
               SUM(COALESCE(cache_read_tokens, 0)),
               SUM(COALESCE(cache_write_tokens, 0)),
               COALESCE(SUM(cost_usd IS NOT NULL), 0)   -- 被计费的请求数 = cost/req 的分母
          FROM requests{where}
         GROUP BY COALESCE(session_id, '(none)')
         ORDER BY SUM(COALESCE(cost_usd, 0)) DESC, COUNT(*) DESC,
                  COALESCE(session_id, '(none)')
    """, params).fetchall()
    if not rows:
        print_no_rows(conn, tz, window)
        return 0

    # 每个 session 在统计窗口内最新的一条请求：client / model 取这一行。
    latest_rows = conn.execute(f"""
        SELECT COALESCE(session_id, '(none)'), app, user_agent, model, started_at
          FROM (
                SELECT session_id, app, user_agent, model, started_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id
                           ORDER BY started_at DESC, finished_at DESC, request_id DESC
                       ) AS rn
                  FROM requests{where}
               )
         WHERE rn = 1
    """, params).fetchall()
    latest = {session: (client_name(app, user_agent), model or "(none)", started_at)
              for session, app, user_agent, model, started_at in latest_rows}

    # cost, requests, input, output, reasoning, cache_read, cache_write, billed
    tot = [0.0] + [0] * 7
    for r in rows:
        for i, v in enumerate(r[1:8]):
            tot[i] += v
        tot[7] += r[8]

    picked = rows if show_all else rows[:DEFAULT_ROWS]
    table = []
    for session, cost, n, tok_in, tok_out, tok_reason, cache_r, cache_w, billed in picked:
        client, model, updated_at = latest.get(session, ("(unknown)", "(none)", None))
        table.append([
            trunc_id(session), fmt_ms(updated_at, tz), f"{cost:.6f}", fmt_int(n),
            fmt_int(tok_in), fmt_int(tok_out), fmt_int(tok_reason),
            fmt_int(cache_r), fmt_int(cache_w),
            f"{cost / billed:.6f}" if billed else "n/a",
            client, model,
        ])

    if len(picked) < len(rows):                       # 省略了过长的部分
        table.append(["..."] + [""] * (len(SESSIONS_HEADERS) - 1))
    table.append([
        "TOTAL", "", f"{tot[0]:.6f}", fmt_int(tot[1]),
        fmt_int(tot[2]), fmt_int(tot[3]), fmt_int(tot[4]), fmt_int(tot[5]), fmt_int(tot[6]),
        f"{tot[0] / tot[7]:.6f}" if tot[7] else "n/a",
        "", "",
    ])

    print(title_with_window(f"{len(rows)} session(s)", window) + "\n")
    render(SESSIONS_HEADERS, SESSIONS_ALIGNS, table,
           separator_before=len(table) - 1, groups=SESSIONS_GROUPS)
    print("\nlegend:\n"
          "  session   = session_id（默认截断显示；空值显示 (none)）\n"
          "  tokens    = 各类型 token 消耗量\n"
          "  requests  = 请求总数（去重后）\n"
          "  cost/req (usd) = 该行总花费 ÷ 被计费的请求数（= JSON 里带 cost 字段的记录数）；\n"
          "              免费模型计 0 也算被计费（均值 0.000000）；没有可计费请求时显示 n/a\n"
          "  updated   = 该 session 在统计窗口内最新一条请求的开始时间（按 --tz 显示，精确到分钟）\n"
          "  client    = 该 session 在统计窗口内最新一条请求的客户端（口径同 clients）\n"
          "  model     = 同一最新请求的 model；空值显示 (none)")
    return 0


def common_args(with_tz: bool = True) -> argparse.ArgumentParser:
    """子命令共享的选项：--db（所有子命令）与 --tz（显示时间与 --days 窗口划分的时区）。"""
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
    src_p = sub.add_parser(
        "sources",
        parents=[common_args()],
        help="列出已导入的文件及其记录数、去重统计、数据覆盖时间范围",
        description="列出已导入的文件：文件名、记录数、去重统计、是否被截断、"
                    "数据覆盖时间范围，以及导入本数据库的时间。"
                    f"默认只列出最近导入的 {DEFAULT_ROWS} 个文件，用 --all 查看全部。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py sources\n"
            "  uv run python db_query.py sources --all\n"
            "  uv run python db_query.py sources --tz UTC\n"
            "  uv run python db_query.py sources --db other.db\n\n"
            "时间按 --tz 指定的时区显示（默认使用本机时区）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src_p.add_argument("--all", action="store_true",
                       help=f"打印完整表格（默认只显示最近导入的 {DEFAULT_ROWS} 个文件）")
    mod_p = sub.add_parser(
        "models",
        parents=[common_args()],
        help="列出花费最高的 model 的花费、token 消耗与失败率",
        description="按 model + provider + product 分组汇总，列出总花费（USD）、"
                    "各类 token 消耗（input/output/reasoning/cache read/cache write）"
                    "与取消数、失败数（各带占比），"
                    "以及平均单次花费（该行总花费 ÷ 被计费的请求数）。"
                    "provider 列打印为 provider(product)；同一 model 的后续行以 ├─ / └─ "
                    "表示归属。默认只列出总花费最高的 "
                    f"{DEFAULT_ROWS} 个 model（其下所有 provider/product 行都会显示），"
                    "用 --all 查看全部。"
                    "可用 --days N 只看最近 N 个自然日（按 --tz 时区划分）。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py models\n"
            "  uv run python db_query.py models --all\n"
            "  uv run python db_query.py models --db other.db\n\n"
            "排序：model 之间按该 model 的总花费从高到低（相同再按请求数），\n"
            "同一 model 内按各行花费从高到低（相同再按请求数）。\n"
            "取消数 = cancelled；失败数 = failed + rejected；"
            "括号内为占该行请求总数的比例。\n"
            "cost/req = 该行总花费 ÷ **被计费**的请求数（JSON 里带 cost 字段的记录数）；\n"
            "免费模型的成功记录 cost=0 也计入分母（均值为 0.000000），无可计费请求时为 n/a。"
        ) + DAYS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mod_p.add_argument("--all", action="store_true",
                       help=f"打印完整表格（默认只显示总花费最高的 {DEFAULT_ROWS} 个 model）")
    add_days_arg(mod_p)
    acc_p = sub.add_parser(
        "accounts",
        parents=[common_args()],
        help="按 workspace / 账户 / API Key 列出花费、请求数、token 消耗与 cost/req",
        description="按 workspace / 账户（user_id 或 service_account_id）/ API Key 三层，"
                    "列出各主体的总花费（USD）、请求总数、各类 token 消耗，"
                    "以及平均单次花费（该行总花费 ÷ 被计费的请求数）。"
                    "user 行没有 API Key，以 — 表示。"
                    "可用 --days N 只看最近 N 个自然日（按 --tz 时区划分）。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py accounts\n"
            "  uv run python db_query.py accounts --full-id\n"
            "  uv run python db_query.py accounts --db other.db\n\n"
            "账户 = COALESCE(user_id, service_account_id)；"
            "每行是该主体（或它的一把密钥）的汇总。\n"
            "cost/req = 该行总花费 ÷ **被计费**的请求数（JSON 里带 cost 字段的记录数），\n"
            "   注意分母不是同行的 requests 列（那是 COUNT(*)）；无可计费请求时为 n/a。\n"
            "默认将 ID 截断显示（保留前缀与尾部）；--full-id 改为只打印完整 ID。"
        ) + DAYS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    acc_p.add_argument("--full-id", action="store_true",
                       help="不打印表格，只打印每个 workspace 下的完整 ID")
    add_days_arg(acc_p)
    cli_p = sub.add_parser(
        "clients",
        parents=[common_args()],
        help="按客户端（user_agent）列出花费、请求数、token 消耗与 cost/req",
        description="按调用 OpenCode API 的客户端汇总：总花费（USD）、请求总数、"
                    "各类 token 消耗（input/output/reasoning/cache read/cache write）"
                    "与平均单次花费（该行总花费 ÷ 被计费的请求数）。"
                    "客户端名取自 user_agent 首段并去掉版本号：opencode 的 desktop / cli "
                    "合并为一行，openai 的 SDK 记为 openai-<语言>；"
                    f"默认只列出总花费最高的 {DEFAULT_ROWS} 个，用 --all 查看全部。"
                    "可用 --days N 只看最近 N 个自然日（按 --tz 时区划分）。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py clients\n"
            "  uv run python db_query.py clients --all\n"
            "  uv run python db_query.py clients --db other.db\n\n"
            "排序：按总花费从高到低（相同再按请求数）；"
            "TOTAL 按时间窗口内全量汇总（无 --days 时即全库）。\n"
            "cost/req = 该行总花费 ÷ **被计费**的请求数（JSON 里带 cost 字段的记录数），\n"
            "免费模型计 0 也计入分母；无可计费请求时为 n/a。"
        ) + DAYS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cli_p.add_argument("--all", action="store_true",
                       help=f"打印完整表格（默认只显示总花费最高的 {DEFAULT_ROWS} 个客户端）")
    add_days_arg(cli_p)
    ses_p = sub.add_parser(
        "sessions",
        parents=[common_args()],
        help="按 session ID 列出花费、请求数、token 消耗与 cost/req",
        description="按 session_id 汇总：总花费（USD）、请求总数、"
                    "各类 token 消耗（input/output/reasoning/cache read/cache write）"
                    "与平均单次花费（该行总花费 ÷ 被计费的请求数）；"
                    "updated 列是该 session 在统计窗口内最新一条请求的开始时间，"
                    "最后两列是同一请求使用的 client 与 model。"
                    f"默认只列出总花费最高的 {DEFAULT_ROWS} 个 session，用 --all 查看全部。"
                    "可用 --days N 只看最近 N 个自然日（按 --tz 时区划分）。",
        epilog=(
            "示例:\n"
            "  uv run python db_query.py sessions\n"
            "  uv run python db_query.py sessions --all\n"
            "  uv run python db_query.py sessions --days 7\n"
            "  uv run python db_query.py sessions --db other.db\n\n"
            "排序：按总花费从高到低（相同再按请求数）；"
            "TOTAL 按时间窗口内全量汇总（无 --days 时即全库）。\n"
            "updated / client / model 取该 session 在窗口内最新一条请求（按 started_at，"
            "并列时依次按 finished_at、request_id）的值；updated 精确到分钟。\n"
            "cost/req = 该行总花费 ÷ **被计费**的请求数（JSON 里带 cost 字段的记录数），\n"
            "免费模型计 0 也计入分母；无可计费请求时为 n/a。"
        ) + DAYS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ses_p.add_argument("--all", action="store_true",
                       help=f"打印完整表格（默认只显示总花费最高的 {DEFAULT_ROWS} 个 session）")
    add_days_arg(ses_p)
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
        return cmd_sources(conn, parse_tz(args.tz), show_all=args.all)
    finally:
        conn.close()


def run_models(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        tz = parse_tz(args.tz)
        cutoff_ms, window = resolve_days(args, tz)
        return cmd_models(conn, tz, show_all=args.all, cutoff_ms=cutoff_ms, window=window)
    finally:
        conn.close()


def run_accounts(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        tz = parse_tz(args.tz)
        cutoff_ms, window = resolve_days(args, tz)
        return cmd_accounts(conn, tz, full_id=args.full_id, cutoff_ms=cutoff_ms, window=window)
    finally:
        conn.close()


def run_clients(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        tz = parse_tz(args.tz)
        cutoff_ms, window = resolve_days(args, tz)
        return cmd_clients(conn, tz, show_all=args.all, cutoff_ms=cutoff_ms, window=window)
    finally:
        conn.close()


def run_sessions(args) -> int:
    conn = connect_ro(args)
    if conn is None:
        return 2
    try:
        tz = parse_tz(args.tz)
        cutoff_ms, window = resolve_days(args, tz)
        return cmd_sessions(conn, tz, show_all=args.all, cutoff_ms=cutoff_ms, window=window)
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
    if args.command == "clients":
        return run_clients(args)
    if args.command == "sessions":
        return run_sessions(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
