# 带逐行贡献来源的受限关系 ETL 执行器

只实现 `source / filter / union_all / inner_join / group` 五个算子的内存执行器。
每条输出都能回查到真正参与的原始记录及其次数，`trace` 给出逐组、逐聚合的贡献明细，
`verify` 重新读取源数据、重放贡献得到聚合值，并与独立 SQLite 等价查询逐组比对。

不实现通用 SQL 解析、任意表达式语言、`LEFT JOIN`、缺失行反事实解释或可视化平台。

## 运行

需要 Python 3.11+，无第三方依赖。

```bash
# 结果 + 来源写入 <out>/results.json（只写这个文件，不改动 fixtures）
python3 -m provenance_etl run    --plan fixtures/plan.json --out out

# 逐组、逐聚合列出贡献行、贡献值、源记录和操作路径
python3 -m provenance_etl trace  --plan fixtures/plan.json

# 重读源数据重放贡献，并与独立 SQLite 查询逐组比对；不一致时退出码为 1
python3 -m provenance_etl verify --plan fixtures/plan.json
```

三个命令都接受 `--json` 输出完整结构化报告。也可以作为库调用：

```python
from provenance_etl import run, trace, verify

report = run("fixtures/plan.json", "out")
trace_report = trace("fixtures/plan.json")
verify_report = verify("fixtures/plan.json")  # report["ok"] is True/False
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 计划格式

`plan.json` 的 `nodes` 是算子列表（按拓扑序书写，执行器仍会自行校验拓扑），
`output` 指定输出节点。所有节点 `id` 唯一，引用必须存在，出现环或自引用即拒绝。

| op | 必需字段 | 说明 |
| --- | --- | --- |
| `source` | `table`, `path` | JSONL 关系表，必须含唯一 `row_id` |
| `filter` | `input`, `field`, `cmp`(`eq`/`gt`), `value` | 单字段与常量比较 |
| `union_all` | `left`, `right` | 两侧业务字段集合必须一致 |
| `inner_join` | `left`, `right`, `left_key`, `right_key`, `left_prefix`, `right_prefix` | 单等值键；左右同名字段加前缀 |
| `group` | `input`, `keys`, `aggregates` | 聚合仅 `COUNT(*)` 与 `SUM(integer)` |

源表每行是 JSON 对象：`row_id` 为整数或字符串且表内唯一；其余业务字段只能是整数、字符串或 `null`。
同一张源表可以被不同分支重复读取（多次 `source` 引用同一文件），但 `(table, row_id)` 必须全局唯一，
因为来源身份以它为准。

`group` 输出是终态：聚合会合并行级身份，因此 `group` 节点不能再作为其他算子的输入。

## 语义约定

- **重复保留**：两条业务值相同但 `row_id` 不同的记录始终是两行；不同分支重复进入同一源行也各算一次。
- **NULL**：`eq`/`gt` 与 NULL 比较均不通过；连接键为 NULL 不匹配；分组键为 NULL 归入同一组。
- **`union_all`**：保留两侧全部出现次数，不先去重来源集合。
- **`inner_join`**：只支持一个等值键；每个左行与全部匹配右行配对，产生完整重复配对。
- **`COUNT(*)`**：每个组内行都计 1，包括业务值为 NULL 的行。
- **`SUM`**：只累计非 NULL 的整数值；组内全为 NULL 时结果为 `null`。

## 来源身份与 trace

每个中间行带稳定身份 `prov`：

```
source     (table, row_id)
filter     继承输入行身份（不新增）
union_all  ("union", "L"/"R", 原身份)
join       ("join", 左身份, 右身份)   # 有序组合，保存全部配对
```

来源从不做集合去重。`trace` 对每个输出组的每个聚合给出：

- `value`：聚合结果；`contribution_count` / `distinct_value_count`：参与行数 / 不同贡献值数；
- `contributing_values`：每个贡献值及出现次数（例如 `10 x8`，解释重复行为何叠加）；
- `contributions`：逐参与行，含 `origin`（源表 / row_id / 源字段）、`source_value` 与完整的
  `source_occurrences`（表、row_id、从该源行到当前行的操作路径）；
- `source_records`：按源记录聚合，给出总出现次数，以及每条操作路径各自的次数。同一原始记录经
  两个 union 分支、再与多个右行连接时，会保留相应的分支与连接次数。

`run` 对非分组输出还会在每行附 `source_occurrences`；`group` 行聚合了多条记录，其行级来源统一
放在 `trace` / `verify` 中，避免给出会误导的无序源 ID 集合。

## verify

`verify` 独立执行三次交叉检查：

1. **重放**：重新读取源文件，按 `trace` 记录的贡献逐条取回源值，重算 `COUNT` / `SUM`
   并与执行结果比较（`SUM` 的 NULL 贡献必须被排除且计入错误）。
2. **SQLite 等价**：把同一份源数据装入内存 SQLite，用一个小型手写 SQL 翻译器（不是复用执行器结果）
   重建 `UNION ALL` / 单键 `INNER JOIN` / `GROUP BY`，逐组比对键与聚合值。
3. **确定性**：物理行序改变后，结果值与来源身份保持不变（由测试覆盖）。

输出 `{"ok": bool, "errors": [...], "groups": [...], "sqlite_sql": ..., "sqlite": [...]}`；
存在不一致时退出码为 1。

## 拒绝条件

- 重复 `row_id`（表内或跨源节点）；
- 未知字段（filter / join key / group key / 聚合字段）；
- 循环计划、自引用、未知输入或输出；
- `union_all` 两侧业务字段不一致；
- 连接前缀导致列名冲突；
- 非整数 `SUM` 字段、`COUNT` 带字段（仅支持 `COUNT(*)`）；
- 非法行值类型（仅允许整数、字符串、NULL）、空源文件、字段集合不一致。

## 文件

```
provenance_etl/
  engine.py   # 计划校验、求值、来源身份
  api.py      # run / trace / verify，SQLite 等价翻译
  cli.py      # 命令行
fixtures/     # 样例计划、源表与参考 SQL（只读）
tests/        # 单元测试
```
