# 批量入库中的坏行二分隔离与约束保持

供应方一批数据里少数坏行会让整批 `COPY` 失败。本工具用 **Python 3.11 + 本机
PostgreSQL 16 + 原生 psycopg3 COPY** 实现：保留所有满足数据库约束的合法行，
精确定位每条坏行并记录其来源（表 / 文件 / 逻辑行号 / 主键 / SQLSTATE / 约束名），
全程不做 Python 侧近似校验，一切接受/拒绝由真实数据库约束裁决。

## 一条命令演示

```bash
./demo.sh
```

脚本会（无需 sudo）：

1. 在 `.pgdata` 初始化并启动一个独立 PostgreSQL 16 集群（端口 `55432`，socket 在 `/tmp`）；
2. 建两个互不影响的库 `batchload5`、`batchload2`，分别以 `block_rows=5`、`block_rows=2` 导入；
3. 打印回执、拒绝溯源与**原生 COPY 调用日志**；
4. `verify`：库内对账 + 独立参考库按源顺序逐条 `INSERT` 核对接受集合；
5. 断言两种 `block_rows` 业务结果完全相同（日志不同）。

自动化测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `python -m batchload init [--recreate]` | 建独立库，执行 `fixtures/schema.sql`，并建审计表 |
| `python -m batchload load-batch --block-rows N [--run-id ID] [--print-log]` | 真实 COPY 分批导入 + 二分隔离坏行 |
| `python -m batchload report [--run-id ID] [--rejects-only\|--log-only] [--block-rows N]` | 回执 / 拒绝 / COPY 调用日志 |
| `python -m batchload verify [--run-id ID]` | 库内对账 + 独立参考库逐条 INSERT 核对 |

公共连接参数：`--host --port --user --dbname --input`（也可用 `PGHOST/PGPORT/PGUSER`）。

## 数据与逻辑行号

- 只导入 `fixtures/schema.sql` 里的 **customers、orders** 两张业务表；`import_receipts`
  用于同事务保存导入回执；另增两个审计表（见下）。
- 输入用标准库 `csv` 解析成**逻辑记录**。字段内换行（`o6`）与引号内逗号（`o5`）
  不会破坏切块——切块依据是逻辑记录数而非物理行。逻辑行号为 1-based、不含表头。
- 空字段（无论是否带引号）统一规整为 `None`，COPY 写成空字段，由 PostgreSQL 转成 `NULL`。
- 处理顺序固定：**customers 全部完成后再 orders**；同表内严格按文件/逻辑行顺序。

## 核心算法（loader.py）

每个原始块在**一个事务**内完成：

1. 对整块发起一次真实 `COPY ... FROM STDIN`（psycopg `cursor.copy()`），外层包 `SAVEPOINT`。
2. 整块成功 → `RELEASE SAVEPOINT`，块内各行接受。
3. 整块失败 → `ROLLBACK TO SAVEPOINT`：事务回到可用状态（不会停在 aborted transaction），
   此前已确认的好行不受影响；随后按**左右半块递归**，左半处理完才处理右半，保持源顺序。
4. 递归到单行仍失败 → 写入 `rejects`，不终止整批。
5. 该块的可接受行 + 本块 `import_receipts` 回执一起 `COMMIT`。

因此：

- 全好块只产生 **一次** 批量 COPY（`depth=0 success`，无更深尝试）；
- 坏块表现为一次失败 COPY 后沿半块不断二分，直到定位到具体坏行；
- 同主键重复时，源顺序中**第一个满足全部约束**的记录胜出。先出现但金额非法的
  `o2`（逻辑行 2）不会抢占订单号，后出现的合法 `o2`（逻辑行 3）被接受。

所有判定来自真实约束：

| 场景 | SQLSTATE | 约束名（PostgreSQL 实际返回） |
| --- | --- | --- |
| 主键重复 / 目标已有行 | `23505` | `customers_pkey` / `orders_pkey` |
| NOT NULL（空必填） | `23502` | 无（PG 不返回约束名，记 NULL） |
| 非法 decimal | `22P02` | 无 |
| 外键缺失 | `23503` | `orders_customer_id_fkey` |
| 负金额 CHECK | `23514` | `orders_amount_check` |

## 表

- `customers`、`orders`、`import_receipts`：完全沿用 `fixtures/schema.sql`，不改动。
- `rejects`（新增审计表）：`(run_id, table_name, file_name, logical_row, source_key,
  sqlstate, constraint_name, message)`，主键 `(run_id, table_name, logical_row)`。
- `copy_attempt_log`（新增审计表）：每次原生 COPY 尝试的深度、覆盖逻辑行区间、
  行数、成败、SQLSTATE、约束名、错误全文。该表用**独立自动提交连接**写入，
  因此块内失败回滚也不会丢失失败尝试的证据。

## 不变量（verify 强制）

- 回执逐输入记录唯一：`输入总数 = 接受数 + 拒绝数`；
- 接受主键集合 == 业务表实际主键集合；拒绝记录与 `rejects` 一一对应；
- 独立参考库（全新临时库，按源顺序逐条 `INSERT`，同样由真实约束裁决）得到的
  接受/拒绝集合与目标库一致；
- `block_rows=2` 与 `block_rows=5` 业务结果相同，仅 COPY 尝试日志不同；
- 目标库已有行不会被覆盖——重复主键以 `23505` 被拒绝。

## 边界

仅做两表导入；不做 schema 迁移、自动修数、通用重试服务或大规模压测。
单导入者模型，不处理并发冲突与崩溃恢复。
