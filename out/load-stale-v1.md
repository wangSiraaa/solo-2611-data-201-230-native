# load 报告

- 父表: `route_fixture.events`
- 状态: **stale_rejected**
- 旧计划被拒绝,未写入任何行。原因:
  - 目录指纹已变: 计划 62c8b653f0cb5ec47ffcbd857f72e268333c1019d15fcaf8cb0be7b860e076ff != 当前 a255c8611c6032ef0ca3f915ce7f21c61e32a7e8da26f7b5a2e47f8f8354eb47
  - 分区变更: oid=17170 route_fixture.middle: bound: {"kind": "range", "lower": [{"t": "value", "v": 10}, {"t": "value", "v": 0}], "upper": [{"t": "value", "v": 20}, {"t": "value", "v": 0}]} -> {"kind": "range", "lower": [{"t": "value", "v": 10}, {"t": "value", "v": 0}], "upper": [{"t": "value", "v": 25}, {"t": "value", "v": 0}]}

