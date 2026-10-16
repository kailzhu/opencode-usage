"""项目路径：所有默认路径的唯一来源。

放在 `shared/` 下，因此项目根是上一级目录（多一层 `.parent`）。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DB_PATH = ROOT / "usage.db"        # SQLite 数据库
LOGS_DIR = ROOT / "logs"           # 原始 JSON 日志目录
SCHEMA_PATH = ROOT / "schema.sql"  # 建表脚本
