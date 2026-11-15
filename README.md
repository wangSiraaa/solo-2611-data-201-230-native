# Modbus 寄存器批量读取与一致观测入仓

只读的 Modbus TCP 采集器（功能码 03 / holding registers）。它把一轮采样整理成
带质量标记的规范 JSON：每个点都带有原寄存器字、解码字节串、来源请求，以及整轮的
版本围栏。

材料、点表和故障场景见 `fixtures/`；本目录是实现。

## 入口

```bash
# 只看请求计划（不连设备，不读写）
python3 -m modbus_collector plan --points fixtures/points.json

# 采集一轮；--output 落盘，--brief 省略逐请求明细
python3 -m modbus_collector collect --points fixtures/points.json \
    --host 127.0.0.1 --port 11502 --retries 2 \
    --output out/collect.json

# 按点检查已落盘结果（文本或 --json）
python3 -m modbus_collector inspect --input out/collect.json [--only-invalid] \
    [--quality <点的质量名>] [--point-id <id>]
```

`collect` 的退出码：`0` = 完整一致快照，`2` = 采集完成但快照不完整（仍会写出
实际观测），`1` = 配置/计划/连接错误。

运行全部故障场景并做独立核对（会自行起停本地模拟器）：

```bash
python3 scripts/run_scenarios.py          # 产物写入 out/scenarios/
python3 -m unittest discover -s tests     # 纯离线单元测试
```

## 读取计划

点表的地址是 **零基** holding-register 地址。规划逻辑（`modbus_collector/planner.py`）：

1. 每个点的 `[address, address+width)` 求并集；相同或重叠的区间合并成一段，
   所以共享寄存器（例如 `counter` 与 `alias-low` 的地址 2）只读一次。
2. 合并后的每一段按 `forbidden_ranges`（左闭右开）切分；点表校验会拒绝
   “跨在禁用区间上的点”，因此不会有请求跨越禁用地址。
3. 在每个连续段内，从最低未读地址开始，在 `max_registers` 限制下尽量把后续
   整点并入同一请求；请求游标跳到已覆盖的最大末尾。这样请求区间互不重叠、
   每个寄存器只出现一次，且 32 位点永远完整落在单个请求中，不会被拆到两轮。
   若某点无法整个放入，规划直接失败（`PlanError`），而不是悄悄拆分。

对示例点表（`max_registers=4`，禁用 `[6,8)`）得到的数据请求：
`0..2`（signed, counter, alias-low）、`4..5`（swapped）、`8..9`（temperature）。
外加上下两次独立的版本寄存器（100）读取，每轮共 5 个请求。

## 一轮的结构与质量标记

每轮按固定顺序：**版本前围栏 → 数据请求 → 版本后围栏**。版本寄存器只单独读取
（`count=1`），不与其他寄存器合并。

- `coherent`：前后版本都成功、均为偶数且相等，并且所有数据请求都正常应答。
  `complete_snapshot = true`。
- `incoherent`：版本为奇数或前后不等（设备可能正在更新），或围栏本身失败。
  此时整轮最多重读 `retries` 次（默认 2，即至多 3 轮）。耗尽后保留最后一轮的
  实际观测，但绝不声明完整快照。
- `degraded`：版本围栏稳定，但有数据请求异常/协议错配；失败区间之外的观测照常
  保留，整轮完整性降级。

版本号仅用于判定本题设备这一轮是否被换过；不推导永久递增编号，也不推导跨重启
顺序。每次重读都是完整一轮（各自重新读前后版本），不会把两轮数据拼接。

## 协议校验

`modbus_collector/modbus.py` 对每个响应逐项核对：MBAP transaction id、protocol
id、unit id、length、功能码、异常 PDU 形态、byte count 与实际数据长度。任何一项
不符都抛 `ProtocolError` 并记入 `response_mismatches`/`Exchange.error`，该帧的数据
一律不解释为任何请求的值；出错后会丢弃连接，避免迟到帧被误认成后续响应。

数据异常响应（功能码 `0x83`）不重试整轮：受影响的点标为 `exception`，并记录异常码
（如 `02 illegal_data_address`）。

## 解码

`modbus_collector/decode.py` 严格按点表：

- `byte_order` 作用于每个 16 位寄存器的两个字节；
- `word_order` 作用于 32 位值的两个寄存器：先按 `word_order` 排列寄存器，再按
  `byte_order` 排列每个寄存器的字节，形成解码字节串，再用 `struct` 解出值；
- 缩放只使用点表写明的十进制系数（`Decimal`），不猜单位、不补指数；
- `float32` 的 NaN/Infinity 保留原寄存器与原始字节，`value=null`、`valid=false`、
  质量 `invalid_nonfinite_float`，不会写成成功的 JSON 数字（`allow_nan=False`）。

每点的记录字段：`raw_registers`、`raw_bytes_hex`、`decoded`（kind/scale/value）、
`value`、`valid`、`quality`、`source_request`（含 transaction id）、
`version_fence`（本轮前后版本号）以及出错时的 `error`。

## 场景核对

`scripts/run_scenarios.py` 对每个模式：起模拟器 → 跑 `plan`/`collect` → 停模拟器
→ 读其请求日志。核对内容包括：

- 日志里 **实际** 每轮的数据区间逐项等于计划数据区间，且前后都有版本寄存器读取；
- 不重复读同一区间、不跨 `[6,8)`、不超过 `max_registers`、不包含“半截点”；
- 用独立的 `struct` 代码从采集器输出的原寄存器手选字节重新解码，值与字节均一致；
- 各场景的质量/完整性/轮数预期：
  `normal` → coherent/1 轮；`change-once` → 首轮 incoherent、重读后 coherent/2 轮；
  `changing` → 3 轮后 incoherent 且保留观测；`exception` → degraded，仅温度点异常；
  `wrong-tid` → degraded，首帧 tid 错配不被采用、其余数据保留；
  `nonfinite` → coherent，NaN 点无效但保留字节。

结果写入 `out/scenarios/<mode>/{plan.json,collect.json,requests.jsonl,verification.json
,summary.json}`。
