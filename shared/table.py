"""终端表格渲染（网格样式：`+---+` 边框与 `|` 竖线）。

单元格可以是普通值，也可以是 `(文本, 对齐)` 二元组，用来覆盖该列的默认对齐。
"""
from __future__ import annotations


def render(headers, aligns, rows, separator_before=None, groups=None) -> None:
    """打印一张网格表格。

    headers           : 列名
    aligns            : 每列对齐，'l' / 'r' / 'c'
    rows              : 行数据（值或 (值, 对齐) 元组）
    separator_before  : 在该下标行之前多画一条分隔线（常用于 TOTAL 行）
    groups            : 每列所属的分组名；同一分组内不画竖线，组名横跨其上
    """
    def text_of(v):
        return v[0] if isinstance(v, tuple) else v

    def align_of(v, default):
        return v[1] if isinstance(v, tuple) and len(v) > 1 else default

    widths = []
    for i, h in enumerate(headers):
        cells = [len(str(text_of(r[i]))) for r in rows] or [0]
        widths.append(max(len(str(h)), *cells))

    def rule() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def row_line(vals) -> str:
        cells = []
        for i, v in enumerate(vals):
            s = str(text_of(v))
            a = align_of(v, aligns[i])
            if a == "r":
                s = s.rjust(widths[i])
            elif a == "c":
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
