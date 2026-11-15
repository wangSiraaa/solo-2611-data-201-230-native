# Morton 分块存储：布局说明与运行命令

实现文件：`morton_store.py`（Python 3.11，仅标准库）。子命令：`build / plan / query / verify / selftest`。

## 1. 磁盘布局

```
store/
├── layout.json            # 布局清单（格式版本、块行数、码界限、摘要、块目录）
└── blocks/
    ├── block-0000.jsonl   # 不可变块：1 行块头 + 若干数据行
    ├── block-0001.jsonl
    └── ...
```

### layout.json

| 字段 | 含义 |
|---|---|
| `format_version` | 布局格式版本，当前为 `1` |
| `bits_per_axis` | 每轴位数，固定 `8`（16 位 Morton 码） |
| `block_rows` | 每块最大行数（来自 config 的 `rows_per_block`） |
| `default_max_intervals` | 查询默认区间预算（来自 config 的 `max_intervals`） |
| `code_min` / `code_max` | 全局码最小/最大值（空集为 `null`） |
| `summary` | 摘要：`total_rows`、`num_blocks`、源文件路径与 `source_sha256`、排序键与码制说明 |
| `directory` | 块目录，见下 |

块目录每条目：`{block_id, path, rows, code_min, code_max, sha256}`。
`code_min/code_max` 是该块全部行的 Morton 码界限（块内按排序键有序，即首行码与末行码），
**剪枝只用此码界限，不以某块首行坐标代替整块空间范围**；`sha256` 为块文件全字节摘要，用于索引绑定校验。

### 块文件（普通 JSONL，不可变）

```
{"format":"morton-block/1","block_id":0,"rows":16}                              ← 块头
{"row_id":"p000-190","x":0,"y":190,"value":190,"code":35840}                    ← 数据行
...
```

- 排序键：`(morton_code, row_id 的 UTF-8 字节)`，块内严格升序，块间码界限单调不交叠。
- Morton 码：16 位，交错 x/y 各 8 位，**码 bit0 = x 的 bit0**，bit1 = y 的 bit0，依此类推。
- 相同坐标的多条事件各自成行，身份（row_id）互不丢失。

### 校验（build 与每次读块时）

- build：拒绝重复 `row_id`、非字符串 `row_id`、越界/非整数坐标（允许 0..255，布尔值拒绝）、非整数 `value`、缺字段。
- 读块（query/verify）：校验文件 `sha256`、块头 `block_id` 与目录一致（索引绑定）、行数一致、
  每行 `code == morton(x,y)`、`(code,row_id)` 严格升序、目录码界限与实际首末行一致。
  任一不符抛 `CorruptBlockError`（明确失败，不静默跳过）。`verify` 另查目录连续性、单调性与 summary 一致性。

## 2. 矩形 → Morton 区间（plan）

1. 矩形 `[x0,x1)×[y0,y1)`（端点 0..256，空区间合法）裁剪到 256×256 整数域。
2. 与完整域的四叉网格相交：完全包含的胞格输出为一个左闭右开码区间
   `[prefix, prefix+4^(8-level))`；部分相交递归细分；不相交跳过。**不是只算两个角点**。
3. 码上首尾相接的相邻区间精确合并（不引入额外点）。
4. 超出 `max_intervals` 预算时，反复合并"相邻间隔（gap）最小"的一对区间，
   合并结果是**更保守的覆盖**（gap 内的码可能落在矩形外），但绝不漏掉矩形内任何整数点；
   每次合并记入 `merge_log`（依据、被合并对、结果、gap 大小）。
5. 用候选区间与目录码界限做重叠剪枝得选中块：`lo <= code_max 且 code_min <= hi-1`。

plan 产物含：裁剪后矩形、精确区间及数量、合并日志、最终候选码区间、选中块及其码界限与命中区间。

## 3. 查询执行（query）

- 只打开选中块（读块时做上述全部完整性/绑定校验，源块损坏即明确失败）。
- 块内行先按码区间过滤得**候选行**（计入 `candidate_rows`），再用原 `x/y` 做精确残差过滤
  `x0<=x<x1 且 y0<=y<y1`——保守区间带入的图外点在此剔除，不会出现在结果里。
- 结果按 `row_id` UTF-8 字节序排序；重复坐标的每条事件独立保留。
- 产物记录**实际**打开的块 ID 列表与每块读取入口（`read_log`：块 ID、路径、读取行数、候选行数），
  来自真实执行，非计划推测。

## 4. 运行命令

```bash
# 构建（config 指定每块 16 行；--block-rows 可覆盖）
python3 morton_store.py build --input fixtures/points.jsonl --config fixtures/config.json --out store

# 校验布局与全部块
python3 morton_store.py verify --store store

# 计划 / 查询（单个：--id 或 --rect X0 X1 Y0 Y1；--max-intervals 覆盖默认预算 8）
python3 morton_store.py plan  --store store --queries fixtures/queries.json --id tiny
python3 morton_store.py query --store store --rect 120 140 10 210 --max-intervals 8

# 现成查询全量执行，产物写入目录
python3 morton_store.py plan  --store store --queries fixtures/queries.json --all --out artifacts
python3 morton_store.py query --store store --queries fixtures/queries.json --all --out artifacts

# 全面自测（oracle 核对 + 覆盖枚举 + 预算不变性 + 损坏注入），写验证报告
python3 morton_store.py selftest --store store --input fixtures/points.jsonl \
    --queries fixtures/queries.json --out artifacts/verification_report.json
```

## 5. 验证方法（selftest，17 项）

- **Morton 码**：全部 65536 个 (x,y) 编解码往返一致；bit 序符合"最低位为 x 最低位"。
- **oracle 核对**：以原 JSONL 逐点过滤为独立 oracle，5 个现成查询（tiny/cross/upper/empty/full）
  在预算 {1,2,3,4,8,16,64,1024} 下结果逐一相等——**改变预算不改变最终结果**。
- **无漏覆盖**：对每个 plan、每个预算，枚举 8 位域内矩形全部整数点，断言每点的码都落在某候选区间内；
  未发生合并时进一步断言区间覆盖的码集与矩形点码集**恰好相等**（不保守）。
- **随机小矩形**：固定种子 20261115 生成 300 个（含空区间、域上边界 x1/y1=256），
  另加全域/单点/竖条等 6 个，×4 种预算，结果与覆盖全部通过。
- **读取入口**：tiny 查询实际只打开 1/15 个块（记录来自真实 query 执行的 `read_log`）。
- **重复坐标**：(0,0) 的两条事件在 full 查询中全部保留。
- **空查询**：无候选区间、不开块、结果为空。
- **损坏注入**：块文件 bit-flip、两块文件调换、目录码界限篡改，均明确抛错；
  build 侧重复 ID/坐标越界/布尔坐标/非整 value/缺字段均被拒绝。

## 6. 查询产物（artifacts/）

`plan_{tiny,cross,upper,empty,full}.json`、`query_{tiny,cross,upper,empty,full}.json`、`verification_report.json`。
本次运行：tiny 1/15 块 5 行，cross 11/15 块 11 行，upper 1/15 块 1 行（域上边界角点 (255,255)），
empty 0 块 0 行，full 15/15 块 226 行。

## 7. 范围说明（按要求不做）

冻结输入、单机、几百到几万点、普通文件块；不做 GIS 重投影、浮点几何、分布式路由、在线更新、B+ 树。
