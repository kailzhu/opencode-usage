"""共享模块：终端表格渲染与显示格式化，供 db_import.py / db_query.py 复用。

目录名用 `shared/`（而不是 `lib/`）是为了避开 Python 标准 .gitignore 模板里的
`lib/` 规则——否则这里的内容会被 git 忽略、无法提交。
"""
