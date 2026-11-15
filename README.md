# 保留旧 SQL 接口的宽表纵向拆分

在**一次停写窗口**内，把 `split_fixture.records` 宽表拆成

| 对象 | 内容 |
| --- | --- |
| `split_fixture.records_main` | 热字段 `id, tenant, name, status` |
| `split_fixture.records_detail` | 冷字段 `id → records_main(id)`、`note`、`attachment`，仅当至少一个冷字段非 NULL 时存在 |
| `split_fixture.records` | **可写兼容视图**，列名/顺序/类型与原宽表完全一致 |
| `split_fixture.migration_report` | 可审查的结构与数量报告（迁移后保留） |

旧客户端仍然用同一个表名 `split_fixture.records` 做 `SELECT / 显式 id INSERT /
普通 UPDATE / DELETE / RETURNING`；`INSERT` 省略 `status` 仍得到 `active`。

不实现 CDC、在线双写或自动回滚服务——只要求这一个停写窗口，之后是普通单库事务。

---

## 迁移命令

前置：Python 3.11、PostgreSQL 16、目标库中已执行 `fixtures/schema.sql`。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 连接参数可用标准 PG* 环境变量，或显式 --dsn
export PGHOST=/tmp PGPORT=5432 PGUSER=postgres
.venv/bin/python migrate.py --dsn "dbname=mydb"          # 迁移
.venv/bin/python migrate.py --dsn "dbname=mydb" --json   # 输出保留的报告
```

也可以直接用 psql 执行同一份脚本（`migrate.py` 与它执行的是同一文件）：

```bash
psql -v ON_ERROR_STOP=1 -d mydb -f sql/migrate.sql
```

失败时 `migrate.py` 退出码为 1。整个迁移是一个显式 `BEGIN … COMMIT`，失败即整体
回滚，原宽表保持原样且可继续读写。

一键复现全部流程（自动建临时库、迁移、跑两套验证并打印报告）：

```bash
./run_all.sh            # 需要 PG16 客户端在 PATH，且能连到某实例
KEEP_DB=1 ./run_all.sh  # 保留验证库以便手工检查
```

## 文件

| 文件 | 作用 |
| --- | --- |
| `sql/migrate.sql` | 迁移脚本：预检 → 加锁 → 建表搬数 → 切换旧名 → 后置校验 → 写报告，全部在一个事务内 |
| `sql/inspect.sql` | 迁移后只读审计：对象类型、视图列、全 NULL 详情行、空值字节、报告 |
| `migrate.py` | 迁移命令封装；失败回滚并以非零码退出 |
| `migration_lib.py` | 执行迁移、读取报告 |
| `verify.py` | 按 `fixtures/client-cases.json` 逐条验证，并与**未迁移宽表副本**对照 |
| `test_safety.py` | 预检冲突、重复执行、切换后失败回滚、多行语句原子性 |
| `run_all.sh` | 建库 → 迁移 → 两套验证 → 打印报告 |
| `output/` | 运行时生成的日志与 JSON 结果（`.gitignore` 已忽略） |

## 迁移做了什么

`sql/migrate.sql` 在**单个事务**中依次执行：

1. **停写**：`LOCK TABLE split_fixture.records IN ACCESS EXCLUSIVE MODE`（外加
   `lock_timeout=5s`，避免无限排队）。锁持有到 `COMMIT`，期间无并发读者/写者。
2. **输入契约预检**：列名与顺序、类型、`NOT NULL` 标志、默认值都必须是
   `id,tenant,name,status,note,attachment` / `bigint,text,text,text,text,bytea`，
   主键必须是 `(id)`，`status` 必须有允许 `active/disabled` 的 `CHECK`。任何偏差
   直接报错退出，未改动任何对象。
3. **目标冲突预检**：`records_main`、`records_detail`、`migration_report` 及三个
   触发器函数若已存在则拒绝；旧表若被外键或视图依赖也拒绝；已迁移过的库再次执行
   会明确报 “already migrated”。
4. **建表与搬数**：主表搬全部行；详情表仅搬
   `WHERE note IS NOT NULL OR attachment IS NOT NULL` 的行。空字符串 `''` 与空
   bytea `''::bytea` **都不是 NULL**，因此会作为真实详情行保留；字节原样复制。
   当场校验：主表行数 = 源行数、详情行数 = 预期非全 NULL 行数、详情表无全 NULL 行。
5. **切换旧名**：`DROP TABLE split_fixture.records` 后立刻 `CREATE VIEW`（同名、
   同列序同类型），并 `ALTER VIEW … ALTER COLUMN status SET DEFAULT 'active'` 以
   保留省略 `status` 时的默认值。三个 `INSTEAD OF` 触发器承载写操作。
6. **后置校验**：视图列名/类型、`relkind='v'`、三个触发器、总行数，以及**整行内容
   摘要**（`md5(string_agg(md5(row_to_json(row)::text)))`）必须与迁移前一致。
7. **保留报告**：把结构与计数写入 `split_fixture.migration_report`（含两表的列、
   约束、视图定义、触发器、源/拆分摘要），提交后长期保留、可审查。

任一步失败 → 事务回滚 → `DROP TABLE` 与所有 `CREATE` 一并撤销，原宽表恢复可用。
`test_safety.py` 通过在 `COMMIT` 前**注入异常**专门验证了这条路径。

### 兼容视图的写语义

| 操作 | 行为 |
| --- | --- |
| `SELECT` | 主表 `LEFT JOIN` 详情表，列与类型同旧表 |
| `INSERT`（显式 id） | 写主表；仅当 `note`/`attachment` 至少一个非 NULL 才写详情表 |
| `UPDATE` 热字段 | 只更新主表；已有详情行不动，**不会**新建空详情 |
| `UPDATE` 冷字段 | 冷字段非全 NULL 时 insert-or-update 详情行；全 NULL 时删除详情行、**保留主行** |
| `DELETE` | 删主表；`ON DELETE CASCADE` 清理详情行 |
| `RETURNING` | 由视图行返回，列/类型与 `SELECT` 一致 |
| 改 `id` | 触发器显式抛错（`ERRCODE 0A000`，消息含 `immutable column "id"`） |
| 违反主键 / `CHECK` / `NOT NULL` | 底层主表约束直接报错；整条多行语句回滚 |

`note`/`attachment` 在 `UPDATE` 中未出现时，视图把它们的旧值带入 `NEW`，因此只改
热字段不会误删已有详情。空值判定统一用 `IS NOT NULL`，所以 `''` 和 `''::bytea`
始终算“有值”。

> 说明：视图在 `information_schema` 中所有列都显示 `is_nullable=YES`，因为
> PostgreSQL 视图不携带 `NOT NULL` 元数据。**约束本身仍然生效**：所有写入都落到
> 带 `NOT NULL`/`PRIMARY KEY`/`CHECK` 的主表，`verify.py` 专门通过视图验证了
> `tenant/name/status` 为 NULL 时的 INSERT/UPDATE 都会被拒绝。数据与约束行为与旧表
> 一致，只是元数据视图不同。

## 验证方法

### 1. 旧接口逐条验证 + 独立宽表对照（`verify.py`）

同一个库里建两个世界：

* `split_fixture.records`：迁移后的可写视图；
* `baseline.records`：用 `fixtures/schema.sql` 原样建立的**未迁移宽表副本**，作为
  合法语句的独立对照（对 `client-cases.json` 中的 SQL 只做 schema 名替换）。

对 `fixtures/client-cases.json` 每条用例从同一快照出发，在两侧各执行一次：

* **成功语句**：比较 `RETURNING` 的值、服务端报告的**列类型**、以及执行后的
  **完整有序行内容**（`SELECT` 全列，NULL 与 `bytea` 逐字节比较）；
* **失败语句**：两侧都必须拒绝，且迁移侧的 `records_main` + `records_detail`
  快照必须与执行前**逐字节相同**（证明没有留下部分修改）。

额外用例（任务明确要求）：

* **不可变 id 单独验证**：普通宽表没有这条限制，因此**不用 baseline 作对照**，
  直接断言迁移侧拒绝 `UPDATE … SET id=99` 且数据不变；
* 一个事务内先改冷字段再 `ROLLBACK`，确认无残留；
* 直接查询 `records_detail`：全 NULL 行数为 0；`INSERT` 不含冷字段不产生详情行；
  空 text / 空 bytea 被存成非 NULL 且长度为 0，并与对照库逐字节一致；
* `tenant/name/status` 的 `NOT NULL` 通过视图仍然生效。

### 2. 预检与失败恢复（`test_safety.py`）

目标对象冲突、输入契约被破坏、旧名不是普通表、重复执行都会在改动前被拒绝，原宽表
数据与可写性不变；在 `COMMIT` 前注入异常后，原宽表恢复为普通表、数据完整、可写，
且没有任何半成品拆分对象；多行 `UPDATE`/`INSERT` 任一行失败时主表与详情表都不留
部分修改。

### 结果

```
verify.py       : 66 passed, 0 failed
test_safety.py  : 28 passed, 0 failed
```

迁移后保留在库中的报告示例：

```
 report_id | source_row_count | main_row_count | detail_row_count | detail_all_null_rows | digest_match
