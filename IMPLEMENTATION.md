# 受限内存多 DISTINCT 精确聚合（实现说明）

实现文件：`aggregate.py`（Python 3.11，仅标准库）。
独立核对脚本：`tests/verify.py`（SQLite / Counter 只用于核对，工具本身不把 GROUP BY 交给数据库）。

## 运行命令

```bash
# 复现默认预算：state_entry_limit=16, merge_fan_in=3
python3 aggregate.py \
  --input fixtures/rows.jsonl \
  --config fixtures/config.json \
  --out-dir out

# 查看输出
cat out/results.jsonl   # 每组一行，NULL 组在前，其余按 UTF-8 字节排序
cat out/report.json     # 溢写/归并运行报告

# 独立核对 + 边界/报错/确定性测试
python3 tests/verify.py
```

`--out-dir` 成功时写出 `results.jsonl` 与 `report.json`。非法输入或非法 config 时进程退出码为 `2`，stderr 给出 `aggregate: error: ...`，且不产出结果文件。

## 输出语义

- `group`、`user`、`product` 只接受 `string` 或 `null`；NULL group 是一个真实分组（输出为 `"group": null`，排在最前）。空串 `""` 是独立的真实键，与 NULL 不同。
- `count` 计该组所有输入行。
- `sum` 只累加非 NULL 金额；若该组没有任何非 NULL 金额则输出 `null`。金额按整数“分”精确累加，输出统一两位小数，全程不经过浮点。
- `user_distinct` 忽略 NULL user。
- `pair_distinct` 只计 `user` 与 `product` 均非 NULL 的二元组。pair 以 `(user, product)` 二元结构存盘/序列化，不使用分隔符拼接，因此不存在歧义（例如 `a|b` + `c` 与 `a` + `b|c` 不会被混同）。
- 排序：NULL 组在前；其余按 `group.encode("utf-8")` 的字节序（不是 Unicode 码点序）。
- 相同业务内容但 `row_id` 不同，各计一行；只有 `row_id` 重复才视为输入非法。

金额文法：`-?[0-9]+(\.[0-9]{1,2})?`。`"1"`、`"2.5"`、`"-0.00"` 合法；`"1.005"`、`".50"`、`"+1.00"`、JSON 数字、`null` 以外的非字符串均非法。

```bash
# 非法样例：第 2 行金额三位小数 -> 整次失败
python3 aggregate.py --input fixtures/invalid.jsonl \
  --config fixtures/config.json --out-dir out-invalid
# aggregate: error: invalid amount '1.005': ...   (exit 2)
```

## 驻留状态计数口径（state_entry_limit）

`state_entry_limit` 约束“同时驻留的聚合状态项”，每项计数如下：

| 项 | 计数 |
| --- | --- |
| 每个驻留 group 的累计器（count / sum / has_sum） | 1 |
| 每个驻留的 distinct user 键 | 1 |
| 每个驻留的 distinct (user, product) pair 键 | 1 |

`resident = 驻留 group 累计器数 + 驻留 user 键数 + 驻留 pair 键数`，任何时刻均有 `resident <= state_entry_limit`，报告中的 `max_resident_entries` 是全程峰值，`peak_resident_detail` 给出峰值时的三项拆分。

**不计数**（按任务约定）：当前正在处理的输入行、固定 `64` 条记录的输出暂存缓冲（`CHUNK_RECORD_LIMIT`）、`row_id` 重复检测用的 `row_id` 集合（受输入行数约束，本任务规模 ≤ 万行）、解释器与固定 I/O 开销。

计数入口：报告 `spill.counters` 保留可核查的累计计数，包括

- `group_states_created`：创建的 group 累计器数；
- `group_spills`：group 累计器被淘汰次数；
- `user_key_items_added_to_memory` / `pair_key_items_added_to_memory`：进入内存计数的 distinct 键项；
- `user_key_direct_spills` / `pair_key_direct_spills`：在无空位时直接写 run 的键（不占驻留项）；
- `user_key_records_written` / `pair_key_records_written`：写入 run 的 distinct 记录；
- `duplicate_user_keys_in_memory` / `duplicate_pair_keys_in_memory`：内存内命中的重复键（不新增驻留项）；
- `aggregate_partials_written`：写出的 group 累计偏置记录数（含最后的内存冲刷）；
- `chunk_flushes`：初始 run 数；`merge_records_read/written`：归并吞吐。

