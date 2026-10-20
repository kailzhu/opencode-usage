# opencode-usage

[English](README.md) | [简体中文](README.zh-CN.md)

把多个 OpenCode 用量 JSON 日志导入**单个 SQLite 数据库**，按 `request_id` 去重，并按模型分析用量与花费。

## 目录结构

```
opencode-usage/
├─ logs/                       原始 JSON 日志（手动放入，只读；已 gitignore）
├─ schema.sql                  建表 + 索引，由 db_import.py 自动执行
├─ db_import.py                导入器：幂等导入、去重、台账、冲突登记
├─ db_query.py                 查询工具：sources / models / accounts 子命令
├─ shared/                     共享模块：表格渲染与格式化
└─ usage.db                    SQLite 数据库（生成物；已 gitignore）
```

## 常用命令

```pwsh
# 导入（不指定 --file 时，扫描 logs/ 下全部 *.json）
uv run python db_import.py

# 只导入指定文件（可多个，可重复使用 --file）
uv run python db_import.py --file logs/a.json logs/b.json

# 只解析统计、不写库
uv run python db_import.py --dry-run

# 查询命令（都支持 --all 与 --db；sources 还支持 --tz）
uv run python db_query.py sources [--all] [--tz UTC] [--db other.db]
uv run python db_query.py models [--all] [--db other.db]
uv run python db_query.py accounts [--full-id] [--db other.db]
```

查询命令默认只列前 5 行——分别是最近导入的文件（`sources`）、总花费最高的模型
（`models`，花费相同时按请求数从多到少）。明细与 `TOTAL` 之间会有一行 `...` 表示省略；
`--all` 打印完整表格。`TOTAL` 行**始终按全量数据汇总**，不受显示行数影响。
`accounts --full-id` 只打印完整 ID 的缩进树，不打印表格。

## TODO / 开发步骤

- [x] 导入 + 查询（`schema.sql`、`db_import.py`、`db_query.py` 的 `sources` / `models` / `accounts`）
- [ ] **按模型的聚合报表** → `db_analyze.py`：一期输出
      (1) 按 `model` 主表（附按 `product` 拆分），(2) 总体汇总；支持 `--csv`、`--tz`
- [ ] **校验与异常报告（延后）**：attempts 重试分布 / 尝试级失败率、异常清单（如 `succeeded` 却无 `cost`）
- [ ] **模型价格表 / 理论成本对照（延后）**

## 导入规则

1. 扫描 `logs/*.json`，逐个读取 `items` 记录。
2. 以**文件内容 `sha256`** 判幂等：已导入过的文件**不再写入**（重命名/移动也能识别）；
   但仍会逐条与库内比对，以便报告 `dup` / `conflict`（此时 `added` 为 0），台账行的
   `imported_at` 保持首次导入时间，汇总行带 `(skipped)` 后缀。
3. **去重**，以 `request_id` 为键，用全字段规范化哈希 `record_hash` 校验一致性：
   哈希相同 → 重复，跳过；哈希不同 → **冲突**，记入 `conflicts` 表，
   **保留首次、不覆盖、不中断**。`attempts[]` 展开为子表，随父记录一起写入或跳过。
4. `day_utc` 在导入时按 UTC 由 `startedAt` 推导；源时间存 epoch 毫秒。
5. 每个文件登记一行台账（`imports`），仅存档，不用于过滤。

导入是**幂等的**，可重复运行；写入顺序不影响最终去重结果。
每次运行结束后打印一张汇总表，之后还有一行 `Total records in database: N`（库内去重后的请求总数）。

## 数据模型

完整列定义见 `schema.sql`。

- **`requests`**（PK `request_id`）— 一次 request 一行；顶层标量字段全部成列，完整 JSON 存于 `raw`
 （`requestHeaders`、`metadata` 只保留在其中）。
- **`attempts`**（PK `(request_id, attempt_index)`）— 一次尝试一行。
- **`imports`**（PK `sha256`）— 导入台账：文件、字节数、导入时间、记录/新增/重复/冲突数、数据覆盖范围。
- **`conflicts`**（PK `(request_id, other_hash)`）— 同 ID 但内容不一致的记录，保留首次写入。

> `cost_usd` 为 `REAL`，直接存原始 `cost`（单位美元），缺值即 `NULL`。

## 分析口径

- `cancelled` 即 `cancelled`；`failed` 即 `failed + rejected`（各带占该行请求总数的比例）。
- **`cost/req`** = `SUM(cost_usd) / COUNT(cost_usd)`——分母仅为被计费的请求
  （带 `cost` 字段的记录；免费模型计 0 也算被计费，缺值 `NULL` 不算）。
  分母为 0 时显示 `n/a`。在 `accounts` 里**这个分母不是它旁边的 `requests` 列**（那是 `COUNT(*)`）。
  `TOTAL` 行使用全局比值，而非各行均值的平均。
- `models` 按 `(model, provider, product)` 分组，打印为 `provider(product)`，
  按总花费（相同再按请求数）排序。`accounts` 按 `workspace_id` /
  `COALESCE(user_id, service_account_id)` / API Key 分组；user 行的 Key 显示为 `—`。

`db_query.py` 的时间按 `--tz` 显示（默认本机时区，表头标注 `UTC+xx`）。

## 约定

- 运行：`uv run python ...`
- 仅用 Python 标准库（`sqlite3`、`json`），不引入第三方依赖。
- `logs/`（含敏感标识）、`tmp/`（agent 临时工作区）与 SQLite 数据库（`*.db`，含 `-wal`/`-shm` 边车文件）不入版本控制
  （见 `.gitignore`）。
