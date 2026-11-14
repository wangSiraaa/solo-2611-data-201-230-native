# 批量关联子查询的NULL感知执行

本目录只包含任务输入和复现实验材料，不含待实现的工具。

一个离线报表把每条外表记录送去查询内表，重复customer_id导致N+1读取。请用Python 3.11和SQLite实现受限批量关联执行器。prepare.py可建立fixtures的真实小数据库，输入schema.json和typed JSON plan，不解析任意SQL。计划只含一张外表、至多两张内表、一个整数等值关联键，以及EXISTS、标量子查询、IN/NOT IN结果列。
按外表row_id顺序处理有限批，提取本批不同的非NULL关联键，以参数化查询批量取内表行，再将结果分别附回每条外行。相同关联键的外行不能被折叠，probe不同仍须产生各自的IN结果；不相关的内表行不应全表读入。相同内表与关联列的多个表达式应共享本批读取，数据库执行统计须证明其查询次数随批和来源数增长，而不是随外行数增长。
关联键比较采用SQL等号，NULL外键不匹配NULL内键。EXISTS仅看是否存在匹配行，内列NULL不影响存在性。标量子查询零行返回NULL、一行返回该值、超过一行使整次查询以具体外行和内行身份报错，不能任选第一条。IN若找到等值为TRUE；无等值但非空候选集合中涉及NULL比较为UNKNOWN；空集合一律FALSE，包括NULL IN空集。NOT IN按三值取反，输出JSON的true/false/null。
实现结果时明确区分空候选、单个NULL和多个候选，不能将内表集合放进Python set后丢掉NULL信息。保留外表全部原列和声明的表达式顺序，表名、列名必须经schema白名单验证，值使用绑定参数。错误查询不得发布部分结果。
用独立SQLite相关子查询逐行核对EXISTS/IN/NOT IN；SQLite对标量多行会取首行，因此标量部分另用独立候选计数核查题定错误语义，不把它当正确oracle。现成数据包含重复外键、无内行、内NULL、外NULL与标量多行。调整batch_size结果相同，在一个来源加入大量不相关键不应扩大本批返回内行数。交付run、执行统计与README；不做嵌套两层相关、任意过滤或通用优化器。

输入清单：
- `fixtures/seed.sql`
- `fixtures/plan.json`
- `fixtures/scalar-plan.json`
- `fixtures/schema.json`
- `prepare.py`

运行环境为 Python 3.11。
