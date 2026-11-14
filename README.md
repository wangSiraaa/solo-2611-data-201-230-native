# 保留旧SQL接口的宽表纵向拆分

本目录只包含任务输入和复现实验材料，不含待实现的工具。

报表经常只读记录的基本字段，大附件让宽表存储变重，但旧客户端仍使用同一个表名读写。请用Python 3.11和PostgreSQL 16实现一次停写窗口内的纵向拆分迁移。fixtures/schema.sql给出已有宽表和真实小数据，contract.json冻结旧SQL接口；只在本题独立库操作，不做前端或在线双写。
将id、tenant、name、status存为主表，note和attachment存为以id引用主表的可选详情表；仅当至少一个冷字段非NULL时保存详情行。空字符串和空bytea是有值，不能当成NULL或缺详情；保留原id和所有字节。详情外键随主行删除清理，原主键、非空、status默认值及CHECK仍有效。
在旧名split_fixture.records上提供列名、顺序、类型相同的可写兼容视图。实现旧接口的SELECT、显式id INSERT、普通UPDATE、DELETE和RETURNING；id按契约不可变，尝试改id应明确失败。旧INSERT省略status仍得到active。只改热字段不应创建空详情；冷字段从全NULL变为有值时新增详情，恢复全NULL时移除详情，同时保留主行。多行语句任一行违反约束时，整条语句应回滚，不能留下部分主表或详情修改。
迁移执行前检查目标对象冲突与输入契约，在一个事务内搬数据和切换旧名；失败恢复原宽表可用状态。迁移成功后保留可审查的结构与数量报告，不靠复制一个只读视图冒充旧写入可用，也不保留另一张持续接收写入的宽表掩盖两套状态。这里只要求停写窗口，之后单库正常事务，不实现CDC或自动回滚服务。
使用给定client-cases逐条验证，并以一份未迁移宽表副本作为合法语句的独立对照，比较SELECT/RETURNING列类型、NULL、bytea和完整行内容；不可变id是契约额外限制，单独验证。对失败语句核查主/详情与执行前一致，再测试一个事务内先改冷字段后ROLLBACK。额外查询底层详情表确认全NULL行确实不存在、空值字节未被删。交付迁移命令、数据库脚本、对照验证和README，不扩展成任意表拆分框架。

输入清单：
- `fixtures/schema.sql`
- `fixtures/client-cases.json`
- `fixtures/contract.json`

运行环境为 Python 3.11、本地 PostgreSQL 16。