-----------+------------------+----------------+------------------+----------------------+--------------
         1 |                4 |              4 |                3 |                    0 | t
```

（`detail_row_count=3`：id=2 的空 `''`/空 bytea、id=3、id=4 各一行；id=1 两冷字段
全 NULL，无详情行。）

## 设计取舍与边界

* **为什么是视图而不是把拆分表重命名**：契约为“同一个表名可写”，`INSTEAD OF`
  触发器让旧 `SELECT/INSERT/UPDATE/DELETE/RETURNING` 语义完整保留，且不需要复制
  一份只读视图冒充可写接口。
* **没有保留第二张持续接收写入的宽表**：旧名是视图，底层只有主表 + 详情表，不存在
  两套并行写入状态。`migration_report` 只存结构与计数，不接收业务写入。
* **只做这一次拆分**：脚本对 `split_fixture.records` 的具体列/约束做硬校验，不是
  通用拆表框架。
* **停写窗口**：`ACCESS EXCLUSIVE` 锁保证搬数与切换期间无并发写；不做 CDC 或在线
  双写。
* **事务与原子性**：整个迁移是单事务；客户端多行语句的原子性由 PostgreSQL 语句级
  回滚保证，触发器只做逐行映射，不吞异常。
* **重复执行**：不会覆盖已迁移状态，会明确拒绝。
