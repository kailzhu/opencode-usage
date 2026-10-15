# opencode-usage

把多个 OpenCode 用量 JSON 日志导入**单个 SQLite 数据库**，按 `request_id` 去重，并按模型分析用量与花费。

## 目录结构

```
opencode-usage/
├─ logs/                       原始 JSON 日志（手动放入，只读；已 gitignore）
├─ tmp/                        临时脚本与草稿（已 gitignore）
├─ schema.sql                  建表 + 索引，由 db_import.py 自动执行
├─ db_import.py                导入器：幂等导入、去重、台账、冲突登记
├─ db_query.py                 查询工具：sources / models 子命令
├─ usage.db                    SQLite 数据库（生成物；已 gitignore）
└─ README.md
```

## 常用命令

```pwsh
# 导入（不指定 --file 时，扫描 logs/ 下全部 *.json）
uv run python db_import.py

# 只导入指定文件（可多个，可重复使用 --file）
uv run python db_import.py --file logs/a.json logs/b.json

# 只解析统计、不写库
uv run python db_import.py --dry-run

# 查看已导入的文件、去重统计与数据覆盖范围
uv run python db_query.py sources
uv run python db_query.py sources --tz UTC
uv run python db_query.py sources --db other.db

# 列出所有 model 的花费、token 消耗与请求/取消/失败数
uv run python db_query.py models
```

## TODO / 开发步骤

- [x] **1. 分析 `logs/` 中 JSON 的数据结构**（说明文档含私有数据，未纳入仓库）
- [x] **2. 固化数据模型与指标口径** → `schema.sql`
- [x] **3. 实现导入器**（sha256 幂等 / `requestID` 去重 / 冲突台账）→ `db_import.py`
- [x] **4. 查询工具**（已导入文件与数据覆盖范围；按 model 的花费、各类 token 消耗、请求/取消/失败数）→ `db_query.py`（`sources` / `models`）
- [ ] **5. 按模型的聚合报表** → `db_analyze.py`：一期输出
      (1) 按 `model` 主表（附按 `product` 拆分），(2) 总体汇总；支持 `--csv`、`--tz`
- [ ] **6. 校验与异常报告（延后）**：attempts 重试分布 / 尝试级失败率、异常清单（如 `succeeded` 却无 `cost`）
- [ ] **7. 模型价格表 / 理论成本对照（延后）**

## 导入规则

1. 扫描 `logs/*.json`，逐个读取 `items` 记录。
2. 以**文件内容 `sha256`** 判幂等：已导入过的文件直接跳过（重命名/移动也能识别）。
3. **去重**，以 `request_id` 为键，用全字段规范化哈希 `record_hash` 校验一致性：
   - 同 `request_id` 且 `record_hash` 相同 → 重复，跳过；
   - 同 `request_id` 但哈希不同 → **冲突**，记入 `conflicts` 表，**保留首次、不覆盖、不中断**。
4. `attempts[]` 展开为子表，随父记录一起写入或跳过。
5. `day_utc` 在导入时按 UTC 由 `startedAt` 推导；源时间存 epoch 毫秒。
6. 每个文件登记一行台账（`imports`），记录 `truncated`/`until`/数据覆盖范围等元信息（仅存档，不用于过滤——`--complete-only` 已取消）。

导入是**幂等的**，可重复运行；写入顺序不影响最终去重结果。

## 数据模型

完整列定义见 `schema.sql`。

- **`requests`**（PK `request_id`）— 一次 request 一行。顶层标量字段全部成列（含 `model`/`requested_model`/`outcome`/`status_code`/`cost_usd`/各类 token/`day_utc`/`source_file` 等）；`requestHeaders`、`metadata` 只保留在 `raw`（完整 JSON）中。
- **`attempts`**（PK `(request_id, attempt_index)`）— 一次尝试一行：`provider`/`model`/`status_code`/`duration_ms`/`error_code`。
- **`imports`**（PK `sha256`）— 导入台账：文件名/路径/字节数/导入时间/记录数/新增/重复/冲突/`truncated`/`until`/数据覆盖起止。
- **`conflicts`**（PK `(request_id, other_hash)`）— 同 ID 但内容不一致的记录（当前为空）。

> `cost_usd` 为 `REAL`，直接存原始 `cost`（单位美元），缺值即 `NULL`。

## 分析口径（按 `model`）

| 指标 | 定义 |
|---|---|
| 成功率 | `SUM(outcome='succeeded') / COUNT(*)`，另出按 `outcome` 与 `status_code` 的分解 |
| 平均单次花费（主） | `SUM(COALESCE(cost_usd,0)) / COUNT(*)`（失败请求花费计 0） |
| 平均单次花费（辅） | `SUM(cost_usd) / 成功请求数` |
| cache 命中率 | `SUM(cache_read_tokens) / (SUM(input_tokens) + SUM(cache_read_tokens) + SUM(cache_write_tokens))` |

`db_query.py` 的时间按 `--tz` 显示（默认本机时区，表头标注 `UTC+xx`）。

## 约定

- 运行：`uv run python ...`
- 仅用 Python 标准库（`sqlite3`、`json`），不引入第三方依赖。
- `logs/`、`usage.db`、`tmp/` 不入版本控制（见 `.gitignore`；日志含 `sk_`/`acc_`/`wrk_` 等敏感标识）。
