# 只读路由计划报告 — route_fixture.strict

- 生成时间(UTC): 2026-11-15T08:21:50.949916+00:00
- 输入: `fixtures/rows.jsonl`(sha256 `bbc1d5a8fc68f59e3de82aead6a1399456e7d9441a036fee104344269feb549d`,8 行)
- 父表 OID: 17088,目录指纹: `912273ac57aac2fb7651a60428f5d04bb0e6f05d678a934aeb68cc3da222a162`
- 计划只读来源:pg_partition_tree / pg_partitioned_table / pg_attribute / pg_get_expr 的 SELECT

## 分区树(目录快照,含 OID 与边界)

- `route_fixture.strict`(oid=17088) — LIST(tenant text COLLATE "C")
  - `route_fixture.strict_east`(oid=17092) — RANGE(day_seq int4, slot int4) = IN ('east')
    - `route_fixture.strict_mid`(oid=17096) = FROM (10, 0) TO (20, 0)

## 逐行路由(接受 2 / 共 8)

### 行 3 → `route_fixture.strict_mid`(oid=17096)

```json
{"day_seq": 10, "row_id": 3, "slot": 0, "tenant": "east", "value": 30}
```
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='east':命中 strict_east 的 IN ('east')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.strict_east`:RANGE 键 (day_seq=10, slot=0):按词典序与 strict_mid 边界 FROM (10, 0) TO (20, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.strict_mid` FROM (10, 0) TO (20, 0):下界 ✓(各列与边界逐列相等),上界 ✓(列 day_seq: 10 < 边界值 20)✓

### 行 8 → `route_fixture.strict_mid`(oid=17096)

```json
{"day_seq": 19, "row_id": 8, "slot": 999, "tenant": "east", "value": 80}
```
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='east':命中 strict_east 的 IN ('east')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.strict_east`:RANGE 键 (day_seq=19, slot=999):按词典序与 strict_mid 边界 FROM (10, 0) TO (20, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.strict_mid` FROM (10, 0) TO (20, 0):下界 ✓(列 day_seq: 19 > 边界值 10),上界 ✓(列 day_seq: 19 < 边界值 20)✓

## 拒绝清单(6 行)

### 行 1 — no_matching_partition

```json
{"day_seq": 9, "row_id": 1, "slot": 999, "tenant": "east", "value": 10}
```
- 原因: 第 2 层 route_fixture.strict_east: 无 RANGE 分区覆盖且无 DEFAULT
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='east':命中 strict_east 的 IN ('east')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.strict_east`:RANGE 键 (day_seq=9, slot=999):逐边界词典序比较后不被任何非 DEFAULT 分区覆盖(见 candidates),且同层无 DEFAULT → 无目的地
  - 候选 `route_fixture.strict_mid` FROM (10, 0) TO (20, 0):下界 ✗(列 day_seq: 9 < 边界值 10),上界 ✓(列 day_seq: 9 < 边界值 20)✗

### 行 2 — no_matching_partition

```json
{"day_seq": 10, "row_id": 2, "slot": -1, "tenant": "west", "value": 20}
```
- 原因: 第 1 层 route_fixture.strict: 无匹配分区且无 DEFAULT
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='west':未命中任何非 DEFAULT 列表(IN ('east');text 按 C collation 逐字节判等),且同层无 DEFAULT 分区 → 无目的地

### 行 4 — no_matching_partition

```json
{"day_seq": 20, "row_id": 4, "slot": 0, "tenant": "west", "value": 40}
```
- 原因: 第 1 层 route_fixture.strict: 无匹配分区且无 DEFAULT
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='west':未命中任何非 DEFAULT 列表(IN ('east');text 按 C collation 逐字节判等),且同层无 DEFAULT 分区 → 无目的地

### 行 5 — no_matching_partition

```json
{"day_seq": null, "row_id": 5, "slot": 2, "tenant": "east", "value": 50}
```
- 原因: 第 2 层 route_fixture.strict_east: RANGE 键含 NULL 且无 DEFAULT
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='east':命中 strict_east 的 IN ('east')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.strict_east`:RANGE 键 (day_seq=NULL, slot=2) 含 NULL:按 PostgreSQL 规则只能进入 DEFAULT 分区,但同层无 DEFAULT → 无目的地

### 行 6 — no_matching_partition

```json
{"day_seq": 15, "row_id": 6, "slot": 1, "tenant": null, "value": 60}
```
- 原因: 第 1 层 route_fixture.strict: 无匹配分区且无 DEFAULT
- 第 1 层 `route_fixture.strict`:LIST 键 tenant=NULL:未命中任何非 DEFAULT 列表(IN ('east');text 按 C collation 逐字节判等),且同层无 DEFAULT 分区 → 无目的地

### 行 7 — no_matching_partition

```json
{"day_seq": 15, "row_id": 7, "slot": 1, "tenant": "else", "value": 70}
```
- 原因: 第 1 层 route_fixture.strict: 无匹配分区且无 DEFAULT
- 第 1 层 `route_fixture.strict`:LIST 键 tenant='else':未命中任何非 DEFAULT 列表(IN ('east');text 按 C collation 逐字节判等),且同层无 DEFAULT 分区 → 无目的地

