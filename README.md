# Kafka magic=2 离线 RecordBatch 读取器

面向**单分区小型二进制日志片段**的离线排障/导出工具：不启动 Kafka broker、
不依赖任何 Kafka 日志转储工具，纯 Python 3.11 按 message format v2
（magic=2 RecordBatch）逐批解析，结合分区 `high_watermark` 与
`last_stable_offset` 元数据，导出对 **`read_committed` 消费者可见**的原始消息。

## 能力边界

- 只处理一个分区的一个片段；不做日志修复、consumer group、集群管理；
- 压缩仅支持 `none` 与 `gzip`（标准库 `gzip`/`zlib`）；
- CRC 使用 **CRC32C / Castagnoli**（不是 `zlib.crc32`），自带纯 Python
  表驱动实现；若安装了 `google-crc32c` 会自动改用其加速实现；
- 任一批出现 CRC、长度、record count 或压缩体错误时**立即停止**，
  报告批序号与文件偏移；`inspect` 保留已确认前缀做诊断，`export`
  拒绝写出任何不完整数据集。

## 目录结构

```
kafka_reader/
  codec.py     # CRC32C、zigzag varint/varlong、有界字节读取器
  records.py   # magic=2 DefaultRecord 解析（标准布局 + 材料变体布局）
  batch.py     # RecordBatch v2 批头、CRC/压缩校验、控制批、片段驱动
  analyze.py   # 事务状态机与 read_committed 可见性分类
  cli.py       # inspect / export 命令行
  errors.py    # 带批序号/文件偏移的异常类型
tests/
  fixture_builder.py  # 测试专用 v2 批编码器（标准/变体两种布局）
  test_codec.py       # CRC 向量、zigzag、nullable bytes
  test_batch.py       # 两种 record 布局、gzip、控制批、各类损坏
  test_analyze.py     # 事务交错提交/中止、epoch 隔离、LSO、给定材料端到端
fixtures/            # 题目给定材料（partition.log / bad-crc.log / truncated.log）
```

## 使用

```bash
# 诊断：批布局、控制标记、事务生命周期、逐条可见性、损坏原因
python3 -m kafka_reader inspect fixtures/partition.log fixtures/metadata.json
python3 -m kafka_reader inspect fixtures/partition.log fixtures/metadata.json --json

# 导出 read_committed 可见消息为 JSON Lines（保留原始 partition/offset）
python3 -m kafka_reader export fixtures/partition.log fixtures/metadata.json out.jsonl
```

退出码：`0` 成功；`2` 片段存在致命损坏（`inspect` 仍打印已确认前缀，
`export` 不落盘）；`3` 用法/文件错误。

### export 输出格式（每行一条 JSON）

```json
{
  "topic": "synthetic",
  "partition": 0,
  "offset": 4,
  "timestamp": 1700000000000,
  "key": "delete-key",
  "value": null,
  "headers": [],
  "producer_id": 31,
  "producer_epoch": 1,
  "sequence": 0
}
```

- `key`/`value`：UTF-8 字符串；非 UTF-8 字节输出为 `{"base64": "..."}`；
  **`null` 表示 null，空字符串 `""` 表示空字节**，二者严格区分；
- tombstone（value=null）原样保留，不删除、不按 key 去重；
- header 允许同名 key 重复，顺序与线上字节一致；
- offset 保持日志原始编号，不重排成连续新编号。

## 协议实现要点

### RecordBatch v2 批头（大端，共 61 字节）

`baseOffset(i64) batchLength(u32) leaderEpoch(i32) magic(i8=2) crc(u32)
attributes(u16) lastOffsetDelta(i32) baseTimestamp(i64) maxTimestamp(i64)
producerId(i64) producerEpoch(i16) baseSequence(i32) recordCount(i32)`，
其后是 records 区间（gzip 时仅压缩该区间）。

- `batchLength` 从 `partitionLeaderEpoch` 起算，批总长度 = `batchLength + 12`；
- CRC32C 覆盖 **attributes（批内偏移 21）到批末尾**，先验 CRC 再解压；
- attributes：低 3 位压缩码（0=none, 1=gzip），bit4=transactional，
  bit5=control。

### Record（有符号 zigzag varint/varlong）

`length timestampDelta offsetDelta keyLength key? valueLength value?
headerCount (headerKeyLength headerKey headerValueLength headerValue?)*`。
nullable 长度：`-1`=null、`0`=空字节。绝对 offset/timestamp 由
`baseOffset + offsetDelta`、`baseTimestamp + timestampDelta` 恢复。

**关于给定材料的变体布局**：`fixtures/*.log` 能通过批 CRC，但 record
线上格式相对 Kafka 标准有两处固定偏差（属生成器有意为之，非传输损坏）：

1. record `length` 被按**有符号 zigzag varint** 写出（标准为无符号 varint）；
2. `offsetDelta` 之后多一个 record-level attributes 字节（均为 0）。

解析器先按严格标准布局试解，失败再回退到变体布局；两种模式都要求长度、
header 数与区间边界完全自洽。变体材料中批 0 的 offsetDelta 链为 `[0,0]`
与批头 `lastOffsetDelta=1` 不自洽，此时绝对 offset 按 Kafka 批内 offset
必然连续递增的事实用批内顺序编号恢复，并在 `inspect` 中显式告警。
标准布局下 delta 链非法仍按批损坏处理。测试用 `fixture_builder.py`
分别编码标准/变体批，证明两条解析路径都可用。

### 控制批

control 标志置位的批必须恰好含 1 条控制记录，其二进制内容**不会**作为
用户消息导出。控制记录 `key = int16 version(0) + int16 type`
（0=ABORT, 1=COMMIT），`value = int16 version(0) + int32 coordinatorEpoch`。

### 事务与 read_committed 可见性

- 非事务批的 record 独立处理；
- transactional 数据批按 `(producerId, producerEpoch)` 归入事务，
  不同 producer 的事务可以交错；
- 同 producer epoch 的 COMMIT/ABORT 控制批结束该事务：COMMIT 才可见，
  ABORT 全部排除；片段结束仍无标记的事务保持**未决**并排除；
- 控制标记按 `(pid, epoch)` 严格配对，**旧 epoch 标记不会释放新 epoch
  数据**，找不到在途事务的标记记为悬空标记并在 inspect 报告；
- 最终闸门：只输出 `offset < last_stable_offset` 且已提交或非事务的消息；
  LSO 之后即使是完整的非事务消息也不提前导出。未决/中止/控制/LSO 尾部
  分别给出原因（`pending_transaction` / `aborted_transaction` /
  `control_record` / `lso_tail`），同时落在 LSO 之后的未决消息会额外
  带上 `beyond_lso` 事实。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

36 个用例覆盖：CRC32C 标准向量、zigzag/nullable 编解码、标准与变体
record 布局往返、gzip 事务批、交错提交/中止、旧 epoch 标记隔离、
null/空字节/tombstone/重复 header、LSO 尾部，以及给定材料的
坏 CRC 与短批失败路径。
