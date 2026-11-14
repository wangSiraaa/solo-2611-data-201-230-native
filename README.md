# PostgreSQL多级分区目录驱动的批量路由

本目录只包含任务输入和复现实验材料，不含待实现的工具。

入仓前需要知道每条记录将进入哪个叶分区，并向上游解释无目的地的记录；直接INSERT只能在执行时给错误，不能提供可审阅的全批计划。请用Python 3.11和本地PostgreSQL 16实现plan与load。fixtures/schema.sql创建两个真实分区父表，输入rows.jsonl；只在本题独立数据库运行，不连接业务库。
plan只读数据库目录，支持最多两级LIST及RANGE分区，键为整数或text普通列；本题text键显式使用C collation，其余collation、HASH和表达式分区拒绝。读取真实层次、键列和边界，不硬编码现成表名。逐行输出经过的分区、每层比较依据与最终叶；无匹配或类型不符的行进入拒绝清单，不把它们路由到随意挑选的叶。LIST中的NULL与DEFAULT区分；复合RANGE按PostgreSQL词典序及左闭右开边界判断，MINVALUE/MAXVALUE不等于普通整数，含NULL的range键应按真实DEFAULT规则处理。
DEFAULT是同层其他分区覆盖的补集，不能先遇到DEFAULT就选择它。计划须绑定输入摘要与涉及分区的OID、键和边界指纹；load前再只读核对，目录或输入已变则拒绝旧计划。这里约定load期间无DDL，不实现在线目录变更协议。
load只加载计划已接受的行，以计划分组批量COPY到实际叶表，在一个数据库事务内完成并保留原始字段。不能通过隐藏写入/回滚来冒充plan的只读路由；独立验证时可以INSERT父表并读tableoid作为oracle。约束错误应使本次load整批回滚，报告错误叶与输入行，不把CHECK失败伪装成路由无匹配。现成constraint-failure文件用于这一场景。
对events和缺少DEFAULT的strict分别运行，比较NULL、复合边界前后、未知tenant和缺失目的地。独立用父表真实路由核对每条接受行的tableoid和完整字段；拒绝行核对数据库不能路由。修改一个边界后旧计划应失效，再以新计划运行。交付源码、只读路由报告、COPY验证及README，不创建新分区、不迁移旧DEFAULT数据、不做在线回填或通用SQL解析器。

输入清单：
- `fixtures/schema.sql`
- `fixtures/rows.jsonl`
- `fixtures/constraint-failure.jsonl`
- `fixtures/config.json`

运行环境为 Python 3.11、本地 PostgreSQL 16。
