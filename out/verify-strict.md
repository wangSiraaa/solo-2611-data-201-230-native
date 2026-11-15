# 独立验证报告 — route_fixture.strict

- 状态: **ok**
- 说明: oracle 的 INSERT 均在验证事务内完成并整体回滚,数据库净变化为 0

## 落库核对(readback,expect=loaded)

- 结果: ✓ 一致;库中 2 行,期望 2 行

## oracle:父表真实路由 vs 计划叶(接受行)

| 行 | 计划叶 | oracle tableoid | 字段一致 | 结果 |
|---|---|---|---|---|
| 3 | `route_fixture.strict_mid` | `route_fixture.strict_mid` | ✓ | ✓ |
| 8 | `route_fixture.strict_mid` | `route_fixture.strict_mid` | ✓ | ✓ |

## 拒绝行的数据库路由核对

- 行 1(no_matching_partition):✓ 数据库确认无法路由 — `[23514] no partition of relation "strict_east" found for row`
- 行 2(no_matching_partition):✓ 数据库确认无法路由 — `[23514] no partition of relation "strict" found for row`
- 行 4(no_matching_partition):✓ 数据库确认无法路由 — `[23514] no partition of relation "strict" found for row`
- 行 5(no_matching_partition):✓ 数据库确认无法路由 — `[23514] no partition of relation "strict_east" found for row`
- 行 6(no_matching_partition):✓ 数据库确认无法路由 — `[23514] no partition of relation "strict" found for row`
- 行 7(no_matching_partition):✓ 数据库确认无法路由 — `[23514] no partition of relation "strict" found for row`

