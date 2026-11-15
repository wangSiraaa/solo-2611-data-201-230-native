# routecopy — 目录驱动的分区路由计划(plan)与批量装载(load)

入仓前先给出**可审阅的全批路由计划**:每条记录将进入哪个叶分区、每层比较
依据是什么、哪些记录没有目的地;然后 **load** 只执行计划已接受的行,按计划
分组批量 COPY 到真实叶表,单事务提交。数据库侧的路由事实(层次、键列、边界)
全部来自 PostgreSQL 16 目录的只读查询,不硬编码任何现成表名。

- 运行环境:Python 3.11、本地 PostgreSQL 16、psycopg 3.2(见 `requirements.txt`)
- 只在独立实验库运行(演示用 `route_lab`),不连接业务库
- 题目原文见 `TASK.md`

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 本地 PostgreSQL 16(独立实例,示例)
initdb -D pgdata -U postgres --auth=trust
pg_ctl -D pgdata -o "-p 54329 -k /tmp/pgrun -c listen_addresses=''" start
createdb -h /tmp/pgrun -p 54329 -U postgres route_lab
psql -h /tmp/pgrun -p 54329 -U postgres -d route_lab -f fixtures/schema.sql

# 一键跑完全部场景(events / strict / 约束失败 / 边界变更 / 类型不符)
scripts/run_demo.sh
```

单条命令形式(连接串用 `--dsn` 或环境变量 `ROUTECOPY_DSN`):

```bash
export PYTHONPATH=src ROUTECOPY_DSN="host=/tmp/pgrun port=54329 user=postgres dbname=route_lab"
python -m routecopy plan   --config fixtures/config.json --parent route_fixture.events \
       --input fixtures/rows.jsonl --plan-out out/plan.json --report-out out/plan.md
python -m routecopy load   --plan out/plan.json --input fixtures/rows.jsonl \
       --out out/load.json --report-out out/load.md
python -m routecopy verify --plan out/plan.json --expect loaded \
       --out out/verify.json --report-out out/verify.md
