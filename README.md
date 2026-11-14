# 受限工作区内的多DISTINCT精确聚合

本目录只包含任务输入和复现实验材料，不含待实现的工具。

批处理需要按group同时计算行数、金额总和、不同user数及不同(user,product)对数，单个热点组的去重集合也可能超过内存。请用Python 3.11实现aggregate命令，读取已有JSONL及config，输出每组一条精确结果和溢写运行报告。group、user、product可为字符串或NULL；NULL group本身是一个分组。count计所有行；sum只计非NULL金额，全NULL组的sum为NULL；user distinct忽略NULL；pair distinct仅计两列均非NULL的完整二元组，字符串不能用分隔符拼接代替无歧义编码。
金额是至多两位小数的十进制字符串，精确按整数分或Decimal处理；非法字段、重复row_id或超出声明类型使本次失败。重复业务内容若row_id不同仍各计一行。输出NULL组在前，其余group按UTF-8字节排序，sum统一两位小数，不使用浮点中转。
实现有限驻留状态的溢写与归并。config的state_entry_limit约束同时驻留的分组累计项与distinct键项总数；不含当前输入行、固定数量I/O缓冲和解释器开销。为这些项给出明确计数口径，达到上限就落盘；同一热点组不能因为只有一个group就让distinct集合无限增长。多个run之间出现同一个distinct键只能计一次，两个distinct度量不能共用错误的去重域。
merge_fan_in限制单次打开的输入run数量；需要时多轮归并，不把全部run或某个热点组整体读入Python容器。核心聚合与溢写由本工具实现，可使用标准文件/堆工具；SQLite或DuckDB只用于独立结果核对，不把整次GROUP BY交给数据库后报告自有内存预算。
提供运行报告中的最大驻留项、run数和归并轮数，并保留可核查的计数入口。用现成数据在16项预算下触发多run及多轮归并，与独立SQL或小数据Counter核对每组所有指标；改变预算和输入顺序结果应相同。覆盖NULL/空串、跨run重复、金额抵消、只有NULL金额及空输入。规模限制为万行以内，不做大压测；交付源码、临时文件生命周期说明和运行命令。

输入清单：
- `fixtures/rows.jsonl`
- `fixtures/invalid.jsonl`
- `fixtures/config.json`

运行环境为 Python 3.11。
