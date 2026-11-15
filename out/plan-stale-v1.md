# 只读路由计划报告 — route_fixture.events

- 生成时间(UTC): 2026-11-15T08:21:51.661845+00:00
- 输入: `fixtures/rows.jsonl`(sha256 `bbc1d5a8fc68f59e3de82aead6a1399456e7d9441a036fee104344269feb549d`,8 行)
- 父表 OID: 17156,目录指纹: `62c8b653f0cb5ec47ffcbd857f72e268333c1019d15fcaf8cb0be7b860e076ff`
- 计划只读来源:pg_partition_tree / pg_partitioned_table / pg_attribute / pg_get_expr 的 SELECT

## 分区树(目录快照,含 OID 与边界)

- `route_fixture.events`(oid=17156) — LIST(tenant text COLLATE "C")
  - `route_fixture.regional`(oid=17160) — RANGE(day_seq int4, slot int4) = IN ('east', 'west')
    - `route_fixture.early`(oid=17164) = FROM (MINVALUE, MINVALUE) TO (10, 0)
    - `route_fixture.middle`(oid=17170) = FROM (10, 0) TO (20, 0)
    - `route_fixture.regional_default`(oid=17176) = DEFAULT
  - `route_fixture.null_tenant`(oid=17182) = IN (NULL)
  - `route_fixture.other_tenant`(oid=17188) = DEFAULT

## 逐行路由(接受 8 / 共 8)

### 行 1 → `route_fixture.early`(oid=17164)

```json
{"day_seq": 9, "row_id": 1, "slot": 999, "tenant": "east", "value": 10}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=9, slot=999):按词典序与 early 边界 FROM (MINVALUE, MINVALUE) TO (10, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✓(列 day_seq: 9 < 边界值 10)✓
  - 候选 `route_fixture.middle` FROM (10, 0) TO (20, 0):下界 ✗(列 day_seq: 9 < 边界值 10),上界 ✓(列 day_seq: 9 < 边界值 20)✗

### 行 2 → `route_fixture.early`(oid=17164)

```json
{"day_seq": 10, "row_id": 2, "slot": -1, "tenant": "west", "value": 20}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='west':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=10, slot=-1):按词典序与 early 边界 FROM (MINVALUE, MINVALUE) TO (10, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✓(列 slot: -1 < 边界值 0)✓
  - 候选 `route_fixture.middle` FROM (10, 0) TO (20, 0):下界 ✗(列 slot: -1 < 边界值 0),上界 ✓(列 day_seq: 10 < 边界值 20)✗

### 行 3 → `route_fixture.middle`(oid=17170)

```json
{"day_seq": 10, "row_id": 3, "slot": 0, "tenant": "east", "value": 30}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=10, slot=0):按词典序与 middle 边界 FROM (10, 0) TO (20, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✗(各列与边界逐列相等)✗
  - 候选 `route_fixture.middle` FROM (10, 0) TO (20, 0):下界 ✓(各列与边界逐列相等),上界 ✓(列 day_seq: 10 < 边界值 20)✓

### 行 4 → `route_fixture.regional_default`(oid=17176)

```json
{"day_seq": 20, "row_id": 4, "slot": 0, "tenant": "west", "value": 40}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='west':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=20, slot=0):逐边界词典序比较后不被任何非 DEFAULT 分区覆盖(见 candidates),DEFAULT 分区 regional_default 是同层补集 → 落入 DEFAULT
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✗(列 day_seq: 20 > 边界值 10)✗
  - 候选 `route_fixture.middle` FROM (10, 0) TO (20, 0):下界 ✓(列 day_seq: 20 > 边界值 10),上界 ✗(各列与边界逐列相等)✗

### 行 5 → `route_fixture.regional_default`(oid=17176)

```json
{"day_seq": null, "row_id": 5, "slot": 2, "tenant": "east", "value": 50}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=NULL, slot=2) 含 NULL:按 PostgreSQL 规则,含 NULL 的 RANGE 键不匹配任何 RANGE 边界,只能进入 DEFAULT 分区 regional_default

### 行 6 → `route_fixture.null_tenant`(oid=17182)

```json
{"day_seq": 15, "row_id": 6, "slot": 1, "tenant": null, "value": 60}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant=NULL:命中 null_tenant 的 FOR VALUES IN (NULL);LIST 中的 NULL 与 DEFAULT 不同,仅匹配显式 NULL 列表项

### 行 7 → `route_fixture.other_tenant`(oid=17188)

```json
{"day_seq": 15, "row_id": 7, "slot": 1, "tenant": "else", "value": 70}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='else':未命中任何非 DEFAULT 列表(IN ('east', 'west')、IN (NULL);text 按 C collation 逐字节判等);DEFAULT 分区 other_tenant 是同层补集 → 落入 DEFAULT

### 行 8 → `route_fixture.middle`(oid=17170)

```json
{"day_seq": 19, "row_id": 8, "slot": 999, "tenant": "east", "value": 80}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=19, slot=999):按词典序与 middle 边界 FROM (10, 0) TO (20, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✗(列 day_seq: 19 > 边界值 10)✗
  - 候选 `route_fixture.middle` FROM (10, 0) TO (20, 0):下界 ✓(列 day_seq: 19 > 边界值 10),上界 ✓(列 day_seq: 19 < 边界值 20)✓

## 拒绝清单(0 行)

(无)
