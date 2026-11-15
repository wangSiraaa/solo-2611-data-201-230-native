# X12 856 发运通知受限入仓器

针对 `fixtures/` 中合成 856 子集的本地、离线实现。不联网，不发送 EDI 回执，不是通用 EDI 映射设计器、格式翻译平台或库存系统。

## 入口

```bash
python3 -m x12_856 parse fixtures/shipment.edi --out out/shipment
python3 -m x12_856 parse fixtures/*.edi --out out/all      # 每个输入一个子目录
python3 -m x12_856 boundary                                # 支持边界
python3 -m unittest discover -s tests -v                    # 43 个测试
```

Python API：

```python
from x12_856.loader import parse, parse_file, export
result = parse_file("fixtures/shipment.edi")
paths  = export(result, "out/shipment")
```

`parse` 返回 `ParseResult`，含 `shipments`、`orders`、`packages`、`items`、
`hl_facts`、`segments`、`scope_statuses`、`envelope_checks`、`diagnostics`。

## 分隔符

只从固定长度 106 字节 ISA 的固定偏移读取，不按 `*` / `:` / `~` 写死：

| 偏移 | 含义 |
| ---: | --- |
| 3 | 元素分隔符 |
| 82 | 重复分隔符 (ISA11) |
| 104 | 组件分隔符 (ISA16) |
| 105 | 段终止符 |

`fixtures/shipment.edi`（`*` `:` `~`）与 `fixtures/alternate.edi`（`|` `>` `!`）得到相同业务事实。控制字符（如 0x1C）也可作终止符。

## 封套与失败范围

| 封套 | 检查 | 失败范围 |
| --- | --- | --- |
| ISA/IEA | IEA01 组数、ISA13/IEA02 控制号 | 整个 interchange |
| GS/GE | GE01 事务数、GS06/GE02 控制号 | 整个 group |
| ST/SE | SE01 含 ST..SE 的段数、ST02/SE02 控制号 | 该 transaction |

- `SE01` 按 **ST 到 SE 闭区间** 逐段计数。
- 上层失败向下传播（interchange 失败会 fail 其 group/transaction）；下层失败 **不** 污染兄弟或父级自身的 `status`，父级 `own_status` 保持 `ok`。
- 每个失败都带原始段号（`segment_no`）与起止段（`end_segment_no`），便于回到原文。
- `bad-count.edi`：仅 `SE_COUNT`，transaction 失败，interchange/group 仍 `ok`。
- `bad-cycle.edi`：`HL_CYCLE` + `HL_LEVEL_PATH`，transaction 失败，带 HL3 段号与环路径。

## HL 身份图与归属

- `HL01` 在 **同一 transaction 内** 唯一；重复即 `HL_ID_DUPLICATE`，两份仍是两条事实，绝不因相同包装码合并。
- `HL02` 为空表示根；根必须是 `S`，且每事务恰好一个。
- 接受 **父段晚于子段** 的历史导出变体（前向引用），但只在本事务内解析；跨事务或跨 interchange 的同号 `HL01` 不可作为父项 → `HL_PARENT_MISSING`。
- 仅允许 `S→O→P→I` 与 `S→O→I`；其他边（含环）→ `HL_LEVEL_PATH` / `HL_CYCLE`。
- 缺父不重挂到“最近前序 HL / 前一个订单”；该 HL 的依赖输出整体不产出。
- 局部字段归属：从一个 `HL` 段到 **下一个 `HL` 段之前**。`PRF` 只在 `O`，`MAN` 只在 `P`，`LIN`/`SN1` 只在 `I`；错层 → `LEVEL_MISMATCH`。`BSN` 是事务级，出现在首 `HL` 之后 → `BSN_NOT_TRANSACTION_LEVEL`。
- 未知段逐字保留、标 `warning`，不改变层级。

## 输出表与来源

`export` 生成：

- `shipments.csv`、`orders.csv`、`packages.csv`、`items.csv`：业务关系事实。
- `hl_groups.csv`：每个 HL 及其祖先链、段范围、有效性、问题码（含失败事务）。
- `segments.csv`：逐段原文、元素、所属 interchange/group/transaction、`owner_hl_id`、HL 局部段范围、状态与原因。
- `scopes.csv`、`diagnostics.csv`、`envelope_checks.csv`、`delimiters.json`、`summary.json`。

每行带 `interchange_no`、`group_no`、`transaction_no`、`transaction_global_no`、`st_control_number`、`st_segment_no`、`se_segment_no`、`source_segment_start/end`，可精确回到原 ST 与原段范围。`envelope_checks.csv` 由段表独立重数，不复用状态位，可用手算小图核对。

## 数量

- 保留 `Decimal` 与其原始字符串；`2.50` 不会被改写成 `2.5`。
- 单位原样保留，不换算、不跨单位合计、不做库存聚合。
- 拒绝非正数、非十进制（如 `1e3`）、空单位。

## 支持边界

只支持 `fixtures/scope.json` 列出的段：`ISA IEA GS GE ST SE BSN HL PRF MAN LIN SN1`，允许 `TD1 REF DTM` 作为非结构化扩展。不实现任意 EDI 映射、release/escape 处理、重复元素展开、复合元素语义解释、库存业务或回执发送。
