# 独立验证报告 — route_fixture.events

- 状态: **ok**
- 说明: oracle 的 INSERT 均在验证事务内完成并整体回滚,数据库净变化为 0

## 落库核对(readback,expect=empty)

- 结果: ✓ 一致;库中 0 行,期望 0 行

## oracle:父表真实路由 vs 计划叶(接受行)

| 行 | 计划叶 | oracle tableoid | 字段一致 | 结果 |
|---|---|---|---|---|
| 1 | `route_fixture.middle` | `middle` | — | ✓ |
| | ↳ 行已被路由到 middle(与计划一致),随后触发约束: [23514] new row for relation "middle" violates check constraint "events_value_check" | | | |

## 拒绝行的数据库路由核对


