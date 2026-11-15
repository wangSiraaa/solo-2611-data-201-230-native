# load 报告

- 父表: `route_fixture.events`
- 状态: **rolled_back**
- 失败叶: `route_fixture.middle`(oid=17117),批次 #0
- 数据库错误: [23514] new row for relation "middle" violates check constraint "events_value_check"
- 约束: `events_value_check`(表 middle)
- 本次 load 已整批回滚,0 行落库;未把约束失败伪装成路由无匹配。
- 诊断: 诊断在独立事务内逐行重放失败批次并全部回滚,数据库净变化为 0
  - 输入行 1: `{"row_id": 100, "tenant": "east", "day_seq": 10, "slot": 0, "value": -1}` → [23514] new row for relation "middle" violates check constraint "events_value_check"

