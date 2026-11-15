# OTLP 指标时间性离线归一化器

把标准 OTLP/JSON 指标批（`ResourceMetrics → ScopeMetrics → Metric → DataPoint`）
离线归一化为数仓可直接装载的三类结果：

1. **半开区间事实表** `fact_table`：统一为 `[start, end)` 的区间增量；
2. **原点对应关系** `origin_mapping`：每个输入点恰好一条，说明它进入了哪个事实、
   被折叠进哪个点、或为何不能确认；
3. **不能确认的记录** `unconfirmed`：全部冲突、歧义与 schema 违规，附原因码与载荷。

只支持**单调 Sum**（`sum.isMonotonic=true` + 字符串 `asInt`）与
**显式桶 Histogram**（`explicitBounds` + `bucketCounts` + `count`）。
Gauge / Summary / ExponentialHistogram / 非单调 Sum 一律隔离。不做告警、统计推断、
重采样、补零或可视化；不依赖 OTel Collector 或任何指标数据库，纯 Python 3.11 标准库。

## 快速使用

```bash
# 人类可读诊断报告
python -m otlp_normalize inspect fixtures/metrics.json

# 结构化 JSON（三表合一）
python -m otlp_normalize normalize fixtures/metrics.json

# 三张 JSONL 事实/原点/隔离表
python -m otlp_normalize normalize fixtures/metrics.json --out-dir out/

# 多批共享同一身份空间；不按文件名选点，跨文件同样参与折叠/冲突
python -m otlp_normalize normalize fixtures/metrics.json fixtures/metrics-shuffled.json
```

```python
from otlp_normalize import normalize, inspect

result = normalize("fixtures/metrics.json")   # 也接受 dict、JSON 字符串或它们的列表
result.facts          # list[FactRow]
result.origins        # list[OriginRecord]，每个输入点一条
result.unconfirmed    # list[UnconfirmedRecord]
result.to_json()      # 三表合一的 JSON
result.write_jsonl("out/")
text = inspect("fixtures/metrics.json")
```

## 数值精确性

* 纳秒时间戳、`asInt`、`count`、`bucketCounts` 按 OTLP 规范是**十进制字符串**，
  解析为 Python 任意精度 `int`，全程整数运算，输出仍为整数字段，不经过 `float`。
* `explicitBounds` 与 histogram `sum` 是 JSON number，由自定义 decoder 解析为
  `Decimal`，比较与逐桶/逐点相减都是精确十进制；输出保留为十进制**字符串**
  （如 `"0.30"`），杜绝二进制浮点尾数。代码库中不存在任何 `float(...)` 转换。
* 大整数用例 `9007199254740997 → 9007199254740999` 的差值精确为 `2`，无 2^53 截断。

## 流身份

身份是可哈希、可确定性排序的纯 tuple，属性集合先按 key 排序再入身份，
因此**属性顺序不影响身份**：

```
(resource 属性, scope 名称, scope 版本, metric 名称, 单位, metric 类型, 点属性)
```

属性值带类型标签：字符串 `"1"` 与整数 `1` 的标签不同（`s:` vs `i:`），
因此是两条不同的流。仅支持 `stringValue / intValue / boolValue`；
同一属性集合出现重复 key 即非法（resource、scope、point 三级都校验），
该点进入隔离表。

## 归一化语义

### 折叠与冲突（先于时间性处理）

* 同流、同 `[start,end)`、**内容完全相同**的点折叠为一条事实，被折叠点在原点表
  标记 `collapsed` 并指向锚点；
* 同流、同区间但**内容冲突**（值/桶/count/sum 任一不同）→
  `duplicate_identity_interval_conflict`，全部隔离，不挑选其中一条；
* 同一完整身份同时出现 delta 与 cumulative 描述符 →
  `mixed_temporality`，**整流拒绝**，不拆成两条流掩盖矛盾。

### DELTA（aggregationTemporality=1）

点本身直接描述 `[startTime, time)` 的区间增量，值/桶原样成为事实。
对两两半开相交的点求连通分量：

* 完全相同的区间已在折叠阶段处理；
* 任何**部分重叠**分量（含多点链状重叠）→ `delta_partial_overlap_conflict`，
  分量内所有点隔离，**不把重叠部分均匀摊开**；
* 相邻不重叠区间（前点 `end == 后点 start`）同属一个计数代；出现缺口时另起一代，
  缺口只加 `gap_before_interval` 质量标记，不补零。

### CUMULATIVE（aggregationTemporality=2）

点按 `(time, startTime)` 排序后驱动计数代（epoch）状态机：

* **首区间**：每个计数代的第一个有效点，从其声明的 `startTime` 到 `time`
  形成首区间，值取该点自身累计值（质量 `first_interval_of_epoch`）；
* **相邻差值**：同一 `startTime` 内的后续点，与前一有效基线点做差形成
  `[基线.time, 当前.time)`；
  * Sum：`asInt` 差值，负数 → `monotonic_regression`，该代立即停止；
  * Histogram：必须 `explicitBounds` 完全相同才**逐桶相减**；同时校验桶差非负、
    `count` 差非负且等于桶差之和；任一倒退 → `monotonic_regression` 并停止该代；
