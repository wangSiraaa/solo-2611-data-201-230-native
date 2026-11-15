# 独立验证报告 — route_fixture.events

- 状态: **ok**
- 说明: oracle 的 INSERT 均在验证事务内完成并整体回滚,数据库净变化为 0

## 落库核对(readback,expect=loaded)

- 结果: ✓ 一致;库中 8 行,期望 8 行

## oracle:父表真实路由 vs 计划叶(接受行)

| 行 | 计划叶 | oracle tableoid | 字段一致 | 结果 |
|---|---|---|---|---|
| 1 | `route_fixture.early` | `route_fixture.early` | ✓ | ✓ |
| 2 | `route_fixture.early` | `route_fixture.early` | ✓ | ✓ |
| 3 | `route_fixture.middle` | `route_fixture.middle` | ✓ | ✓ |
| 4 | `route_fixture.regional_default` | `route_fixture.regional_default` | ✓ | ✓ |
| 5 | `route_fixture.regional_default` | `route_fixture.regional_default` | ✓ | ✓ |
| 6 | `route_fixture.null_tenant` | `route_fixture.null_tenant` | ✓ | ✓ |
| 7 | `route_fixture.other_tenant` | `route_fixture.other_tenant` | ✓ | ✓ |
| 8 | `route_fixture.middle` | `route_fixture.middle` | ✓ | ✓ |

## 拒绝行的数据库路由核对


