"""显示格式化：数值、时间、时区与 ID。

这些函数只负责「把值变成显示字符串」，不依赖本项目的任何业务结构。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ------------------------------------------------------------------ 数值 / 计数
def fmt_int(v: int) -> str:
    """整数加千分位，如 1234567 -> '1,234,567'。"""
    return f"{v:,}"


def fmt_pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.2f}%" if den else "n/a"


def fmt_count_pct(num: int, den: int) -> str:
    """计数 + 占比，如 '3 (0.19%)'。"""
    return f"{num} ({fmt_pct(num, den)})"


def trunc_id(s, width: int = 20) -> str:
    """ID 截断显示：保留前缀与尾部，中间用 … 代替（默认总宽 20 字符）；空值显示 —。"""
    if not s:
        return "—"
    if len(s) <= width:
        return s
    return s[: width - 6] + "…" + s[-5:]


# ------------------------------------------------------------------ 客户端
def client_name(app, user_agent) -> str:
    """从 app / user_agent 推导客户端名（去掉版本号）。

    取 user_agent 首段：`opencode/latest/2.0.26/desktop` -> `opencode`，
    `OpenAI/JS 7.23.0` -> `openai-js`，`Codex Desktop/0.163.0` -> `codex-desktop`。
    opencode 的 desktop / cli 变体合并为一行；缺 user_agent 时回退到 app 列，
    再回退到 (unknown)。
    """
    ua = (user_agent or "").strip()
    if ua:
        head, _, rest = ua.partition("/")
        head = head.strip().lower()
        if head == "opencode":
            return "opencode"
        if head == "openai":
            lang = rest.split()[0].lower() if rest else ""
            return f"openai-{lang}" if lang else "openai"
        return head.replace(" ", "-")
    app = (app or "").strip().lower()
    # app 只有 cli / desktop，是 opencode 的两个前端，与上面的合并口径保持一致
    return "opencode" if app in ("cli", "desktop") else app or "(unknown)"


# ------------------------------------------------------------------ 时间 / 时区
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
    """epoch 毫秒 -> 'YYYY-MM-DD HH:MM'（tz 为 None 时用本机时区）。"""
    if ms is None:
        return "n/a"
    dt = datetime.fromtimestamp(ms / 1000, tz) if tz else datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_span(a, b) -> str:
    """两个 epoch 毫秒之间的跨度，如 '22.2h' / '2.9d'。"""
    if a is None or b is None:
        return "n/a"
    hours = (b - a) / 3_600_000
    return f"{hours / 24:.1f}d" if hours >= 48 else f"{hours:.1f}h"


def fmt_iso(s, tz) -> str:
    """ISO-8601 UTC 字符串 -> 'YYYY-MM-DD HH:MM'（tz 为 None 时用本机时区）。"""
    if not s:
        return "n/a"
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(s)
    dt = dt.astimezone(tz) if tz else dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")
