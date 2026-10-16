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
    groups            : 每列所属的分组名。给了 groups 时表头分两行：
                        第 1 行 = 无分组列的列名 + 各分组的组名（横跨其子列），
                        第 2 行 = 只给分组内各子列的列名（无分组列留空）。
    """
    def text_of(v):
        return v[0] if isinstance(v, tuple) else v

    if groups is not None and len(groups) != len(headers):
        raise ValueError("groups must have the same length as headers "
                         f"(got {len(groups)} groups vs {len(headers)} headers)")

    def align_of(v, default):
        return v[1] if isinstance(v, tuple) and len(v) > 1 else default

    widths = []
    for i, h in enumerate(headers):
        cells = [len(str(text_of(r[i]))) for r in rows] or [0]
        widths.append(max(len(str(h)), *cells))

    def rule() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def pad(s: str, i: int) -> str:
        a = aligns[i]
        if a == "r":
            return s.rjust(widths[i])
        if a == "c":
            return s.center(widths[i])
        return s.ljust(widths[i])

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

    def top_line() -> str:
        """第 1 行表头：无分组列写列名，分组列写组名（横跨子列，组内不画竖线）。"""
        out, i = [], 0
        while i < len(headers):
            g = groups[i]
            j = i
            if g is not None:                 # 只有真正的分组才合并相邻列
                while j + 1 < len(headers) and groups[j + 1] == g:
                    j += 1
            if g is None:
                out.append("| " + pad(str(headers[i]), i) + " ")
            else:
                area = sum(widths[i:j + 1]) + 2 + 3 * (j - i)
                out.append("|" + str(g).center(area))
            i = j + 1
        return "".join(out) + "|"

    def sub_line() -> str:
        """第 2 行表头：无分组列留空，分组列写自己的列名。"""
        cells = [pad(str(h), i) if groups[i] is not None else " " * widths[i]
                 for i, h in enumerate(headers)]
        return "| " + " | ".join(cells) + " |"

    print(rule())
    if groups:
        print(top_line())
        print(sub_line())
    else:
        print(row_line(headers))
    print(rule())
    for i, r in enumerate(rows):
        if separator_before is not None and i == separator_before:
            print(rule())
        print(row_line(r))
    print(rule())
