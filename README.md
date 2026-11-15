# HTML多层表头与跨格数据的结构化采集

这是题目输入材料与受限模拟环境，不包含采集器/转换器实现。所有内容为本地合成数据。协议范围和启动说明见 fixtures/README.md 及 scope.json。

## 实现：tablefreeze.py

Python 3.11，仅标准库，不运行 JavaScript、不爬取其他页面。按 scope.json 的
`table_ids` 处理指定 table；嵌套 table 属于另一张表，其文字与格子不进入父表。

```bash
# 抽取并发布产物（每个 table 一个输出子目录）
python3.11 tablefreeze.py extract --html fixtures/catalog.html \
    --scope fixtures/scope.json --out out/

# 打印逻辑网格、单元格与表头关联，便于人工追查
python3.11 tablefreeze.py inspect --html fixtures/catalog.html --scope fixtures/scope.json
```

退出码：`0` 全部成功；`2` 至少一张表被拒绝（fatal）；`1` 用法/输入错误。

### 产物（out/&lt;table_id&gt;/）

- `long_table.csv` — 可入仓长表。每个逻辑数据位置一行：行组 ID（行组元素 id，
  缺省 `tag#序号`）、组内行号、列号（均从 0 开始）、原始 cell ID、`slot_role`
  （origin/shared/missing）、原文本、`data-type`、规范值、值状态、行/列表头
  ID 路径。跨格 td 的多个位置引用同一 cell ID，仅 origin 携带原文与值；
  缺失格与内容为空的 td 分别记录。表被拒绝时不发布此文件（并清除陈旧产物）。
- `headers.json` — 表头关系。`headers` 登记全部 th（ID、文本、scope、位置、
  跨度、自身显式 headers 链）；`relations` 给出每个源数据 cell 的表头 ID 路径
  及来源（headers 属性 / scope 推断）。
- `problems.json` — 问题记录。fatal（拒绝该表）：占位重叠、越界行跨度、非法
  跨度值、表内 cell ID 重复、表头引用环、headers 指向缺失 ID/td/另一张表、
  找不到目标表；warning/info：缺失格、空单元格、类型转换失败、未知 data-type。
  每条问题定位到源 cell（行组、行号、列号、cell ID）。

### 受限规范（对应 fixtures/scope.json 的 rules）

- 网格：单元格放在本行首个未占列；colspan 为正整数，rowspan 为正整数或 0
  （0 延伸到本行组结束，不跨行组）；正跨度不得与已占格子相交或跨出行组。
- 隐式表头：列头取 thead 中 `scope=col/colgroup` 且列区间相交的 th（按行再列
  排序）；行头取同一 tbody 中 `scope=row` 覆盖目标行的 th，`scope=rowgroup`
  作用于所在 tbody 的每一行。不实现 HTML 全部自动推断算法。
- 显式 headers：存在 headers 属性时递归展开被引 th 自己的 headers，祖先在前、
  自身在后，按首次出现去重；显式列表取代 scope 推断；被引 th 的 scope 为
  row/rowgroup 归入行路径，其余归入列路径。
- 文本：单元格后代文本（排除嵌套 table），仅去首尾空白，内部空白保留；
  相同显示文本但不同 ID 的表头不合并。
- 类型：`data-type=text|int|decimal`；int 输出规范有符号十进制；decimal 必须
  恰好两位小数；无 data-type 按 text（标识符的前导零因此保留）。类型转换失败
  保留原文与原因，不改变网格与表头关联。

### 测试

```bash
python3.11 -m unittest discover -s tests -v
```

覆盖：catalog 两个等价页面（属性顺序、嵌套无关内容不同）输出逐字节一致；
手工追查跨两行两列的 v4 与显式 headers 覆盖 scope 的 v3；三个坏例（引用环、
跨行组 rowspan、跨表 ID 引用）断言失败定位到源 cell 且不发布成功表；以及
重叠、缺失格/空格、同文不同 ID 表头、类型失败保留原文等边界。