* **边界变化**：同 `startTime` 下 `explicitBounds` 变化 →
  `incomparable_bucket_bounds`，无法从两次累计桶恢复可靠增量，
  该代基线停止，**不按桶下标硬减**；直到出现显式新 `startTime` 的新计数代；
* **计数代衔接**：`startTime` 改变即新代。新起点 `>= 前一已确认结束` 才可衔接
  （相等允许，仅 `>` 时标缺口）；新起点 `< 前一已确认结束` →
  `cumulative_epoch_overlap_ambiguous`，报告歧义且不产生事实；
* 基线已失效而当前点未声明新 `startTime` → `baseline_unavailable`；
* 缺口绝不补零、不重采样。

### Histogram 的 sum

`sum` 字段允许缺失，缺失**不补 0**。首区间的 sum 取点自身（缺失则 `sum_known=false`）；
差值区间只有在**两个参与点都带 sum** 时才确定，否则输出 `sum=null`、
`sum_known=false` 并加质量标记 `sum_unknown`。

### Histogram 逐点 schema 校验

* `explicitBounds` 必须全部有限且**严格递增**；
* `len(bucketCounts) == len(explicitBounds) + 1`，桶计数为非负整数字符串；
* `count` 为非负整数字符串且**精确等于桶计数之和**；
* `sum` 若存在必须是有限数。

## 输出结构

事实行（`fact_table`）字段：

| 字段 | 含义 |
| --- | --- |
| `fact_id` | 事实 ID（`F####`，分配序，不参与等价比较） |
| `identity` | 完整流身份（resource/scope/metric/point 四级属性） |
| `interval` | `{start, end, half_open:"[start,end)"}`，整数纳秒，`start < end` |
| `kind` | `monotonic_sum` / `histogram` |
| `aggregationTemporality` | 来源点的时间性（1/2） |
| `generation` | 计数代序号与该代声明起点 |
| `quality` | 质量标记：`ok / delta_point / first_interval_of_epoch / gap_before_interval / sum_unknown` |
| `value` | Sum：`{type:"sum", asInt}`；Histogram：边界、**增量桶**、增量 count、增量 sum（或 null）、`sum_known` |
| `source_origin_ids` | 参与事实的全部源点（含被折叠点） |

原点行（`origin_mapping`）状态：`fact`（进入事实）、`collapsed`（精确重复被折叠）、
`unconfirmed`（隔离，附 `unconfirmed_id` 与原因）。事实表按
`(身份, start, end, generation)` 稳定排序，因此乱序批与属性换序批的规范结果一致。

## 模块划分

```
otlp_normalize/
  canonical.py   类型化属性、规范身份（排序 + 类型标签）、严格整数解析
  model.py       数据结构、原因码、ExactJSONDecoder（number→Decimal）
  parser.py      OTLP 遍历与逐点 schema 校验，产出 RawPoint / 原点 / 隔离记录
  engine.py      折叠冲突、DELTA 重叠分量、CUMULATIVE 计数代状态机
  report.py      三表 JSON 渲染与 inspect 文本
  normalizer.py  交付入口 normalize() / inspect()、CLI 后端
  __main__.py    python -m otlp_normalize {normalize|inspect}
tests/test_normalizer.py   29 个 unittest 用例
```

## 验证（29 个用例，纯标准库）

```bash
python -m unittest tests.test_normalizer -v
```

覆盖：乱序批等价、属性换序、字符串 1/整数 1 分流、精确折叠、同区间内容冲突、
混合 temporality 整流拒绝、delta 部分重叠、累计单调倒退（sum 与桶）、
计数代重叠歧义、代衔接边界（相等可接/更早拒绝）、缺口不补零、桶边界变化
`incomparable` 与新代恢复、缺 sum 传播、非法桶 schema、重复属性、大整数精度、
Decimal 边界无浮点尾数、多文件共享身份不按文件名选点等。

### 材料等价流核对（手算）

`cum_events` 与 `delta_events` 是不同 metric 身份、数值区间等价的两条独立流，
归一化后逐区间配对一致（测试 `test_03`）：

| 区间 | cum 相邻差 | delta 原值 |
| --- | --- | --- |
| `[0,10)` | 首点 4 | 4 |
| `[10,20)` | 9−4=5 | 5 |
| `[20,30)` | 新代首点 3 | 3 |

`cum_hist` 与 `delta_hist` 同样配对（测试 `test_04`），桶差手算：

```
[2,4,3] − [1,2,1] = [1,2,2]，count 9−4=5，sum 140−50=90
```

与 `delta_hist` 第二点 `[1,2,2] / count 5 / sum 90` 完全一致。
`bad_hist_schema` 在同 `startTime=0` 下边界由 `[10,20]` 变为 `[15,20]`，
该点判 `incomparable_bucket_bounds` 并停止该代；`startTime=20` 的新点
重新形成 `[20,30)` 事实（中间 `[10,20)` 缺口保留、不补零）。
