# 受限批量关联执行器（NULL 感知，消除 N+1）

用 Python 3.11 + 内置 SQLite 实现的离线报表关联执行器。它不解析任意 SQL，
只接受经 schema 白名单校验的 typed JSON plan，把朴素的「每条外行查一次内表」
（N+1）改写为「每批、每个来源一条参数化批量探测」，并完整实现 SQL 三值逻辑
与标量子查询基数错误语义。

## 目录与交付物

```
prepare.py                  由 fixtures/seed.sql 建真实小库，交叉校验 schema/plan，建关联索引
run.py                      CLI 执行入口：python run.py DB PLAN SCHEMA OUT_RESULTS OUT_STATS
run.sh                      一键端到端复现（多批大小 / 两来源 / 错误路径 / 独立核对）
check.py                    独立 oracle 与不变量测试（49 项断言）
batchjoin/
  plan.py                   typed JSON plan 加载与白名单/结构校验（无 SQL 解析）
  engine.py                 批量探测引擎、语句计数、原子发布、标量基数错误
  predicates.py             三值逻辑与标量基数（显式区分空候选/单 NULL/多候选，不用 set）
fixtures/
  seed.sql                  真实小数据（重复外键、无内行、内 NULL、外 NULL、标量多行）
  schema.json               表/列白名单与逻辑类型（带 ? 表示可空）
  plan.json                 主 plan：EXISTS + IN + NOT IN（共享 payments 来源）
  scalar-plan.json          标量 plan（含 customer_id=50 的两行 -> 多行错误）
  two-source-plan.json      两来源 plan（payments + flags），证明查询数随来源数增长
output/                     运行产物（结果 JSON、统计 JSON、report.db），由 run.sh 生成
```

## 快速开始

```bash
python3.11 prepare.py output/report.db          # 1. 建库 + 静态校验（拒绝覆盖已存在路径）
python3.11 run.py output/report.db \
    fixtures/plan.json fixtures/schema.json \
    output/results.json output/stats.json       # 2. 执行
python3.11 check.py                             # 3. 独立核对（49 项）
# 或一键：
./run.sh
```

退出码：`0` 成功；`2` plan/schema 不合法；`3` 查询期错误（标量多行），
此时结果与统计文件都不会产生——**错误查询不发布部分结果**。

## plan 契约（受限范围，刻意不做通用优化器）

- 一张外表、**至多两张内表**、**一个整数等值关联键**；
- 表达式仅 `exists` / `scalar` / `in` / `not_in`，按声明顺序输出；
- 表名、列名必须出现在 `schema.json` 白名单，标识符双引号引用并转义；
- 所有取值经 `?` 占位符绑定参数，plan 文本从不进入 SQL 语法；
- 不支持嵌套两层相关、任意 WHERE 过滤、JOIN、自定义函数。

来源（source）= `(内表, 内关联列, 外关联列)`。同一来源上的多个表达式
**共享本批的一次读取**（见 `plan.json` 的三个 payments 表达式）。

## 执行模型

1. 一次顺序扫描外表，按 `outer_order`（row_id）排序、`batch_size` 切批；
2. 每批对每个来源提取**本批不同的非 NULL 关联键**（保序去重），
   发一条参数化 `IN (?, ?, ...)` 查询，只取本来源表达式需要的列；
3. 取回的内行按关联键分组，再**逐行附回每条外行**——外行不折叠，
   同一关联键的不同 `probe` 各自得到自己的 IN/NOT IN 结果；
4. 外键为 NULL 的批/行不发探测（SQL 等号下 `NULL = NULL` 不为真）；
5. 全部批成功后，临时文件 + `os.replace` 原子发布结果。

## SQL 语义（含 NULL 三值逻辑）