```

退出码:`0` 成功;`2` 用法/输入错误;`3` load 数据库错误(已整批回滚);
`4` 计划过期被拒绝;`5` 验证不一致;`6` 目录形态不受支持。

## plan:只读路由计划

`plan` 连接被显式置为 **READ ONLY**,只执行目录 SELECT
(`pg_partition_tree` / `pg_partitioned_table` / `pg_attribute` / `pg_type` /
`pg_collation` / `pg_get_expr(relpartbound, …)`),路由计算全部在内存中完成;
不存在“先插入再回滚”的伪只读。

**支持范围**(超出即拒绝,退出码 6):

- 最多两级分区(`config.max_partition_depth`),LIST 与 RANGE 策略
- 键为整数族(int2/int4/int8)或 text 的**普通列**;text 键必须显式
  `COLLATE "C"`(目录中核对 `attcollation`)
- 拒绝:HASH 分区、表达式分区键、其他 collation、超深层次、不支持的键类型
- 边界文本只按 `FOR VALUES IN (…)` / `FROM (…) TO (…)` / `DEFAULT` 的
  固定文法解析,不是通用 SQL 解析器

**路由语义**(与 PostgreSQL 16 对齐,逐行输出每层比较依据):

- **LIST**:先查全部非 DEFAULT 分区的列表项;text 按 C collation 逐字节判等
  (UTF-8 字节序与码点序一致,等价于 Python 字符串比较)。`FOR VALUES IN (NULL)`
  的 NULL 列表项与 DEFAULT 严格区分:NULL 键只命中显式 NULL 列表,否则才考虑
  DEFAULT。
- **RANGE**:复合键按词典序、左闭右开 `[FROM, TO)` 比较,逐列分出胜负;
  `MINVALUE`/`MAXVALUE` 是无限边界,不等于任何普通整数(任何行值都
  > MINVALUE、< MAXVALUE)。
- **RANGE 键含 NULL**:按 PostgreSQL 真实规则,不匹配任何 RANGE 边界,
  只能进入同层 DEFAULT 分区;无 DEFAULT 则无目的地(已在 PG16 上实测确认:
  `(NULL,2)`、`(10,NULL)`、`(25,NULL)` 全部落入 DEFAULT)。
- **DEFAULT 是同层补集**:先评估所有非 DEFAULT 分区,都不覆盖才落入
  DEFAULT;同层无 DEFAULT 时该行进入拒绝清单,**绝不**路由到随意挑选的叶。
- **类型不符**:JSON 类型与列类型不符(如 text 列收到数字、int4 溢出、
  布尔/浮点冒充整数、未知列)的行进入拒绝清单。

**计划绑定**(`out/plan-*.json`):输入文件 sha256 与行数;父表 OID;
全部分区的 OID、键列(含类型与 collation)、边界指纹;整体目录指纹
(规范化 JSON 的 sha256)。`out/plan-*.md` 是可审阅的逐行路由报告。

## load:复核后单事务批量 COPY

1. **只读复核**(与 COPY 同事务):重算输入文件 sha256、重查目录并重算指纹,
   与计划逐项比对;目录或输入已变则拒绝旧计划(退出码 4),报告具体哪个
   分区的哪段边界发生了变化。约定 load 期间无 DDL,不实现在线目录变更协议。
2. **装载**:只取计划中 `accepted` 的行(行内容来自计划文件,输入文件仅用于
   摘要核对),按计划分组到叶,以 `config.batch_rows` 为批大小逐批
   `COPY 叶表 (列…) FROM STDIN`,全部批次在**一个数据库事务**内完成,
   原始字段原样保留(缺失列按 NULL)。
3. **约束失败**:任何 COPY 错误(如 `CHECK(value>=0)`)使整批回滚,0 行落库,
   退出码 3;报告错误叶、批次与数据库错误。随后另起**仅用于诊断**的事务,
   对失败批次逐行重放并全部回滚(数据库净变化为 0),以定位具体输入行。
   CHECK 失败不会被伪装成路由无匹配。

## verify:独立核对

- **readback**:只读 `SELECT tableoid::regclass, * FROM 父表`,与计划接受集
  按 (叶, 全部字段) 多重集比对,验证 COPY 落库的行与字段完整。
- **oracle**:对每条计划接受的行,`INSERT 父表 … RETURNING tableoid`,
  以数据库真实路由为 oracle 核对计划叶与全部字段;触发约束的行从错误诊断
  中取出实际路由到的关系名继续比对。整个验证事务最后回滚,不落任何数据。
- **拒绝行核对**:对“无目的地”拒绝行尝试 INSERT 父表,必须报
  `no partition of relation`,证明数据库同样无法路由;类型不符的拒绝行属
  客户端严格性(DB 可能强转),不参与该项。

## 演示场景(`scripts/run_demo.sh`,报告在 `out/`)

| 场景 | 内容 | 关键报告 |
|---|---|---|
| A | events:8 行全部接受,含 NULL 租户、复合边界 `(10,-1)` 落回 early、`(20,0)` 与 NULL range 键进 DEFAULT、未知租户进 DEFAULT | `plan-events.md` / `load-events.md` / `verify-events.md` |
| B | strict(无 DEFAULT):2 行接受,6 行缺失目的地被拒,DB 核对确认不可路由 | `plan-strict.md` / `verify-strict.md` |
| C | constraint-failure:`value=-1` 触发 CHECK,整批回滚,报告错误叶与输入行 | `load-constraint-failure.md` |
| D | 修改边界 `middle TO (20,0)→(25,0)`:旧计划被拒(退出码 4),篡改输入亦被拒;新计划下行 4 改道 middle | `load-stale-v1.md` / `plan-stale-v2.md` |
| E | 类型不符/非法行全部进拒绝清单 | `plan-type-mismatch.md` |

## 代码结构

```
src/routecopy/
  catalog.py    只读目录内省:层次/键列/边界解析/受支持性校验/目录指纹
  planner.py    路由引擎(纯内存)+ 计划构建 + 路由报告渲染
  loader.py     新鲜度复核、单事务分批 COPY、失败诊断重放
  verifier.py   readback / 父表路由 oracle / 拒绝行核对
  cli.py        plan / load / verify 命令行
scripts/run_demo.sh        全场景演示
scripts/reset_schema.sql   重置 fixture schema
scripts/alter_boundary.sql 场景 D 的边界修改
tests/type-mismatch.jsonl  类型不符输入(场景 E)
```

## 明确不做的事

不创建新分区;不迁移旧 DEFAULT 数据;不做在线回填;不写通用 SQL 解析器
(只解析目录边界的固定文法);不连接业务库;plan 不做任何写操作。
非键列同样要求在 `config.supported_types` 内,以保证字段原样搬运。