## 溢写与归并策略

1. **内存哈希聚合**：`group -> GroupState`，`GroupState` 持有 `count / sum_cents / has_sum / users / pairs`。
2. **达到上限时的淘汰**：
   - 需要新建 group 且无空位：淘汰一个驻留 group 累计器（连同其 distinct 键）为一条按 group 排序的 run；淘汰得到的记录先进入固定 64 条的暂存缓冲，缓冲满才真正落盘，因此不会“每个 group 淘汰就产生一个微小 run”。
   - 需要新增 distinct 键且无空位：先冲刷**该 group 自己**的 distinct 键到 run，但**保留**其 count/sum 累计器。因此单个热点组的去重集合不会因为“只有一个 group”而无限增长，同时计数/金额累计不必随每个键反复落盘重算。
   - 若该组也没有键可冲、且所有驻留项都是其它组的累计器，则把新键直接写 run（`*_direct_spills`），不强行淘汰累计器。
3. **run 排序键**：`(group 排序键, 类型序号, 键...)`，类型序号 `A=0` 累计偏置、`U=1` user 键、`P=2` pair 键。因此同一组的全部 `A` 记录连续且排在最前，`U` 与 `P` 各自形成独立、连续的排序域——两个 distinct 度量不会串用去重域。
4. **多轮归并**：每轮最多同时打开 `merge_fan_in` 个 run 做 k 路堆归并（堆里最多 `fan_in` 条记录），输出合并后的有序 run；run 数超过 `fan_in` 时重复该轮直到只剩 1 个。`merge_rounds` 是实际执行轮数，`max_open_runs_in_merge <= merge_fan_in`。
5. **跨 run 去重**：归并阶段对有序流做相邻去重——`A` 记录按组累加 count/sum；`U`/`P` 记录仅当与其前一条签名完全相同时跳过。由于排序把相同键排到相邻位置，跨任意多个 run 的同一 distinct 键只会计一次。归并状态是 O(1)（不把某个组整体读入容器）。
6. **最终扫描**：对最终有序 run 再流式扫描，按组输出一行结果；每组的 user/pair 计数同样只依赖相邻去重，常驻 O(1)。

## 临时文件生命周期

- 所有 spill/merge run 位于 `tempfile.TemporaryDirectory(prefix="aggregate-")` 中；成功结束、以及可处理的输入错误（`InputError`）退出时目录都会被删除。
- 归并完成后，上一轮输入 run 立即删除，只留下当前轮输出，磁盘占用不随轮数线性累积。
- `results.jsonl` / `report.json` 先写入同目录的 `.{name}.tmp`，再 `os.replace` 原子替换，避免留下半成品；异常时清理 `.tmp`。
- 若进程被强杀（SIGKILL 等），操作系统会在系统临时目录留下 `aggregate-*`，可手动清理；正常路径不会残留。

## 复杂度与规模

- 驻留状态：`O(state_entry_limit)`；归并：每路一个文件句柄、堆中 `O(merge_fan_in)` 条记录。
- 排序/溢写为外部排序思路；总 I/O `O(N log_{fan_in} R)` 量级（N 行、R 初始 run 数）。
- 本任务规模控制在万行以内，未做大压测。

## 已覆盖的核对场景（`tests/verify.py`）

- 默认预算 16 / fan-in 3：`runs=11`、`merge_rounds=3`（>1 个 run、多轮归并）。
- 预算 `1..1000` × fan-in `2..8` 矩阵：结果与 Python Counter 及 SQLite 独立查询逐组完全一致。
- 输入顺序 6 次随机打乱：逻辑结果不变，仍触发多轮归并。
- NULL 组 / 空串组、空串 user/product、NULL-only 金额、金额抵消、UTF-8 字节序、空输入。
- 单热点组（53 users / 41 products）在预算 1 / 4 / 16 下仍受 `state_entry_limit` 约束并跨 run 去重。
- 非法字段、重复 `row_id`、三位小数、浮点金额、非字符串列、布尔/缺失/多余字段、非法 config 均整次失败（exit 2）。
- 重复运行输出字节级一致。