| 场景 | EXISTS | 标量 | IN | NOT IN |
|---|---|---|---|---|
| 零匹配行（含外键 NULL、无内行） | FALSE | NULL | **FALSE（空集合一律 FALSE）** | TRUE |
| 匹配行存在、命中等值 | TRUE | — | TRUE | FALSE |
| 无等值，候选含 NULL（非空集合） | TRUE | — | **UNKNOWN→JSON null** | UNKNOWN→null |
| 外值 NULL、候选非空 | TRUE | 取该值 | UNKNOWN→null | UNKNOWN→null |
| 标量恰好一行（值可为 NULL） | TRUE | 返回该值（NULL 也原样返回） | — | — |
| 标量多于一行 | — | **报错**，带外行 row_id 与全部内行主键 | — | — |

要点：

- `NULL IN (空集)` 也是 FALSE；空候选、单个 NULL、多个候选在 `predicates.py`
  中是显式分支，候选始终用**有序 list 承载、保留 NULL 与重复**，不放进
  Python `set`（set 会丢 NULL/重复信息）；
- EXISTS 只看行是否存在，内列 NULL 不影响存在性；
- 标量多行不静默取首行：抛 `CardinalityError`，消息含具体外行 `row_id`
  与全部命中内行主键（fixture 中 row_id=7 命中 flags 3、4）。

## 执行统计如何证明「随批与来源增长，而非随外行数增长」

`stats.*.json` 的计数器由 SQLite `set_trace_callback` 独立采集，
`inner_batch_queries` 对每个来源统计「含 ≥1 个非 NULL 键的批数」：

| batch_size | 批数 | 内表批量查询（单来源 plan） | 朴素 N+1 |
|---|---|---|---|
| 1 | 7 | 6（row6 外键 NULL，整批跳过） | 6 |
| 2 | 4 | 4 | 6 |
| 3 | 3 | 3 | 6 |
| 7 / 1000 | 1 | 1 | 6 |

两来源 plan（batch_size=7）：外表扫描 1 次 + 每来源 1 次 = 共 3 条 SELECT，
其中 payments 的 3 个表达式共享 1 次查询；朴素 N+1 需 12 次。
每个来源还记录 `per_batch.distinct_keys` 与 `rows_returned`：
`check.py` 验证向 payments 插入 500 个不相关客户（1500 行噪音）后，
各批 `rows_returned` 与 distinct 键集合**逐批不变**，不相关行不会被读入。

## 独立核对方法（check.py）

- EXISTS / IN / NOT IN：对每条外行执行**独立 SQLite 相关子查询**逐行比对，
  利用 SQLite 自身的三值逻辑实现作为 oracle；
- 标量：**不**把 SQLite 标量取值当 oracle（它对多行会静默取首行），
  改为独立执行候选计数 `SELECT (SELECT COUNT(*) ...)`，
  用计数核查零行/单行/多行的题定错误语义；
- 另含批大小不变性、不相关键压力、原子发布、白名单注入拒绝、
  空表边界与三值纯函数单元测试，共 49 项断言。

## 数据语义锚点（fixtures/seed.sql）

| row_id | customer_id | probe | has_payments | in_codes | not_in_codes | 说明 |
|---|---|---|---|---|---|---|
| 1 | 10 | 7 | true | true | false | code 7 等值命中 |
| 2 | 10 | 8 | true | null | null | 重复外键；codes(7,NULL) 涉 NULL 比较→UNKNOWN |
| 3 | 20 | 8 | true | false | true | code 9 全非空不命中 |
| 4 | 30 | NULL | true | null | null | 外值 NULL、候选非空→UNKNOWN |
| 5 | 40 | NULL | false | false | true | 无内行→空集合 FALSE / NOT IN TRUE |
| 6 | NULL | 7 | false | false | true | 外键 NULL 不匹配任何内键（含 NULL 内键） |
| 7 | 50 | 9 | false | false | true | payments 无此客户 |

`flags` 中 customer_id=50 有两行（flag_id 3、4），使 `scalar-plan.json`
对 row_id=7 触发多行基数错误；删除 flag_id=4 后同一 plan 成功，
row1/2（重复外键 10）各自独立得到 `'ok'`，证明外行不折叠。
