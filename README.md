# Parquet统计驱动的保守筛选与列读取计划

本目录只包含任务输入和复现实验材料，不含待实现的工具。

离线明细目录里有许多正常的Parquet行组，分析员只想取少数记录。请用Python 3.11和PyArrow实现plan与scan，输入fixtures/files.json和queries.json，输出查询结果Parquet及行组选择说明。只支持已有平面schema、整数/字符串比较、IS NULL、AND/OR/NOT和列投影，不解析SQL，不做损坏恢复。
计划阶段只读footer。根据每个row group的min/max、null_count和行数判断条件是否可能为TRUE，不能把UNKNOWN直接当FALSE后取反。缺失或不可靠统计要保守保留；AND/OR组合不能假设不同列的极值来自同一行。只有能证明没有TRUE行时才能跳过；报告采用的统计、推理路径与未知项。
执行时先读保留组的过滤列，按SQL三值逻辑形成精确行选择，再为确有命中行的组读取剩余投影列。过滤列同时也是输出列时应复用，不能重复读该列；无命中组不读payload等输出专用列。保留重复数据及原文件、行组、组内行号，输出按输入文件顺序及原物理行序排列。缺失统计的文件照常得到正确结果。
记录实际调用的row group与列集合，把“计划跳过”“过滤后无命中”“读取了哪些列”分清。记录的是工具发起的列读取请求，不把footer给出的压缩大小冒充操作系统真实读字节。读取计数应由执行入口产生，不根据预期计划事后填写。
用无剪枝的完整扫描独立核对结果、顺序和来源，覆盖全NULL组、无统计、否定条件、OR跨列及空结果。现成样例至少有一个范围查询应跳过某些有统计组，且无命中组不触发投影专用列请求；再将同一数据改为不写统计，结果应一致而计划更保守。交付源码、计划格式、实际运行记录和README；不做page index、远程Range服务或大规模性能测试。

输入清单：
- `fixtures/files.json`
- `fixtures/stats.parquet`
- `fixtures/no-stats.parquet`
- `fixtures/queries.json`

运行环境为 Python 3.11。
