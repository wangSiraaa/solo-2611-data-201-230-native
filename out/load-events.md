# load 报告

- 父表: `route_fixture.events`
- 状态: **committed**
- 目录指纹复核: `22001ce14757dc68152d66d49c50376156c1eb1bf5070f874117d1cfc70ab5ac`(与计划一致)
- 输入摘要复核: `bbc1d5a8fc68f59e3de82aead6a1399456e7d9441a036fee104344269feb549d`(与计划一致)
- 事务: 单事务:只读复核 + 全部 COPY,一次提交
- 总行数: 8

| 叶分区 | OID | COPY 批次数 | 行数 |
|---|---|---|---|
| `route_fixture.early` | 17058 | 1 | 2 |
| `route_fixture.middle` | 17064 | 1 | 2 |
| `route_fixture.regional_default` | 17070 | 1 | 2 |
| `route_fixture.null_tenant` | 17076 | 1 | 1 |
| `route_fixture.other_tenant` | 17082 | 1 | 1 |

