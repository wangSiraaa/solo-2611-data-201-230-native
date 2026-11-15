# 只读路由计划报告 — route_fixture.events

- 生成时间(UTC): 2026-11-15T08:21:52.322611+00:00
- 输入: `tests/type-mismatch.jsonl`(sha256 `1ca7b60cf705bed79a87ea829f244f801edf7fca501d3d561bc28371692953a1`,9 行)
- 父表 OID: 17156,目录指纹: `a255c8611c6032ef0ca3f915ce7f21c61e32a7e8da26f7b5a2e47f8f8354eb47`
- 计划只读来源:pg_partition_tree / pg_partitioned_table / pg_attribute / pg_get_expr 的 SELECT

## 分区树(目录快照,含 OID 与边界)

- `route_fixture.events`(oid=17156) — LIST(tenant text COLLATE "C")
  - `route_fixture.regional`(oid=17160) — RANGE(day_seq int4, slot int4) = IN ('east', 'west')
    - `route_fixture.early`(oid=17164) = FROM (MINVALUE, MINVALUE) TO (10, 0)
    - `route_fixture.middle`(oid=17170) = FROM (10, 0) TO (25, 0)
    - `route_fixture.regional_default`(oid=17176) = DEFAULT
  - `route_fixture.null_tenant`(oid=17182) = IN (NULL)
  - `route_fixture.other_tenant`(oid=17188) = DEFAULT

## 逐行路由(接受 2 / 共 9)

### 行 7 → `route_fixture.early`(oid=17164)

```json
{"day_seq": 9, "row_id": 16, "slot": 1, "tenant": "east", "value": 1}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=9, slot=1):按词典序与 early 边界 FROM (MINVALUE, MINVALUE) TO (10, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✓(列 day_seq: 9 < 边界值 10)✓
  - 候选 `route_fixture.middle` FROM (10, 0) TO (25, 0):下界 ✗(列 day_seq: 9 < 边界值 10),上界 ✓(列 day_seq: 9 < 边界值 25)✗

### 行 9 → `route_fixture.regional_default`(oid=17176)

```json
{"day_seq": 10, "row_id": 17, "slot": null, "tenant": "east", "value": 5}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=10, slot=NULL) 含 NULL:按 PostgreSQL 规则,含 NULL 的 RANGE 键不匹配任何 RANGE 边界,只能进入 DEFAULT 分区 regional_default

## 拒绝清单(7 行)

### 行 1 — type_mismatch

```json
{"day_seq": 9, "row_id": 10, "slot": 1, "tenant": 123, "value": 1}
```
- 原因: 列 tenant(text)需要 JSON 字符串,实际 int: 123

### 行 2 — type_mismatch

```json
{"day_seq": "9", "row_id": 11, "slot": 1, "tenant": "east", "value": 1}
```
- 原因: 列 day_seq(int4)需要 JSON 整数,实际 str: '9'

### 行 3 — type_mismatch

```json
{"day_seq": 9, "row_id": 12, "slot": true, "tenant": "east", "value": 1}
```
- 原因: 列 slot(int4)需要 JSON 整数,实际 bool: True

### 行 4 — type_mismatch

```json
{"day_seq": 2147483648, "row_id": 13, "slot": 1, "tenant": "east", "value": 1}
```
- 原因: 列 day_seq 值 2147483648 超出 int4 范围 [-2147483648,2147483647]

### 行 5 — type_mismatch

```json
{"day_seq": 9, "extra": 1, "row_id": 14, "slot": 1, "tenant": "east", "value": 1}
```
- 原因: 未知列 'extra'(父表无此列)

### 行 6 — type_mismatch

```json
{"day_seq": 9.5, "row_id": 15, "slot": 1, "tenant": "east", "value": 1}
```
- 原因: 列 day_seq(int4)需要 JSON 整数,实际 float: 9.5

### 行 8 — not_an_object

```json
["not", "an", "object"]
```
- 原因: 输入行不是 JSON 对象

