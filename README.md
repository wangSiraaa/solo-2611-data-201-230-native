# 分区完成事件驱动的多源ETL装配

这里仅提供输入材料和源端模拟。使用独立临时输出目录运行，不改写 `fixtures/` 中原始材料。

## 实现

`partition_etl/` 是 Python 3.11 标准库实现（无第三方依赖）。固定两源（`sales`、`fx`）一输出：

- 目标日 D 的 sales 取同日 sales 分区；fx 取 D 的前一自然日分区。
- 分区“当前有效输入”= 该来源已收到并通过校验的最高版本；不会扫描目录自行发现未通知的文件。
- 版本跨来源独立，不要求版本号相等。

### 事件校验顺序（拒绝即跳过，后续事件继续处理）

1. 路径必须在 `input_root` 内（拒绝绝对路径、`..`、反斜杠、越界）。
2. 文件可读，且字节 SHA-256 与通知一致。
3. 重新校验 CSV 表头、列数、主键唯一、金额/汇率为合法十进制（金额非负，汇率正）。
4. `event_id` 不得描述不同内容（否则 `event_id_conflict`）。
5. 同来源/分区/版本不得映射到不同内容（否则 `version_content_conflict`）；同内容重复通知记为 `duplicate`。

通过的字节会存为不可变快照，ETL 只读取快照并再次校验其摘要。

### ETL 与输出

- 金额与汇率用 `Decimal` 计算，`ROUND_HALF_UP` 保留两位。明细保留订单原值与原汇率。
- 依赖不齐时**不调用** ETL。
- 缺汇率 / 坏销售数据时本次尝试失败：保留最近成功输出与 generation，不改字节；`status` 会标明最新输入尚未成功处理。
- 只有实际有效输入向量发生变化的输出才重建；其余输出文件字节与 generation 不变。
- 每次输出版本绑定精确的 sales/fx 分区、版本与内容摘要（`assemblies/<date>/current.json`），并保留历史 generation。
- 每次 ingest 的 run 记录所有实际 ETL 调用（有 `etl` 列表，含输入向量、结果），重复通知不会偷偷全量重算。

## 使用

```bash
./partition-etl ingest-events --config fixtures/config.json \
  --events fixtures/events.jsonl --out /tmp/out

./partition-etl status --config fixtures/config.json --out /tmp/out

./partition-etl export --config fixtures/config.json --out /tmp/out \
  --date 2026-01-02 --to -        # 或 --to some/dir（写入 <date>.csv）
```

`--out` 必须是一个独立的输出/状态目录；状态文件为 `<out>/state.json`。`status` 的每个目标日包含：

- `required`：精确依赖分区
- `latest_available_vector` / `missing_dependencies`
- `recent_success_vector` / `recent_success`（最近成功 generation 与其输出路径、摘要）
- `latest_inputs_processed` 与 `failure_reason`
- `last_failure`、`last_rejection`

## fixture 事件顺序覆盖的场景

`fixtures/events.jsonl` 已提供：

- sales 01-02/01-03 先到，fx 后到；
- fx 01-01 v1→v2 修订只影响 01-02；
- fx 01-02 v1 缺 EUR 使 01-03 一次 ETL 失败，v2 补齐后成功；
- 重复通知（e3/e5）；
- e8 复用 01-01 v2 版本号但内容不同 → 拒绝；
- e9 只有 sales 01-04，缺 fx 01-03，不产生输出。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖上述顺序、迟到旧版本不回退、选择性重算、同版本冲突、摘要/路径拒绝、`ROUND_HALF_UP` 等。
