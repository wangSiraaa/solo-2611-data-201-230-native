# 只读路由计划报告 — route_fixture.events

- 生成时间(UTC): 2026-11-15T08:21:51.307276+00:00
- 输入: `fixtures/constraint-failure.jsonl`(sha256 `ff57877a33e9facb72b30671d423a3c6c6eefd0de680d7060d138339325c8654`,1 行)
- 父表 OID: 17103,目录指纹: `62a8800c4a1898eb0a92eb51c95fb7772ce95e0d62bff4738608cc25e8df1f37`
- 计划只读来源:pg_partition_tree / pg_partitioned_table / pg_attribute / pg_get_expr 的 SELECT

## 分区树(目录快照,含 OID 与边界)

- `route_fixture.events`(oid=17103) — LIST(tenant text COLLATE "C")
  - `route_fixture.regional`(oid=17107) — RANGE(day_seq int4, slot int4) = IN ('east', 'west')
    - `route_fixture.early`(oid=17111) = FROM (MINVALUE, MINVALUE) TO (10, 0)
    - `route_fixture.middle`(oid=17117) = FROM (10, 0) TO (20, 0)
    - `route_fixture.regional_default`(oid=17123) = DEFAULT
  - `route_fixture.null_tenant`(oid=17129) = IN (NULL)
  - `route_fixture.other_tenant`(oid=17135) = DEFAULT

## 逐行路由(接受 1 / 共 1)

### 行 1 → `route_fixture.middle`(oid=17117)

```json
{"day_seq": 10, "row_id": 100, "slot": 0, "tenant": "east", "value": -1}
```
- 第 1 层 `route_fixture.events`:LIST 键 tenant='east':命中 regional 的 IN ('east', 'west')(text 按 C collation 逐字节判等)
- 第 2 层 `route_fixture.regional`:RANGE 键 (day_seq=10, slot=0):按词典序与 middle 边界 FROM (10, 0) TO (20, 0) 比较,≥ 下界且 < 上界(左闭右开)→ 命中
  - 候选 `route_fixture.early` FROM (MINVALUE, MINVALUE) TO (10, 0):下界 ✓(列 day_seq: 下界 MINVALUE 为负无穷,任何值都更大),上界 ✗(各列与边界逐列相等)✗
  - 候选 `route_fixture.middle` FROM (10, 0) TO (20, 0):下界 ✓(各列与边界逐列相等),上界 ✓(列 day_seq: 10 < 边界值 20)✓

## 拒绝清单(0 行)

(无)
