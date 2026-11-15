# load 报告

- 父表: `route_fixture.events`
- 状态: **committed**
- 目录指纹复核: `a255c8611c6032ef0ca3f915ce7f21c61e32a7e8da26f7b5a2e47f8f8354eb47`(与计划一致)
- 输入摘要复核: `bbc1d5a8fc68f59e3de82aead6a1399456e7d9441a036fee104344269feb549d`(与计划一致)
- 事务: 单事务:只读复核 + 全部 COPY,一次提交
- 总行数: 8

| 叶分区 | OID | COPY 批次数 | 行数 |
|---|---|---|---|
| `route_fixture.early` | 17164 | 1 | 2 |
| `route_fixture.middle` | 17170 | 1 | 3 |
| `route_fixture.regional_default` | 17176 | 1 | 1 |
| `route_fixture.null_tenant` | 17182 | 1 | 1 |
| `route_fixture.other_tenant` | 17188 | 1 | 1 |

